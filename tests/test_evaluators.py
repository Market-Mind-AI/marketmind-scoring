"""Scoring is deterministic: same output, same score, no model in the loop."""

from evaluation import evaluators


def _by_name(results) -> dict[str, float]:
    return {e.name: e.value for e in results}


def test_a_correct_identification_scores_every_metric() -> None:
    scores = _by_name(
        evaluators.identification(
            input={"hint_text": "air fryer 5.5L"},
            output={
                "product_found": True,
                "product_name": "Air Fryer",
                "category": "Kitchen Appliances",
                "brand": None,
            },
            expected_output={
                "product_found": True,
                "product_name": ["air fryer"],
                "category_required": True,
                "brand": None,
            },
        )
    )

    assert scores == {
        "product_found_correct": 1.0,
        "product_name_match": 1.0,
        "category_present": 1.0,
    }


def test_accents_and_case_do_not_change_a_name_match() -> None:
    scores = _by_name(
        evaluators.identification(
            input={},
            output={
                "product_found": True,
                "product_name": "Café Colombiano",
                "category": "Coffee",
                "brand": None,
            },
            expected_output={
                "product_found": True,
                "product_name": ["cafe colombiano"],
                "category_required": True,
                "brand": None,
            },
        )
    )

    assert scores["product_name_match"] == 1.0


def test_a_name_outside_the_accepted_list_does_not_match() -> None:
    scores = _by_name(
        evaluators.identification(
            input={},
            output={
                "product_found": True,
                "product_name": "kitchen appliance",
                "category": "Kitchen",
                "brand": None,
            },
            expected_output={
                "product_found": True,
                "product_name": ["air fryer"],
                "category_required": True,
                "brand": None,
            },
        )
    )

    assert scores["product_name_match"] == 0.0


def test_brand_is_scored_only_when_the_case_states_one() -> None:
    with_brand = _by_name(
        evaluators.identification(
            input={},
            output={
                "product_found": True,
                "product_name": "whisky",
                "category": "Spirits",
                "brand": "cardhu",
            },
            expected_output={
                "product_found": True,
                "product_name": ["whisky"],
                "category_required": True,
                "brand": "Cardhu",
            },
        )
    )
    without_brand = _by_name(
        evaluators.identification(
            input={},
            output={
                "product_found": True,
                "product_name": "whisky",
                "category": "Spirits",
                "brand": "Something",
            },
            expected_output={
                "product_found": True,
                "product_name": ["whisky"],
                "category_required": True,
                "brand": None,
            },
        )
    )

    assert with_brand["brand_correct"] == 1.0
    assert "brand_correct" not in without_brand


def test_inventing_a_product_where_there_is_none_scores_zero() -> None:
    scores = _by_name(
        evaluators.identification(
            input={"hint_text": "quero"},
            output={
                "product_found": True,
                "product_name": "air fryer",
                "category": "Kitchen",
                "brand": None,
            },
            expected_output={
                "product_found": False,
                "product_name": [],
                "category_required": False,
                "brand": None,
            },
        )
    )

    assert scores == {"product_found_correct": 0.0}


def test_correctly_refusing_to_invent_a_product_scores_one() -> None:
    scores = _by_name(
        evaluators.identification(
            input={"hint_text": "quero"},
            output={"product_found": False},
            expected_output={
                "product_found": False,
                "product_name": [],
                "category_required": False,
                "brand": None,
            },
        )
    )

    assert scores == {"product_found_correct": 1.0}


def test_a_failed_call_scores_zero_rather_than_crashing() -> None:
    scores = _by_name(
        evaluators.identification(
            input={},
            output=None,
            expected_output={
                "product_found": True,
                "product_name": ["air fryer"],
                "category_required": True,
                "brand": None,
            },
        )
    )

    assert scores["product_found_correct"] == 0.0


def test_expecting_a_product_but_finding_none_scores_the_missed_metrics_zero() -> None:
    """An early return here would drop the case out of every mean_of(...)
    but product_found_correct, so a prompt that refuses more often would be
    scored over an easier, smaller set of cases and look better for it."""
    scores = _by_name(
        evaluators.identification(
            input={"hint_text": "air fryer 5.5L"},
            output={"product_found": False},
            expected_output={
                "product_found": True,
                "product_name": ["air fryer"],
                "category_required": True,
                "brand": "Cardhu",
            },
        )
    )

    assert scores == {
        "product_found_correct": 0.0,
        "product_name_match": 0.0,
        "category_present": 0.0,
        "brand_correct": 0.0,
    }


def test_the_right_catalog_entry_scores_one() -> None:
    scores = _by_name(
        evaluators.catalog(
            input={}, output={"matched_title": "air fryer"}, expected_output={"expected_title": "air fryer"}
        )
    )

    assert scores == {"match_correct": 1.0}


def test_no_match_is_an_answer_in_its_own_right() -> None:
    right = _by_name(
        evaluators.catalog(input={}, output={"matched_title": None}, expected_output={"expected_title": None})
    )
    wrong = _by_name(
        evaluators.catalog(input={}, output={"matched_title": "picanha"}, expected_output={"expected_title": None})
    )

    assert right == {"match_correct": 1.0}
    assert wrong == {"match_correct": 0.0}


def test_market_recall_is_the_share_of_expected_markets_named() -> None:
    scores = _by_name(
        evaluators.markets(
            input={},
            output={"markets": ["USA", "BRA", "CHN"]},
            expected_output={"expected_markets": ["USA", "BRA", "GBR", "DEU"], "must_include": ["USA"]},
        )
    )

    assert scores["markets_recall"] == 0.5
    assert scores["must_include_hit"] == 1.0


def test_missing_a_must_include_market_is_caught_even_with_good_recall() -> None:
    scores = _by_name(
        evaluators.markets(
            input={},
            output={"markets": ["ARG", "CHL", "PER"]},
            expected_output={"expected_markets": ["ARG", "CHL", "PER", "BRA"], "must_include": ["BRA"]},
        )
    )

    assert scores["markets_recall"] == 0.75
    assert scores["must_include_hit"] == 0.0


def test_the_run_level_evaluator_averages_one_metric() -> None:
    class _Result:
        def __init__(self, evaluations):
            self.evaluations = evaluations

    from langfuse import Evaluation

    item_results = [
        _Result([Evaluation(name="match_correct", value=1.0)]),
        _Result([Evaluation(name="match_correct", value=0.0)]),
        _Result([Evaluation(name="other", value=9.0)]),
    ]

    mean = evaluators.mean_of("match_correct")(item_results=item_results)

    assert [(e.name, e.value) for e in mean] == [("match_correct_mean", 0.5)]


def test_an_apostrophe_does_not_split_a_name() -> None:
    assert evaluators.normalize("women's running shoes") == evaluators.normalize("womens running shoes")
    assert evaluators.normalize("Levi’s") == evaluators.normalize("levis")
    assert evaluators.normalize("Levi’s") == evaluators.normalize("Levi's")


def test_a_possessive_name_still_matches_its_accepted_spelling() -> None:
    scores = {
        e.name: e.value
        for e in evaluators.identification(
            input={},
            output={
                "product_found": True,
                "product_name": "women's running shoes",
                "category": "Footwear",
                "brand": None,
            },
            expected_output={
                "product_found": True,
                "product_name": ["womens running shoes"],
                "category_required": True,
                "brand": None,
            },
        )
    }

    assert scores["product_name_match"] == 1.0
