from app.utils.text import as_term_list, merge_localized_terms, normalize_alias


def test_lowercases_and_collapses_whitespace() -> None:
    assert normalize_alias("  Air   Fryer  ") == "air fryer"


def test_strips_accents() -> None:
    assert normalize_alias("Fritadeira sem Óleo") == "fritadeira sem oleo"


def test_strips_punctuation() -> None:
    assert normalize_alias("Air-Fryer, XL!") == "air fryer xl"


def test_same_input_always_normalizes_the_same() -> None:
    assert normalize_alias("Air Fryer") == normalize_alias("  air   FRYER ")


def test_as_term_list_tolerates_legacy_string_and_list() -> None:
    assert as_term_list("presunto pata negra") == ["presunto pata negra"]
    assert as_term_list(["a", "b"]) == ["a", "b"]
    assert as_term_list(None) == []
    assert as_term_list(["", "  ", "x"]) == ["x"]  # blanks dropped


def test_merge_localized_terms_unions_and_dedups_per_language() -> None:
    existing = {"pt": ["presunto pata negra"], "en": ["iberico ham"]}
    new = {
        "pt": ["Presunto Pata Negra", "presunto de porco preto"],  # first is a case-dup
        "es": ["jamón ibérico"],  # new language
    }
    merged = merge_localized_terms(existing, new)
    assert merged == {
        "pt": ["presunto pata negra", "presunto de porco preto"],  # dedup keeps first surface form
        "en": ["iberico ham"],
        "es": ["jamón ibérico"],
    }


def test_merge_localized_terms_coerces_legacy_string_shape() -> None:
    # An entity persisted before the list migration holds {lang: "term"}.
    assert merge_localized_terms({"pt": "presunto ibérico"}, {"pt": ["presunto pata negra"]}) == {
        "pt": ["presunto ibérico", "presunto pata negra"]
    }


def test_merge_localized_terms_handles_none() -> None:
    assert merge_localized_terms(None, None) == {}
    assert merge_localized_terms(None, {"pt": ["x"]}) == {"pt": ["x"]}
