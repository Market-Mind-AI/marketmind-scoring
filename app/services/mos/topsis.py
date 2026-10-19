"""Cap. 5 §5.6 — TOPSIS with fixed ideal/anti-ideal (correction over the
literal text's "best/worst observed among compared alternatives" — see
memory marketmind_mos_engine). Since dimension sub-scores are already
bounded 0-100 via Cap. 4's reference-population normalization, the ideal
market is always 100 on every dimension and the anti-ideal always 0 —
comparable across independent runs, not just within one query's country
set. This also means the classical TOPSIS vector-normalization step
(dividing by the Euclidean norm across compared alternatives) is skipped
entirely: it exists to make differently-scaled raw criteria comparable,
which Cap. 4's normalization already guarantees. Only the weighted-distance
step remains."""

import math

from app.domain.enums import IndicatorDimension


def topsis_score(scores: dict[IndicatorDimension, float], weights: dict[IndicatorDimension, float]) -> float:
    """scores/weights keyed by the same 7 dimensions (Cap. 5 §5.5).
    Returns the TOPSIS relative-closeness coefficient in [0, 1] — MOS is
    this multiplied by 100 (Cap. 5 §5.7)."""
    d_plus_sq = 0.0
    d_minus_sq = 0.0
    for dimension, weight in weights.items():
        value = scores[dimension]
        d_plus_sq += weight * (100 - value) ** 2
        d_minus_sq += weight * value**2

    d_plus = math.sqrt(d_plus_sq)
    d_minus = math.sqrt(d_minus_sq)
    if d_plus + d_minus == 0:
        return 0.5  # degenerate: every weighted dimension sits exactly midway
    return d_minus / (d_plus + d_minus)
