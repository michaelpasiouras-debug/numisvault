"""
CoinBids Auction Intelligence 3.0 — statistics engine (Phase 6).

Pure functions, no I/O, no external data. Every function here is unit-tested
against hand-computable synthetic numbers (see test_auction_intelligence.py)
because a wrong weighted-quantile or MAD implementation would silently
mis-price real coins.
"""
from __future__ import annotations
import math
from datetime import date
from typing import List, Optional, Sequence, Tuple

AUCTION_CONFIG = {
    "recency_half_life_days_default": 730,      # 2 years
    "recency_half_life_days_liquid_modern": 450,  # ~15 months
    "recency_half_life_days_rare_historical": 1460,  # 4 years
    "mad_outlier_z_threshold": 3.5,
    "mad_min_sample_for_outlier_check": 5,
}


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> Optional[float]:
    """Correct weighted median via the standard weighted-quantile definition
    at p=0.5 (spec §19: 'implement correct weighted quantile helper, do not
    approximate by duplicating observations')."""
    return weighted_quantile(values, weights, 0.5)


def weighted_quantile(values: Sequence[float], weights: Sequence[float], p: float) -> Optional[float]:
    """Weighted quantile using linear interpolation on the weighted empirical
    CDF. p in [0,1]. Returns None for an empty input (spec §18 n=0 case) and
    never divides by zero (spec §58)."""
    if not values:
        return None
    if len(values) != len(weights):
        raise ValueError("values and weights must be the same length")
    pairs = sorted(zip(values, weights), key=lambda x: x[0])
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 0:
        # No usable weight information — fall back to an unweighted quantile
        # rather than raising or silently returning a wrong number.
        pairs = [(v, 1.0) for v, _ in pairs]
        total_weight = float(len(pairs))
    if len(pairs) == 1:
        return pairs[0][0]
    # Cumulative weight at the midpoint of each item's weight slice, matching
    # the common "Hazen"-style weighted-quantile definition.
    cum = 0.0
    cdf_points: List[Tuple[float, float]] = []
    for v, w in pairs:
        cum += w
        cdf_points.append((v, (cum - w / 2.0) / total_weight))
    target = p
    if target <= cdf_points[0][1]:
        return cdf_points[0][0]
    if target >= cdf_points[-1][1]:
        return cdf_points[-1][0]
    for i in range(len(cdf_points) - 1):
        v0, c0 = cdf_points[i]
        v1, c1 = cdf_points[i + 1]
        if c0 <= target <= c1:
            if c1 == c0:
                return v0
            frac = (target - c0) / (c1 - c0)
            return v0 + frac * (v1 - v0)
    return cdf_points[-1][0]  # pragma: no cover — unreachable given the bounds above


def median_absolute_deviation(values: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    """Returns (median, MAD). MAD = median(|x_i - median(x)|). Spec §17."""
    if not values:
        return None, None
    vs = sorted(values)
    med = weighted_quantile(vs, [1.0] * len(vs), 0.5)
    abs_devs = sorted(abs(v - med) for v in vs)
    mad = weighted_quantile(abs_devs, [1.0] * len(abs_devs), 0.5)
    return med, mad


def robust_z_scores(values: Sequence[float]) -> List[Optional[float]]:
    """0.6745 * (x_i - median) / MAD per value, in input order. Returns None
    per-element when MAD is 0 or undefined (degenerate sample — never divide
    by zero)."""
    med, mad = median_absolute_deviation(values)
    if med is None:
        return [None] * len(values)
    if not mad or mad <= 0:
        return [0.0 for _ in values]
    return [0.6745 * (v - med) / mad for v in values]


def flag_outliers(values: Sequence[float], threshold: float = None) -> List[Optional[str]]:
    """Returns a list parallel to `values`: None, "OUTLIER_LOW" or
    "OUTLIER_HIGH" per spec §17. Never used to silently discard data — only
    to flag it; the caller decides whether to exclude flagged points from a
    given statistic while still showing them in the evidence view. Below the
    minimum sample size for a meaningful MAD (spec default n>=5), nothing is
    flagged — a tiny sample doesn't have enough information to call anything
    an outlier."""
    threshold = AUCTION_CONFIG["mad_outlier_z_threshold"] if threshold is None else threshold
    if len(values) < AUCTION_CONFIG["mad_min_sample_for_outlier_check"]:
        return [None] * len(values)
    zs = robust_z_scores(values)
    out = []
    for z in zs:
        if z is None:
            out.append(None)
        elif z > threshold:
            out.append("OUTLIER_HIGH")
        elif z < -threshold:
            out.append("OUTLIER_LOW")
        else:
            out.append(None)
    return out


def recency_weight(age_days: Optional[int], half_life_days: int = None) -> float:
    """Exponential decay: 0.5 ** (age_days / half_life_days). Spec §11 — a
    sale is never hard-discarded for being old, only down-weighted. Missing
    age is treated as maximally stale (weight -> a small floor) rather than
    silently full-weight, since we cannot verify recency for it."""
    half_life_days = AUCTION_CONFIG["recency_half_life_days_default"] if half_life_days is None else half_life_days
    if age_days is None:
        return 0.05
    if age_days < 0:
        age_days = 0
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def age_days_between(sale_date: Optional[date], as_of: Optional[date]) -> Optional[int]:
    if sale_date is None or as_of is None:
        return None
    return max(0, (as_of - sale_date).days)


def dispersion_ratio(values: Sequence[float]) -> Optional[float]:
    """IQR / median as a simple, robust dispersion metric (spec §23).
    Returns None when fewer than 2 values or median is 0 (undefined ratio)."""
    if len(values) < 2:
        return None
    p25 = weighted_quantile(values, [1.0] * len(values), 0.25)
    p75 = weighted_quantile(values, [1.0] * len(values), 0.75)
    med = weighted_quantile(values, [1.0] * len(values), 0.5)
    if med is None or med == 0 or p25 is None or p75 is None:
        return None
    return (p75 - p25) / med


def dispersion_label(ratio: Optional[float]) -> str:
    if ratio is None:
        return "UNKNOWN"
    if ratio < 0.10:
        return "TIGHT"
    if ratio < 0.20:
        return "MODERATE"
    if ratio < 0.35:
        return "WIDE"
    return "VERY_WIDE"
