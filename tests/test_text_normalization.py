from app.utils.text import normalize_alias


def test_lowercases_and_collapses_whitespace() -> None:
    assert normalize_alias("  Air   Fryer  ") == "air fryer"


def test_strips_accents() -> None:
    assert normalize_alias("Fritadeira sem Óleo") == "fritadeira sem oleo"


def test_strips_punctuation() -> None:
    assert normalize_alias("Air-Fryer, XL!") == "air fryer xl"


def test_same_input_always_normalizes_the_same() -> None:
    assert normalize_alias("Air Fryer") == normalize_alias("  air   FRYER ")
