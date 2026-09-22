"""The cases are the yardstick: they have to parse, be unique, and say what they expect."""

import pytest

from evaluation import cases


def test_every_dataset_loads() -> None:
    assert cases.DATASETS == ("identify-product", "catalog-match", "reference-markets")
    for name in cases.DATASETS:
        loaded = cases.load(name)
        assert loaded.name == name
        assert loaded.description
        assert loaded.items


def test_the_case_counts_are_what_the_spec_says() -> None:
    assert len(cases.load("identify-product").items) == 25
    assert len(cases.load("catalog-match").items) == 15
    assert len(cases.load("reference-markets").items) == 12


@pytest.mark.parametrize("name", ["identify-product", "catalog-match", "reference-markets"])
def test_ids_are_unique(name: str) -> None:
    ids = [case.id for case in cases.load(name).items]

    assert len(ids) == len(set(ids))


def test_identification_cases_carry_what_the_evaluators_read() -> None:
    for case in cases.load("identify-product").items:
        assert case.input["hint_text"]
        assert case.input["countries"]
        assert isinstance(case.expected_output["product_found"], bool)
        assert isinstance(case.expected_output["product_name"], list)
        assert isinstance(case.expected_output["category_required"], bool)


def test_a_quarter_of_the_identification_cases_name_no_product() -> None:
    """The rule they exist to check: text that names no product must not become one."""
    found = [c for c in cases.load("identify-product").items if not c.expected_output["product_found"]]

    assert len(found) >= 3


def test_catalog_cases_include_look_alikes_that_must_not_match() -> None:
    items = cases.load("catalog-match").items
    for case in items:
        assert case.input["text"]
        assert case.input["candidates"]
        assert set(case.expected_output) == {"expected_title"}
    assert sum(1 for c in items if c.expected_output["expected_title"] is None) >= 4


def test_market_cases_expect_only_supported_countries() -> None:
    basket = set(cases.SUPPORTED_COUNTRIES)
    for case in cases.load("reference-markets").items:
        expected = case.expected_output["expected_markets"]
        must = case.expected_output["must_include"]
        assert set(expected) <= basket, case.id
        assert set(must) <= set(expected), case.id


def test_an_unknown_dataset_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown dataset"):
        cases.load("does-not-exist")
