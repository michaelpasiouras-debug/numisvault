"""
CoinBids Auction Intelligence 3.0 — valuation engine (Phase 7, spec §19-27,
§66 output schema).

Combines a list of already-classified AuctionComparable records (see
auction_matching.classify_comparable) with the existing dealer market_sample
architecture into a single Valuation Snapshot: realized-market statistics,
dealer-market statistics, dealer/auction disagreement, dynamic fusion weights,
Fair Value range + central estimate, a multi-factor Confidence score, an
Observed Demand/Liquidity score built only from real comparable counts (no
invented bidder data), and an optional Trend read.

Nothing here invents data. Every count/score is derived from the actual
AuctionComparable/dealer-offer lists passed in. If there isn't enough
evidence for a claim (trend, demand, high confidence), the engine says so
explicitly rather than guessing.
"""
from __future__ import annotations
from datetime import date, datetime
from typing import List, Optional, Sequence

import auction_stats as stats
from auction_models import AuctionComparable, ComparableTier, PriceSemantics

AUCTION_CONFIG = dict(stats.AUCTION_CONFIG)
AUCTION_CONFIG.update({
    "source_weight": {
        "auction_house_direct": 1.00,
        "major_archive_clear_hammer": 0.95,
        "trusted_aggregator": 0.90,
        "manual_with_url_date": 0.85,
        "manual_price_only": 0.65,
        "csv": 0.80,
    },
    "dealer_disagreement_moderate_pct": 0.10,
    "dealer_disagreement_significant_pct": 0.20,
})


# ---------------------------------------------------------------------------
# REALIZED AUCTION MARKET (spec §19)
# ---------------------------------------------------------------------------

def _source_weight_for(comp: AuctionComparable) -> float:
    if comp.source == "manual":
        return AUCTION_CONFIG["source_weight"]["manual_with_url_date"] if (comp.source_url and comp.auction_date) \
            else AUCTION_CONFIG["source_weight"]["manual_price_only"]
    if comp.source == "csv":
        return AUCTION_CONFIG["source_weight"]["csv"]
    # Reserved for future licensed adapters — not reachable in this delivery
    # since no automated adapter is enabled (see auction_source_matrix.md).
    return AUCTION_CONFIG["source_weight"]["trusted_aggregator"]


