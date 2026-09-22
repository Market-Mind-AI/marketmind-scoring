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
    pré-aquecimento o job falhava no prazo de uma hora e o macro dos 24 países
                   ficava dependente das fontes estarem de pé no minuto da
                   análise -- em 25/08/2026 a ONU ficou 29 minutos fora e 9
                   países saíram de um ranking.
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
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.models.analysis_request import AnalysisRequest
from app.db.models.indicator_weight import IndicatorWeight
from app.utils.time import utc_now

# Quanto tempo o pré-aquecimento pode ficar sem uma passagem bem-sucedida. Ele
# roda às 04:00; 26 horas dá folga para uma noite atrasar sem alarme falso, e
# não deixa passar duas.
_PREWARM_MAX_HORAS = 26


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
            self._prewarm_recente,
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

    async def _prewarm_recente(self) -> Verificacao:
        """O macro dos países tem de estar quente FORA da análise. Quando este
        job para, toda análise volta a depender das fontes estarem de pé no
        minuto em que o usuário perguntou."""
        from app.db.models.job import Job

        limite = utc_now() - timedelta(hours=_PREWARM_MAX_HORAS)
        ultimo = (
            await self._session.execute(
                select(func.max(Job.created_at)).where(Job.requested_by == "macro_prewarm")
            )
        ).scalar()
        ok = ultimo is not None and ultimo >= limite
        if ultimo is None:
            detalhe = "nunca rodou"
        else:
            horas = (utc_now() - ultimo).total_seconds() / 3600
            detalhe = f"última passagem há {horas:.1f}h (limite {_PREWARM_MAX_HORAS}h)"
        return Verificacao("prewarm_recente", ok, detalhe)
