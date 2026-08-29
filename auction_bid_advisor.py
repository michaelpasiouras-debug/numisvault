"""
CoinBids Auction Intelligence 3.0 — live bid advisor (Phase 8, spec §28-32,
§67 output schema).

Two-step design, exactly matching spec §32 ("Live bid changes don't rerun
research"):
  1. build_valuation_snapshot() is expensive (calls the valuation engine over
     the comparable/dealer data) and is cached/reused across bid changes.
  2. bid_advice(snapshot, current_hammer, ...) is a cheap, pure, local
     recomputation — safe to call on every keystroke without re-running any
     search.
"""
from __future__ import annotations
from typing import Optional

AUCTION_CONFIG = {
    "margin_of_safety_base": {
        "HIGH": (0.10, 0.15),
        "MEDIUM": (0.15, 0.22),
        "LOW": (0.22, 0.30),
        "VERY LOW": (0.30, 0.40),
    },
    "collector_premium_by_confidence": {
        "HIGH": 1.08, "MEDIUM": 1.10, "LOW": 1.15, "VERY LOW": 1.20,
    },
    "strong_buy_fraction_of_margin": 0.5,  # "materially below" value-buy threshold
}


def dynamic_margin_of_safety(confidence_label: str, dispersion_label: Optional[str],
                              disagreement_label: Optional[str], houses: int) -> float:
    """spec §30: base range by confidence, then adjusted up for risk factors
    (wide dispersion, dealer/auction disagreement, single-source
    concentration) or down slightly for strong evidence."""
    lo, hi = AUCTION_CONFIG["margin_of_safety_base"].get(confidence_label, (0.22, 0.30))
    margin = (lo + hi) / 2.0

    risk_ups = 0
    if dispersion_label in ("WIDE", "VERY_WIDE"):
        risk_ups += 1
    if disagreement_label == "SIGNIFICANT":
        risk_ups += 1
    if houses <= 1:
        risk_ups += 1
    margin += risk_ups * 0.03
    margin = min(margin, hi + 0.06)  # don't blow past the band by too much

    return round(margin, 4)


def compute_ceilings(fair_low: Optional[float], fair_central: Optional[float], fair_high: Optional[float],
                      confidence_label: str, dispersion_label: Optional[str] = None,
                      disagreement_label: Optional[str] = None, houses: int = 0) -> dict:
    """spec §29. Returns ALL-IN budgets first (value_buy_max_all_in,
    fair_buy_max_all_in, collector_max_all_in) — hammer ceilings are derived
    from these later, once buyer premium/shipping/fees are known, by
    bid_advice()."""
    if fair_central is None:
        return {"value_buy_max_all_in": None, "fair_buy_max_all_in": None, "collector_max_all_in": None,
                "margin_of_safety": None}

    margin = dynamic_margin_of_safety(confidence_label, dispersion_label, disagreement_label, houses)
    conservative = fair_low if fair_low is not None else fair_central * (1 - margin)
    value_buy_all_in = conservative * (1 - margin)
    fair_buy_all_in = fair_central
    collector_premium = AUCTION_CONFIG["collector_premium_by_confidence"].get(confidence_label, 1.15)
    upper = fair_high if fair_high is not None else fair_central * (1 + margin)
    collector_max_all_in = upper * collector_premium

    return {
        "value_buy_max_all_in": _r(value_buy_all_in),
        "fair_buy_max_all_in": _r(fair_buy_all_in),
        "collector_max_all_in": _r(collector_max_all_in),
        "margin_of_safety": margin,
    }


def max_all_in_to_hammer(max_all_in: Optional[float], shipping: float, fixed_fees: float,
                          taxes: float, buyer_premium_pct: float) -> Optional[float]:
    """spec §29: inverse hammer from an all-in budget. Never confuse this
    with the all-in figure itself."""
    if max_all_in is None:
        return None
    budget_for_hammer_plus_premium = max_all_in - shipping - fixed_fees - taxes
    if budget_for_hammer_plus_premium <= 0:
        return 0.0
    return budget_for_hammer_plus_premium / (1 + buyer_premium_pct / 100.0)


def current_all_in(current_hammer: float, buyer_premium_pct: float, shipping: float,
                    fixed_fees: float, taxes: float) -> float:
    return current_hammer * (1 + buyer_premium_pct / 100.0) + shipping + fixed_fees + taxes


