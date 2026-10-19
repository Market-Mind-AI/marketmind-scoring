"""The instruction texts the models receive, pinned before they moved to Langfuse.

While the defaults are in use (no Langfuse — as in every test), moving the texts
into Langfuse Prompt Management must not change a byte of what the model sees.
The snapshot was generated from the code before the move.

The vision entries were regenerated on 2026-09-28, on purpose: the instructions
became the (cached) system prompt with no variables, and the countries moved to
the user message. Each entry is the system prompt, a separator, and the user
message.

The `es` entries were regenerated on 2026-10-19, on purpose: Spanish got its own
instruction (`explanation-system-es`) instead of the English one plus a "write
in this language" line. The `fr` entries pin that fallback, which is what any
language without texts of its own still gets.

To regenerate: render each key below with the same calls these tests make and
write the JSON back (ensure_ascii=False, indent=2, sort_keys=True); then check
that only the entries you meant to change did."""

import itertools
import json
from functools import lru_cache
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.explanation_client import _system_prompt
from app.services.vision_client import ProductIdentification, VisionClient

_SNAPSHOT_FILE = Path(__file__).parent / "prompt_snapshots.json"

_EXPLANATION_CASES = list(
    itertools.product(("pt", "en", "es", "fr"), (True, False), (False, True), (None, "Quero vender no Chile"))
)

_VISION_CASES = [
    ("ZmFrZQ==", "image/jpeg", "air fryer 5L", ["BRA", "USA"]),
    ("ZmFrZQ==", "image/jpeg", None, ["BRA", "USA"]),
    (None, None, "air fryer 5L", ["BRA", "USA"]),
    ("ZmFrZQ==", "image/jpeg", "air fryer 5L", []),
    ("ZmFrZQ==", "image/jpeg", None, []),
    (None, None, "air fryer 5L", []),
]


@lru_cache(maxsize=1)
def _snapshot() -> dict[str, str]:
    return json.loads(_SNAPSHOT_FILE.read_text(encoding="utf-8"))


def _explanation_key(language: str, single: bool, champion: bool, user_prompt: str | None) -> str:
    return f"explanation|{language}|single={single}|champion={champion}|user_prompt={user_prompt is not None}"


def _vision_key(image: str | None, hint: str | None, countries: list[str]) -> str:
    return f"vision|image={image is not None}|hint={hint is not None}|countries={','.join(countries)}"


async def _vision_prompt(image: str | None, media_type: str | None, hint: str | None, countries: list[str]) -> str:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ProductIdentification(
            product_name="air fryer",
            brand="Philips",
            category="kitchen",
            target_age_min=None,
            target_age_max=None,
            estimated_price=89.9,
            price_currency="USD",
            confidence=0.9,
        )
    )
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        await VisionClient().identify_product(
            image_base64=image, image_media_type=media_type, hint_text=hint, countries=countries
        )
    kwargs = provider.complete.await_args.kwargs
    return f"{kwargs['system']}\n---\n{kwargs['user']}"


@pytest.mark.parametrize(("language", "single", "champion", "user_prompt"), _EXPLANATION_CASES)
def test_the_explanation_system_prompt_is_unchanged(language, single, champion, user_prompt) -> None:
    rendered = _system_prompt(language, single_country=single, user_prompt=user_prompt, external_champion=champion)

    assert rendered == _snapshot()[_explanation_key(language, single, champion, user_prompt)]


@pytest.mark.parametrize(("image", "media_type", "hint", "countries"), _VISION_CASES)
async def test_the_vision_instructions_are_unchanged(image, media_type, hint, countries) -> None:
    rendered = await _vision_prompt(image, media_type, hint, countries)

    assert rendered == _snapshot()[_vision_key(image, hint, countries)]