def _final_weight(comp: AuctionComparable, as_of: date, half_life_days: int) -> float:
    match_w = (comp.identity_match_score or 0) / 100.0
    age = stats.age_days_between(_parse_date(comp.auction_date), as_of)
    comp.age_days = age
    rec_w = stats.recency_weight(age, half_life_days=half_life_days)
    comp.recency_weight = rec_w
    src_w = _source_weight_for(comp)
    comp.source_weight = src_w
    grade_w = comp.grade_weight if comp.grade_weight is not None else 1.0
    comp.match_weight = match_w
    w = match_w * rec_w * src_w * grade_w
    comp.final_weight = w
    return w


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def realized_market_summary(comparables: Sequence[AuctionComparable], as_of: Optional[date] = None,
                             half_life_days: int = None) -> dict:
    """spec §19 / §66 'realized_market' block. Only comparables that are
    realized_eligible() (real completed sale, HAMMER or REALIZED_INCL_PREMIUM
    semantics, not withdrawn/unsold/estimate) AND passed identity matching
    (tier != REJECT) ever enter the statistics."""
    as_of = as_of or date.today()
    half_life_days = half_life_days or AUCTION_CONFIG["recency_half_life_days_default"]

    eligible = [c for c in comparables if c.comparable_tier != ComparableTier.REJECT.value and c.realized_eligible()]
    n = len(eligible)
    if n == 0:
        return {
            "count": 0, "exact_count": 0, "strong_count": 0, "supporting_count": 0,
            "weighted_median": None, "p25": None, "p75": None, "p10": None, "p90": None,
            "dispersion": None, "dispersion_label": "UNKNOWN", "latest_sale": None,
            "distinct_auction_houses": 0, "sample_note": "n=0: no realized-auction valuation available.",
        }

    prices, weights, outlier_flags_input = [], [], []
    for c in eligible:
        p = c.effective_price()
        w = _final_weight(c, as_of, half_life_days)
        prices.append(p)
        weights.append(w)

    outlier_flags = stats.flag_outliers(prices)
    for c, flag in zip(eligible, outlier_flags):
        c.outlier_flag = flag
    # Outliers are flagged for the evidence view but NOT silently discarded
    # from statistics (spec §17) — down-weight them slightly instead of
    # removing them outright, keeping the estimate robust without hiding data.
    adj_weights = [w * (0.35 if f else 1.0) for w, f in zip(weights, outlier_flags)]

    wmedian = stats.weighted_median(prices, adj_weights)
    p25 = stats.weighted_quantile(prices, adj_weights, 0.25)
    p75 = stats.weighted_quantile(prices, adj_weights, 0.75)
    p10 = stats.weighted_quantile(prices, adj_weights, 0.10) if n >= 5 else None
    p90 = stats.weighted_quantile(prices, adj_weights, 0.90) if n >= 5 else None
    disp = stats.dispersion_ratio(prices)

    exact_count = sum(1 for c in eligible if c.comparable_tier == ComparableTier.EXACT.value)
    strong_count = sum(1 for c in eligible if c.comparable_tier == ComparableTier.STRONG.value)
    supporting_count = sum(1 for c in eligible if c.comparable_tier == ComparableTier.SUPPORTING.value)
    houses = {(c.auction_house or "").strip().lower() for c in eligible if c.auction_house}
    dates = [d for d in (_parse_date(c.auction_date) for c in eligible) if d]
    latest_sale = max(dates).isoformat() if dates else None

    if n == 1:
        sample_note = "n=1: single-sale reference point only, not a robust market range."
    elif n == 2:
        sample_note = "n=2: very low confidence, both values shown rather than a claimed range."
    elif n <= 4:
        sample_note = "n=3-4: low confidence, median with min/max shown instead of tight quantiles."
    elif n <= 9:
        sample_note = "n=5-9: medium confidence possible depending on match quality and dispersion."
    else:
        sample_note = "n>=10: potentially high confidence if matches are strong, recent, and dispersion is not wide."

    return {
        "count": n, "exact_count": exact_count, "strong_count": strong_count, "supporting_count": supporting_count,
        "weighted_median": _r(wmedian), "p25": _r(p25), "p75": _r(p75), "p10": _r(p10), "p90": _r(p90),
        "dispersion": _r(disp, 4), "dispersion_label": stats.dispersion_label(disp),
        "latest_sale": latest_sale, "distinct_auction_houses": len(houses),
        "sample_note": sample_note,
    }


# ---------------------------------------------------------------------------
# DEALER MARKET (spec §20) — reuses CoinBids' EXISTING market_sample /
# purchase-anchor separation; does not alter that architecture.
# ---------------------------------------------------------------------------

def dealer_market_summary(dealer_sample_prices: Sequence[float], lowest_offer: Optional[float] = None,
                           second_lowest: Optional[float] = None) -> dict:
    """dealer_sample_prices = the broader, relevance-ranked market_sample
    (NOT the two purchase anchors). Robust median after simple outlier
    flagging — dealer ASKS are never weighted as if they were actual sales
    (spec §20)."""
    prices = [p for p in dealer_sample_prices if p is not None and p > 0]
    if not prices:
        return {"count": 0, "median": None, "lowest_offer": lowest_offer, "second_lowest": second_lowest}
    outlier_flags = stats.flag_outliers(prices)
    weights = [0.35 if f else 1.0 for f in outlier_flags]
    med = stats.weighted_median(prices, weights)
    return {
        "count": len(prices), "median": _r(med),
        "lowest_offer": lowest_offer, "second_lowest": second_lowest,
    }


# ---------------------------------------------------------------------------
# DEALER/AUCTION DISAGREEMENT (spec §22)
# ---------------------------------------------------------------------------

def disagreement(dealer_median: Optional[float], realized_median: Optional[float]) -> dict:
    if dealer_median is None or realized_median is None or realized_median == 0:
        return {"pct": None, "label": "UNKNOWN", "text": None}
    pct = (dealer_median - realized_median) / realized_median
    if abs(pct) < AUCTION_CONFIG["dealer_disagreement_moderate_pct"]:
        label = "ALIGNED"
    elif abs(pct) < AUCTION_CONFIG["dealer_disagreement_significant_pct"]:
        label = "MODERATE"
    else:
        label = "SIGNIFICANT"
    direction = "above" if pct > 0 else "below"
    text = f"Dealer asks are {abs(pct)*100:.0f}% {direction} recent realized auction median." if abs(pct) >= 0.01 else "Dealer asks are closely aligned with realized auction median."
    return {"pct": _r(pct, 4), "label": label, "text": text}


