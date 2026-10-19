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


# --- o idioma é o de quem pediu (2026-10-19) --------------------------------
#
# A interface é EN/ES e o padrão configurado era PT: quem escolheu espanhol
# recebia a explicação em português. O idioma viaja com a análise; nulo é o
# configurado, como sempre foi.


def _narrador(texto: str = "ok"):  # noqa: ANN202
    from unittest.mock import AsyncMock

    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=texto)
    return provider


async def test_the_narrative_is_written_in_the_language_the_analysis_asked_for(db_session) -> None:  # noqa: ANN001
    from unittest.mock import patch

    from app.services.explanation_client import _SYSTEM_PROMPT_ES

    provider = _narrador("España lidera.")
    with patch("app.services.explanation_client.resolve_explanation_llm", return_value=provider):
        await ExplanationClient(db_session).narrate(
            packet={"countries": [{"country": "ESP", "mos": 70.0}]}, product_name="air fryer", language="es"
        )

    system = provider.complete.await_args.kwargs["system"]
    assert system.startswith(_SYSTEM_PROMPT_ES[:40])
    assert "Write the entire narrative in this language" not in system, "Spanish has its own texts now"


async def test_without_a_language_the_configured_default_still_applies(db_session) -> None:  # noqa: ANN001
    from unittest.mock import patch

    from app.core.config import settings
    from app.services.explanation_client import _SYSTEM_PROMPT_PT

    provider = _narrador()
    with (
        patch.object(settings, "ollama_llm_narrative_language", "pt"),
        patch("app.services.explanation_client.resolve_explanation_llm", return_value=provider),
    ):
        await ExplanationClient(db_session).narrate(
            packet={"countries": [{"country": "ESP", "mos": 70.0}]}, product_name="air fryer", language=None
        )

    assert provider.complete.await_args.kwargs["system"].startswith(_SYSTEM_PROMPT_PT[:40])


async def test_the_revision_is_asked_for_in_the_narrative_language(db_session) -> None:  # noqa: ANN001
    """Um pedido em inglês sobre um texto em espanhol convida o modelo a
    traduzi-lo — e a regra de revisão manda manter o idioma do original."""
    from unittest.mock import patch

    provider = _narrador("Texto revisado")
    with patch("app.services.explanation_client.resolve_explanation_llm", return_value=provider):
        await ExplanationClient(db_session).revise(
            narrative="España es la mejor apuesta.", claims=["no cubre costes"], language="es"
        )

    pedido = provider.complete.await_args.kwargs["user"]
    assert pedido.startswith("REVISIÓN.")
    assert "España es la mejor apuesta." in pedido
