"""A noite correu bem?

As probes e os eventos do Kubernetes respondem se os processos estão de pé, e
foi o que faltou até 22/09/2026: seis defeitos viveram semanas porque ninguém
olhava. Mas processo de pé não é resultado: naquele mesmo dia o refresh do World
Bank recoletava 288 indicadores por noite e terminava `Complete`, em verde, sem
que nada estivesse certo.

Isto responde a outra pergunta — o que o sistema TEM, não se ele rodou. Cada
verificação nasceu de uma falha real:

    frescor        o UN Data era perguntado em ISO3 quando indexa por ISO2, e
                   respondia vazio: tudo parecia vencido e era recoletado
                   inteiro, para sempre.
    macro quente   o pré-aquecimento falhava no prazo de uma hora e o macro dos
                   24 países ficava dependente das fontes estarem de pé no
                   minuto da análise -- em 25/08/2026 a ONU ficou 29 minutos
                   fora e 9 países saíram de um ranking. Pergunta-se pelo dado,
                   não pelo job: ver `_macro_quente`.
    pesos          sem eles não há MOS, e eles são versionados por período: o
                   mês virar sem calibragem é uma análise que não sai.
    análises presas uma análise `running` além do prazo é trabalho que o
                   comprador pagou e não recebeu.

O retorno é lido de dois jeitos de propósito. Uma pessoa abre e lê; um CronJob
diário chama com `curl --fail`, e um 503 vira Job `Failed`, que é o que a regra
`sq-mm-cronjob-falhou-dev` já observa. O mesmo alarme, sem infraestrutura nova.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.models.analysis_request import AnalysisRequest
from app.db.models.indicator_weight import IndicatorWeight
from app.utils.time import utc_now


@dataclass
class Verificacao:
    nome: str
    ok: bool
    detalhe: str

    def to_dict(self) -> dict[str, Any]:
        return {"nome": self.nome, "ok": self.ok, "detalhe": self.detalhe}


@dataclass
class Relatorio:
    verificacoes: list[Verificacao] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(v.ok for v in self.verificacoes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verificado_em": utc_now().isoformat(),
            "verificacoes": [v.to_dict() for v in self.verificacoes],
        }


class Sentinela:
    """Olha o resultado, não o processo."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def verificar(self) -> Relatorio:
        relatorio = Relatorio()
        for verificacao in (
            self._pesos_do_periodo,
            self._analises_presas,
            self._macro_quente,
        ):
            try:
                relatorio.verificacoes.append(await verificacao())
            except Exception as exc:  # noqa: BLE001 — uma verificação que quebra É um achado
                nome = verificacao.__name__.lstrip("_")
                logger.exception(f"Verificação {nome} falhou")
                relatorio.verificacoes.append(
                    Verificacao(nome, ok=False, detalhe=f"a própria verificação falhou: {exc}")
                )
        return relatorio

    async def _pesos_do_periodo(self) -> Verificacao:
        """Sem pesos do período, não há MOS — a análise não sai."""
        periodo = (await self._session.execute(select(func.max(IndicatorWeight.period_key)))).scalar()
        quantos = (
            await self._session.execute(
                select(func.count()).select_from(IndicatorWeight).where(IndicatorWeight.period_key == periodo)
            )
        ).scalar() or 0
        atual = utc_now().strftime("%Y-%m")
        ok = quantos > 0 and periodo == atual
        detalhe = f"{quantos} dimensões calibradas para {periodo}"
        if periodo != atual:
            detalhe += f"; o período corrente é {atual} e ainda não foi calibrado"
        return Verificacao("pesos_do_periodo", ok, detalhe)

    async def _analises_presas(self) -> Verificacao:
        """Uma análise `running` além do próprio prazo é trabalho pago e não
        entregue. A varredura de retomada existe para isso; se ainda assim
        sobrou uma, é ela que falhou."""
        agora = utc_now()
        presas = (
            (
                await self._session.execute(
                    select(AnalysisRequest.id).where(
                        AnalysisRequest.status == "running",
                        AnalysisRequest.deadline_at.is_not(None),
                        AnalysisRequest.deadline_at < agora,
                    )
                )
            )
            .scalars()
            .all()
        )
        ok = not presas
        detalhe = "nenhuma" if ok else f"{len(presas)} além do prazo: {[str(i) for i in presas[:5]]}"
        return Verificacao("analises_presas", ok, detalhe)

    async def _macro_quente(self) -> Verificacao:
        """O macro dos países tem de estar quente FORA da análise. Quando o
        pré-aquecimento para, toda análise volta a depender das fontes estarem
        de pé no minuto em que o usuário perguntou.

        Isto pergunta pelo DADO, e a primeira versão perguntava pelo rastro: a
        última linha de `jobs` com `requested_by='macro_prewarm'`. Errado, e de
        um jeito instrutivo — o pré-aquecimento só cria Job quando acha
        trabalho (`macro_coverage`, `if not faltando: continue`), que é a
        eficiência que se pediu dele. Uma noite perfeita não deixa rastro
        nenhum, então quanto mais saudável o sistema, mais cedo a verificação
        ficaria vermelha. Em 23/09/2026 o job das 04:00 terminou em verde e a
        sentinela das 05:00 relatou "última passagem há 16.4h", apontando para
        uma execução manual da véspera.

        Às 05:00, uma hora depois do pré-aquecimento, vencido significa que ele
        não deu conta — o TTL é de 30 dias, nada vence sozinho em uma hora.
        """
        from app.services.macro_coverage import MacroPrewarmService

        auditoria = await MacroPrewarmService(self._session).auditar()
        ok = auditoria.vencidos == 0
        detalhe = f"{auditoria.indicadores} indicadores macro em {auditoria.paises} países"
        if ok:
            detalhe += ", todos frescos"
        else:
            quais = ", ".join(auditoria.paises_com_vencidos[:8])
            detalhe += f"; {auditoria.vencidos} vencidos em {len(auditoria.paises_com_vencidos)} países ({quais})"
        return Verificacao("macro_quente", ok, detalhe)