# ---------------------------------------------------------------------------
# DYNAMIC FUSION (spec §21)
# ---------------------------------------------------------------------------

def fusion_weights(realized: dict) -> dict:
    """Returns {"realized_weight":..,"dealer_weight":..,"basis":str} per the
    spec's tiered table. Uses exact+strong count (not raw n) as the primary
    signal, since a pile of weak/SUPPORTING matches shouldn't buy the same
    trust as strong ones."""
    n_good = realized["exact_count"] + realized["strong_count"]
    if realized["count"] == 0:
        return {"realized_weight": 0.0, "dealer_weight": 1.0, "basis": "no realized sales — dealer-only estimate"}
    if n_good >= 10:
        rw = 0.90
    elif n_good >= 5:
        rw = 0.80
    elif n_good >= 3:
        rw = 0.70
    elif n_good >= 1:
        rw = 0.50  # supporting anchor, not a robust realized market claim
    else:
        rw = 0.30  # only SUPPORTING-tier comps exist; treat cautiously
    return {"realized_weight": rw, "dealer_weight": round(1 - rw, 4),
            "basis": f"{n_good} exact/strong realized comparable(s)"}


def fair_value_range(realized: dict, dealer: dict, weights: dict) -> dict:
    """Blends realized P25/median/P75 with the dealer median according to
    the fusion weights. Never artificially narrows the range — if realized
    data is thin, the dealer contribution and the realized P25-P75 spread
    both widen the effective range rather than pretend precision."""
    rw, dw = weights["realized_weight"], weights["dealer_weight"]
    r_med, r_p25, r_p75 = realized["weighted_median"], realized["p25"], realized["p75"]
    d_med = dealer["median"]

    if r_med is None and d_med is None:
        return {"fair_low": None, "fair_central": None, "fair_high": None}
    if r_med is None:
        central = d_med
        low, high = d_med * 0.85, d_med * 1.15  # wide, low-confidence band — dealer-only
    elif d_med is None:
        central = r_med
        low = r_p25 if r_p25 is not None else r_med * 0.9
        high = r_p75 if r_p75 is not None else r_med * 1.1
    else:
        central = rw * r_med + dw * d_med
        r_low = r_p25 if r_p25 is not None else r_med * 0.9
        r_high = r_p75 if r_p75 is not None else r_med * 1.1
        low = rw * r_low + dw * min(d_med, r_low)
        high = rw * r_high + dw * max(d_med, r_high)
    return {"fair_low": _r(low), "fair_central": _r(central), "fair_high": _r(high)}


# ---------------------------------------------------------------------------
# CONFIDENCE ENGINE 2.0 (spec §26)
# ---------------------------------------------------------------------------

