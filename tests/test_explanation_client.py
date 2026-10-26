"""A narrativa sai no idioma de quem pediu a análise."""

from app.services.explanation_client import ExplanationClient

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