def recommend(all_in: float, ceilings: dict) -> dict:
    """spec §31 recommendation state machine, using ALL-IN comparisons."""
    value_max = ceilings.get("value_buy_max_all_in")
    fair_max = ceilings.get("fair_buy_max_all_in")
    collector_max = ceilings.get("collector_max_all_in")

    if value_max is None or fair_max is None or collector_max is None:
        return {"status": "UNKNOWN", "reason": "Insufficient fair-value evidence to compute a recommendation."}

    strong_buy_cut = value_max * (1 - AUCTION_CONFIG["strong_buy_fraction_of_margin"] * 0.2)
    if all_in <= strong_buy_cut:
        status = "STRONG BUY"
        pct = ((value_max - all_in) / value_max) * 100 if (value_max and value_max > 0) else 0
        reason = f"Current all-in is materially ({pct:.0f}%) below the calibrated Value Buy ceiling." if pct > 0 else "Current all-in is exceptionally below the calibrated Value Buy ceiling."
    elif all_in <= value_max:
        status = "BUY"
        pct = (1 - all_in / fair_max) * 100 if fair_max else 0
        reason = f"Current all-in is {pct:.0f}% below central Fair Value and at/under the Value Buy ceiling."
    elif all_in <= fair_max:
        status = "FAIR"
        reason = "Current all-in sits between the Value Buy and Fair Buy ceilings — a fair, not exceptional, price."
    elif all_in <= collector_max:
        status = "CAUTION"
        reason = "Current all-in is above central Fair Value but still within a defensible collector premium ceiling."
    elif all_in <= collector_max * 1.15:
        status = "STOP"
        reason = "Current all-in exceeds the Collector Max ceiling — this is not a defensible price on current evidence."
    else:
        status = "OVERPAY"
        reason = "Current all-in is materially above even the upper fair-value/collector ceiling."
    return {"status": status, "reason": reason}


def bid_advice(snapshot: dict, current_hammer_raw: str, buyer_premium_pct: float,
               shipping: float = 0.0, fixed_fees: float = 0.0, taxes: float = 0.0) -> dict:
    """Cheap, local recompute from an already-built valuation snapshot — spec
    §32/§62. Buy mode requires a real, valid positive current_hammer (spec
    §28); an empty/invalid/negative/zero bid yields no verdict, matching the
    same rule already enforced in CoinBids' existing Auction Intelligence
    frontend for the current UI, extended here to the new engine."""
    raw = (current_hammer_raw or "").strip()
    try:
        current_hammer = float(raw.replace(",", "."))
    except (TypeError, ValueError):
        current_hammer = None
    bid_valid = raw != "" and current_hammer is not None and current_hammer > 0

    fusion = snapshot.get("fusion", {})
    confidence = snapshot.get("confidence", {})
    realized = snapshot.get("realized_market", {})

    ceilings = compute_ceilings(
        fusion.get("fair_low"), fusion.get("fair_central"), fusion.get("fair_high"),
        confidence.get("label", "LOW"),
        dispersion_label=realized.get("dispersion_label"),
        disagreement_label=snapshot.get("disagreement", {}).get("label"),
        houses=realized.get("distinct_auction_houses", 0),
    )

    result = {
        "current_hammer": current_hammer if bid_valid else None,
        "buyer_premium_pct": buyer_premium_pct, "shipping": shipping, "fees": fixed_fees, "taxes": taxes,
        "current_all_in": None,
        "value_buy_max_all_in": ceilings.get("value_buy_max_all_in"),
        "fair_buy_max_all_in": ceilings.get("fair_buy_max_all_in"),
        "collector_max_all_in": ceilings.get("collector_max_all_in"),
        "value_buy_max_hammer": max_all_in_to_hammer(ceilings.get("value_buy_max_all_in"), shipping, fixed_fees, taxes, buyer_premium_pct),
        "fair_buy_max_hammer": max_all_in_to_hammer(ceilings.get("fair_buy_max_all_in"), shipping, fixed_fees, taxes, buyer_premium_pct),
        "collector_max_hammer": max_all_in_to_hammer(ceilings.get("collector_max_all_in"), shipping, fixed_fees, taxes, buyer_premium_pct),
        "margin_of_safety": ceilings.get("margin_of_safety"),
        "recommendation": None,
        "reason": None,
    }

    if not bid_valid:
        result["recommendation"] = None
        result["reason"] = "Enter the current hammer bid before calculating a buy/stop recommendation."
        return result

    all_in = current_all_in(current_hammer, buyer_premium_pct, shipping, fixed_fees, taxes)
    result["current_all_in"] = _r(all_in)
    rec = recommend(all_in, ceilings)
    result["recommendation"] = rec["status"]
    result["reason"] = rec["reason"]
    return result


def _r(x, ndigits=2):
    return None if x is None else round(x, ndigits)