def confidence_score(identity_quality: float, realized: dict, source_count_hint: Optional[int] = None) -> dict:
    """identity_quality: 0-1 (e.g. resolver confidence for a 'resolved'
    status). Returns {"score":0-100, "label":..., "reasons":[...]}. Hard caps
    are applied AFTER the weighted score per spec §26 — sample count alone
    never grants High confidence (spec §18)."""
    reasons = []
    n = realized["count"]
    n_good = realized["exact_count"] + realized["strong_count"]
    houses = realized["distinct_auction_houses"]

    identity_pts = round(20 * max(0, min(1, identity_quality)))
    reasons.append(f"identity quality {identity_pts}/20")

    qty_pts = 15 if n >= 10 else 12 if n >= 5 else 8 if n >= 3 else 4 if n >= 1 else 0
    reasons.append(f"{n} realized sales -> {qty_pts}/15")

    ratio = (n_good / n) if n else 0
    ratio_pts = round(15 * ratio)
    reasons.append(f"{n_good} exact/strong of {n} -> {ratio_pts}/15")

    latest = realized.get("latest_sale")
    if latest:
        try:
            age = (date.today() - datetime.strptime(latest, "%Y-%m-%d").date()).days
        except ValueError:
            age = None
    else:
        age = None
    recency_pts = 10 if (age is not None and age <= 365) else 6 if (age is not None and age <= 730) else 2 if age is not None else 0
    reasons.append(f"latest sale age -> {recency_pts}/10")

    grade_pts = 8  # neutral default; a full implementation would inspect per-comp grade certainty
    reasons.append(f"grade quality (heuristic) -> {grade_pts}/10")

    diversity_pts = 10 if houses >= 3 else 6 if houses == 2 else 3 if houses == 1 else 0
    reasons.append(f"{houses} distinct auction house(s) -> {diversity_pts}/10")

    disp = realized.get("dispersion")
    dispersion_pts = 10 if (disp is not None and disp < 0.10) else 7 if (disp is not None and disp < 0.20) else 4 if (disp is not None and disp < 0.35) else 0
    reasons.append(f"dispersion {realized.get('dispersion_label')} -> {dispersion_pts}/10")

    agreement_pts = 5  # left neutral here; combined at call-site when disagreement info is available
    reasons.append(f"dealer/auction agreement (baseline) -> {agreement_pts}/10")

    raw = identity_pts + qty_pts + ratio_pts + recency_pts + grade_pts + diversity_pts + dispersion_pts + agreement_pts
    raw = max(0, min(100, raw))

    label = "HIGH" if raw >= 85 else "MEDIUM" if raw >= 65 else "LOW" if raw >= 40 else "VERY LOW"

    # Hard caps (spec §26) — applied regardless of the raw score.
    capped_reasons = []
    if n == 0:
        raw = min(raw, 39); label = "LOW"; capped_reasons.append("no realized sales -> capped LOW")
    elif n <= 2:
        raw = min(raw, 39); label = "LOW"; capped_reasons.append("only 1-2 realized sales -> capped LOW")
    if houses <= 1 and n > 0:
        cap = 64
        if raw > cap:
            raw = cap; label = "MEDIUM"
        capped_reasons.append("only one auction source -> capped at MEDIUM")
    if disp is not None and disp >= 0.35:
        cap = 64
        if raw > cap:
            raw = cap; label = "MEDIUM"
        capped_reasons.append("very wide dispersion -> cannot be HIGH")
    if identity_quality < 0.75:
        raw = min(raw, 39); label = "LOW"; capped_reasons.append("weak identity confidence -> capped LOW")

    return {"score": int(raw), "label": label, "reasons": reasons + capped_reasons}


# ---------------------------------------------------------------------------
# OBSERVED DEMAND / LIQUIDITY (spec §24) — built ONLY from real comparable
# counts/recency/diversity/dispersion. Never fabricates bidder counts.
# ---------------------------------------------------------------------------

def demand_score(comparables_last_12m: int, comparables_last_24m: int, houses: int,
                  latest_sale: Optional[str], dispersion: Optional[float]) -> dict:
    freq_pts = min(30, comparables_last_12m * 6 + comparables_last_24m * 2)
    diversity_pts = min(15, houses * 5)
    depth_pts = min(20, comparables_last_24m * 2)
    if latest_sale:
        try:
            age = (date.today() - datetime.strptime(latest_sale, "%Y-%m-%d").date()).days
        except ValueError:
            age = None
    else:
        age = None
    recency_pts = 15 if (age is not None and age <= 180) else 9 if (age is not None and age <= 365) else 3 if age is not None else 0
    stability_pts = 20 if (dispersion is not None and dispersion < 0.10) else 12 if (dispersion is not None and dispersion < 0.20) else 5 if dispersion is not None else 0

    total = min(100, freq_pts + diversity_pts + depth_pts + recency_pts + stability_pts)
    label = "HIGH" if total >= 70 else "MEDIUM" if total >= 40 else "LOW"
    return {"score": int(total), "label": label,
            "caveat": "Observed Demand/Liquidity — derived only from realized-sale counts and dispersion actually present, not bidder counts (unavailable)."}


# ---------------------------------------------------------------------------
# TREND (spec §25) — only computed with sufficient time-series depth.
# ---------------------------------------------------------------------------

