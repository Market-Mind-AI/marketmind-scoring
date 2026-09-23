"""A sentinela olha resultado, não processo.

As probes dizem se o processo está de pé. Em 22/09/2026 o refresh do World Bank
recoletava 288 indicadores por noite e terminava `Complete`, em verde, com nada
certo. Processo de pé, resultado errado — e nada via.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analysis_request import AnalysisRequest
from app.services.collection_client import CollectionDispatchError
from app.services.macro_coverage import Auditoria, MacroPrewarmService
from app.services.sentinela import Sentinela
from app.utils.time import utc_now


async def _relatorio(session: AsyncSession) -> dict:
    return (await Sentinela(session).verificar()).to_dict()


def _de(relatorio: dict, nome: str) -> dict:
    return next(v for v in relatorio["verificacoes"] if v["nome"] == nome)


async def test_an_analysis_past_its_own_deadline_is_a_finding(db_session: AsyncSession) -> None:
    """Trabalho que o comprador pagou e não recebeu. A varredura de retomada
    existe justamente para isso — se ainda assim sobrou uma, foi ela que
    falhou, e é o tipo de coisa que ninguém percebe olhando pods."""
    presa = AnalysisRequest(
        id=uuid.uuid4(),
        status="running",
        countries=["ESP"],
        created_at=utc_now() - timedelta(days=2),
        deadline_at=utc_now() - timedelta(hours=3),
    )
    db_session.add(presa)
    await db_session.flush()

    v = _de(await _relatorio(db_session), "analises_presas")

    assert v["ok"] is False
    assert str(presa.id) in v["detalhe"]


async def test_an_analysis_still_within_its_deadline_is_not(db_session: AsyncSession) -> None:
    """Uma análise pode ficar horas em `running` legitimamente, insistindo numa
    fonte fora do ar. O prazo é o que separa insistir de estar presa."""
    viva = AnalysisRequest(
        id=uuid.uuid4(),
        status="running",
        countries=["ESP"],
        created_at=utc_now(),
        deadline_at=utc_now() + timedelta(hours=20),
    )
    db_session.add(viva)
    await db_session.flush()

    v = _de(await _relatorio(db_session), "analises_presas")

    assert str(viva.id) not in v["detalhe"]


async def test_a_perfect_night_leaves_no_trace_and_must_still_pass(db_session: AsyncSession) -> None:
    """A regressão que motivou esta verificação, em 23/09/2026.

    A primeira versão lia a última linha de `jobs` com
    `requested_by='macro_prewarm'`. Mas o pré-aquecimento só cria Job quando
    acha trabalho — `if not faltando: continue` — que é a eficiência que se
    pediu dele. Naquele dia ele terminou em verde às 04:00 sem criar Job
    nenhum, e a sentinela das 05:00 relatou "última passagem há 16.4h",
    apontando para uma execução manual da véspera.

    Quanto mais saudável o sistema, mais cedo o alarme tocaria. Por isso o
    banco aqui NÃO tem Job nenhum: tudo fresco tem de passar mesmo assim.
    """
    auditoria = Auditoria(paises=24, indicadores=406, vencidos=0)

    with patch.object(MacroPrewarmService, "auditar", AsyncMock(return_value=auditoria)):
        v = _de(await _relatorio(db_session), "macro_quente")

    assert v["ok"] is True
    assert "todos frescos" in v["detalhe"]


async def test_macro_left_stale_is_a_finding_and_names_the_countries(db_session: AsyncSession) -> None:
    """Às 05:00, uma hora depois do pré-aquecimento, vencido significa que ele
    não deu conta: o TTL é de 30 dias, nada vence sozinho em uma hora. Em
    25/08/2026 a ONU ficou 29 minutos fora e nove países saíram de um ranking —
    o nome do país é o que diz onde olhar."""
    auditoria = Auditoria(paises=24, indicadores=406, vencidos=12, paises_com_vencidos=["ARG", "PER"])

    with patch.object(MacroPrewarmService, "auditar", AsyncMock(return_value=auditoria)):
        v = _de(await _relatorio(db_session), "macro_quente")

    assert v["ok"] is False
    assert "12 vencidos em 2 países" in v["detalhe"]
    assert "ARG" in v["detalhe"] and "PER" in v["detalhe"]


async def test_a_source_that_cannot_be_asked_is_a_finding_not_a_pass(db_session: AsyncSession) -> None:
    """ "Não consegui perguntar" é diferente de "perguntei e está tudo bem", e
    às 05:00 também significa que o pré-aquecimento das 04:00 provavelmente não
    conseguiu coletar."""
    quebrado = AsyncMock(side_effect=CollectionDispatchError("undata freshness failed"))

    with patch.object(MacroPrewarmService, "auditar", quebrado):
        v = _de(await _relatorio(db_session), "macro_quente")

    assert v["ok"] is False
    assert "a própria verificação falhou" in v["detalhe"]


async def test_a_check_that_blows_up_is_itself_a_finding(db_session: AsyncSession) -> None:
    """Uma verificação que quebra não pode passar por verificação que passou —
    seria o silêncio que este arquivo existe para acabar."""

    async def _analises_presas(self) -> None:  # noqa: ANN001 — precisa do mesmo nome e do self
        raise RuntimeError("banco fora")

    with patch.object(Sentinela, "_analises_presas", _analises_presas):
        relatorio = await _relatorio(db_session)

    v = _de(relatorio, "analises_presas")
    assert v["ok"] is False
    assert "a própria verificação falhou" in v["detalhe"]
    assert relatorio["ok"] is False, "e o relatório inteiro fica vermelho"
