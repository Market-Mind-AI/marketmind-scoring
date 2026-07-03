"""Cross-validation only — NOT used to decide any product's actual MOS.
Runs the classical (textbook) Entropy Weight + TOPSIS from the `pymcdm`
library against the same population matrix our custom implementation uses,
so the two can be compared manually (see docs/marketmind-capitulo5-mos.md
and memory marketmind_mos_engine for why production deviates: fixed
ideal=100/anti-ideal=0 instead of the best/worst observed among compared
alternatives, and no per-query vector renormalization since Cap. 4 already
normalizes every indicator against a fixed reference population).

pymcdm's TOPSIS derives ideal/anti-ideal FROM the given matrix (classical
behavior) and applies its own min-max renormalization internally — this is
intentional here, it's exactly the textbook behavior we want to contrast
against, not a bug.
"""

import numpy as np
from pymcdm.methods import TOPSIS
from pymcdm.weights import entropy_weights as pymcdm_entropy_weights


def classical_topsis_scores(matrix: list[list[float]]) -> list[float]:
    """matrix: rows = alternatives (countries), columns = the 5 dimensions,
    same column order for every row. Needs at least 2 rows — same
    mathematical requirement as our own entropy_weight.py."""
    if len(matrix) < 2:
        raise ValueError("classical TOPSIS needs at least 2 alternatives in the comparison population")

    np_matrix = np.array(matrix, dtype=float)
    weights = pymcdm_entropy_weights(np_matrix)
    types = np.ones(np_matrix.shape[1])
    topsis = TOPSIS()
    scores = topsis(np_matrix, weights, types)
    return [float(s) for s in scores]
