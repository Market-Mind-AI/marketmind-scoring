"""A segunda passada sobre a narrativa.

A checagem de NÚMEROS fica em zero e a de AFIRMAÇÕES não. Medido em 23/09/2026:
uma execução com `hallucination_rate=0.0` (0 de 134 números) afirmava mesmo
assim que "os EUA não fazem parte do ranking" e que "a Espanha é uma aposta de
exportação mais coerente" — a segunda os dados não podem sustentar, porque não
cobrem custo, tarifa nem exigência sanitária.

O invariante do projeto é que o modelo nunca calcula um número, e é ele que
torna a primeira checagem exata e gratuita. Nunca foi defesa contra inventar a
conclusão.
"""

from app.services.explanation_client import ExplanationClient

# --- a segunda passada (23/09/2026) -----------------------------------------


async def test_a_revision_asks_for_each_flagged_claim_to_be_fixed(db_session) -> None:  # noqa: ANN001
    """Reescrever, não apagar: a frase ofensiva costuma ter uma versão honesta
    ("lidera o MOS" no lugar de "é a melhor aposta")."""
    from unittest.mock import AsyncMock, patch

    provider = AsyncMock()
    provider.complete = AsyncMock(return_value="Texto revisto")

    with patch("app.services.explanation_client.resolve_explanation_llm", return_value=provider):
        saida = await ExplanationClient(db_session).revise(
            narrative="A Espanha é a melhor aposta.", claims=["não cobre custo nem tarifa"]
        )

    assert saida == "Texto revisto"
    pedido = provider.complete.await_args.kwargs["user"]
    assert "não cobre custo nem tarifa" in pedido
    assert "A Espanha é a melhor aposta." in pedido


async def test_a_revision_that_fails_keeps_the_delivered_text(db_session) -> None:  # noqa: ANN001
    """Melhor esforço, como a narrativa: o texto já foi entregue, e uma revisão
    que não sai nunca pode custar a explicação que existe."""
    from unittest.mock import AsyncMock, patch

    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("provedor fora"))

    with patch("app.services.explanation_client.resolve_explanation_llm", return_value=provider):
        assert await ExplanationClient(db_session).revise(narrative="texto", claims=["c"]) is None


async def test_nothing_flagged_means_nothing_to_revise(db_session) -> None:  # noqa: ANN001
    assert await ExplanationClient(db_session).revise(narrative="texto", claims=[]) is None