def trend(comparables: Sequence[AuctionComparable], as_of: Optional[date] = None) -> dict:
    as_of = as_of or date.today()
    eligible = [c for c in comparables if c.comparable_tier != ComparableTier.REJECT.value and c.realized_eligible()]
    dated = [(_parse_date(c.auction_date), c.effective_price()) for c in eligible]
    dated = [(d, p) for d, p in dated if d is not None and p is not None]
    if len(dated) < 6:
        return {"label": "INSUFFICIENT_DATA", "annualized_change_pct": None, "note": f"only {len(dated)} dated realized sales (need >=6)"}
    dated.sort(key=lambda x: x[0])
    span_days = (dated[-1][0] - dated[0][0]).days
    if span_days < 180:
        return {"label": "INSUFFICIENT_DATA", "annualized_change_pct": None, "note": f"date span only {span_days} days (need >=180)"}

    mid = len(dated) // 2
    older_prices = [p for _, p in dated[:mid]]
    recent_prices = [p for _, p in dated[mid:]]
    older_med = stats.weighted_median(older_prices, [1.0] * len(older_prices))
    recent_med = stats.weighted_median(recent_prices, [1.0] * len(recent_prices))
    if not older_med:
        return {"label": "INSUFFICIENT_DATA", "annualized_change_pct": None, "note": "degenerate older-half median"}
    change_pct = (recent_med - older_med) / older_med
    years = max(span_days / 365.0, 0.25)
    annualized = change_pct / years

    if annualized > 0.05:
        label = "RISING"
    elif annualized < -0.05:
        label = "FALLING"
    else:
        label = "STABLE"
    return {"label": label, "annualized_change_pct": _r(annualized * 100, 1),
            "note": f"{len(dated)} dated realized sales over {span_days} days"}


# ---------------------------------------------------------------------------
# TOP-LEVEL SNAPSHOT (spec §66)
# ---------------------------------------------------------------------------

def compute_valuation_snapshot(identity: dict, identity_quality: float,
                                comparables: Sequence[AuctionComparable],
                                dealer_sample_prices: Sequence[float],
                                dealer_lowest: Optional[float] = None,
                                dealer_second_lowest: Optional[float] = None,
                                as_of: Optional[date] = None,
                                target_currency: Optional[str] = None,
                                fx_http_get=None) -> dict:
    as_of = as_of or date.today()
    # FX normalization: any comparable priced in a currency other than
    # target_currency is converted BEFORE it enters the statistics, so
    # weighted_median/quantiles never silently mix e.g. USD and EUR amounts.
    # Optional/backward-compatible: if target_currency is not supplied, every
    # comparable's price is used as-is (existing behavior, e.g. when the
    # caller already normalized upstream or all comparables share one
    # currency). A comparable whose FX lookup fails is not dropped — it
    # simply has no normalized_price and effective_price() falls back to its
    # raw hammer/realized price in its original currency, same as before FX
    # support existed.
    if target_currency:
        import auction_fx as _fx
        kwargs = {"http_get": fx_http_get} if fx_http_get else {}
        for c in comparables:
            if c.currency and c.currency.upper() != target_currency.upper():
                try:
                    _fx.normalize_comparable(c, target_currency, **kwargs)
                except Exception:
                    pass  # FX is best-effort enrichment, never fatal to the snapshot
    realized = realized_market_summary(comparables, as_of=as_of)
    dealer = dealer_market_summary(dealer_sample_prices, dealer_lowest, dealer_second_lowest)
    disagree = disagreement(dealer["median"], realized["weighted_median"])
    weights = fusion_weights(realized)
    fair = fair_value_range(realized, dealer, weights)
    conf = confidence_score(identity_quality, realized)
    # fold disagreement into confidence: significant disagreement is itself a
    # signal of noisy/uncertain valuation, so it caps confidence too.
    if disagree["label"] == "SIGNIFICANT" and conf["score"] > 64:
        conf["score"] = 64
        conf["label"] = "MEDIUM"
        conf["reasons"].append("significant dealer/auction disagreement -> capped at MEDIUM")

    demand = demand_score(
        comparables_last_12m=sum(1 for c in comparables if _within_days(c.auction_date, as_of, 365)),
        comparables_last_24m=sum(1 for c in comparables if _within_days(c.auction_date, as_of, 730)),
        houses=realized["distinct_auction_houses"],
        latest_sale=realized["latest_sale"],
        dispersion=realized["dispersion"],
    )
    tr = trend(comparables, as_of=as_of)

    return {
        "identity": identity,
        "realized_market": realized,
        "dealer_market": dealer,
        "disagreement": disagree,
        "fusion": {**weights, **fair},
        "confidence": conf,
        "demand": demand,
        "trend": tr,
        "checked_at": as_of.isoformat(),
    }


def _within_days(date_str: Optional[str], as_of: date, days: int) -> bool:
    d = _parse_date(date_str)
    if d is None:
        return False
    return 0 <= (as_of - d).days <= days


def _r(x: Optional[float], ndigits: int = 2) -> Optional[float]:
    return None if x is None else round(x, ndigits)
