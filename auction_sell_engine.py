"""
CoinBids Auction Intelligence 3.0 — Sell Engine (Phase 9).

Helps a seller decide a consignment estimate, reserve, and starting bid, and
shows realistic net proceeds after commission/fees — built on the exact same
Valuation Snapshot as the buy-side (auction_valuation.py), so a coin's sell
guidance is always internally consistent with its buy guidance.

Nothing here invents an auction house's actual commission schedule — the
caller supplies real percentages/fees; this module only does the arithmetic
and the range/reserve logic honestly.
"""
from __future__ import annotations
from typing import Optional

AUCTION_CONFIG = {
    # Conventional relationship between reserve and low estimate at most
    # auction houses — reserve is typically set at or below the low estimate,
    # never above it (a reserve above the low estimate risks an unsold lot).
    "reserve_as_fraction_of_low_estimate": {
        "HIGH": 0.95, "MEDIUM": 0.85, "LOW": 0.75, "VERY LOW": 0.65,
    },
    "starting_bid_as_fraction_of_reserve": 0.60,
}


def estimate_range(fair_low: Optional[float], fair_central: Optional[float], fair_high: Optional[float],
                    confidence_label: str) -> dict:
    """Pre-sale estimate range for consignment. Widens slightly beyond the
    buy-side Fair Value range for lower confidence, since a seller's estimate
    conventionally leaves more room than a buyer's ceiling."""
    if fair_central is None:
        return {"estimate_low": None, "estimate_central": None, "estimate_high": None,
                "note": "Insufficient fair-value evidence to propose a consignment estimate."}

    widen = {"HIGH": 1.00, "MEDIUM": 1.08, "LOW": 1.18, "VERY LOW": 1.30}.get(confidence_label, 1.20)
    low = fair_low if fair_low is not None else fair_central * 0.85
    high = fair_high if fair_high is not None else fair_central * 1.15
    center = (low + high) / 2.0
    half_span = (high - low) / 2.0 * widen
    return {
        "estimate_low": _r(max(0, center - half_span)),
        "estimate_central": _r(fair_central),
        "estimate_high": _r(center + half_span),
        "note": f"Confidence {confidence_label}: estimate range widened by {int((widen-1)*100)}% vs raw Fair Value spread.",
    }


def reserve_recommendation(estimate_low: Optional[float], confidence_label: str) -> dict:
    """Reserve is set as a fraction of the low estimate, more conservative
    (lower fraction) as confidence drops — spec principle: never recommend a
    reserve the evidence can't defend."""
    if estimate_low is None:
        return {"reserve": None, "note": "No estimate available."}
    frac = AUCTION_CONFIG["reserve_as_fraction_of_low_estimate"].get(confidence_label, 0.70)
    reserve = estimate_low * frac
    return {"reserve": _r(reserve), "fraction_of_low_estimate": frac,
            "note": f"Reserve set at {int(frac*100)}% of low estimate given {confidence_label} confidence."}


def starting_bid_recommendation(reserve: Optional[float]) -> dict:
    if reserve is None:
        return {"starting_bid": None}
    sb = reserve * AUCTION_CONFIG["starting_bid_as_fraction_of_reserve"]
    return {"starting_bid": _r(sb)}


def seller_net_proceeds(expected_hammer: Optional[float], seller_commission_pct: float,
                         insurance_pct: float = 0.0, photography_fee: float = 0.0,
                         listing_fee: float = 0.0, other_fixed_fees: float = 0.0) -> dict:
    """What the seller actually walks away with. Commission and insurance are
    percentage-of-hammer deductions (typical auction-house convention);
    photography/listing/other fees are fixed deductions. Never assumes a
    'no commission' default — caller must supply the real rate."""
    if expected_hammer is None:
        return {"net_proceeds": None, "total_deductions": None}
    commission = expected_hammer * (seller_commission_pct / 100.0)
    insurance = expected_hammer * (insurance_pct / 100.0)
    total_deductions = commission + insurance + photography_fee + listing_fee + other_fixed_fees
    net = expected_hammer - total_deductions
    effective_net = max(0.0, net)
    net_pct = (effective_net / expected_hammer) * 100 if (expected_hammer and expected_hammer > 0) else 0.0
    return {
        "expected_hammer": _r(expected_hammer),
        "commission": _r(commission),
        "insurance": _r(insurance),
        "fixed_fees": _r(photography_fee + listing_fee + other_fixed_fees),
        "total_deductions": _r(total_deductions),
        "net_proceeds": _r(net),
        "net_pct_of_hammer": _r(net_pct, 1),
    }


def sell_advice(snapshot: dict, seller_commission_pct: float, insurance_pct: float = 0.0,
                 photography_fee: float = 0.0, listing_fee: float = 0.0,
                 other_fixed_fees: float = 0.0) -> dict:
    """Top-level sell-side recommendation built from an already-computed
    Valuation Snapshot (auction_valuation.compute_valuation_snapshot) —
    consistent with the buy-side bid_advice() function, both read from the
    same underlying evidence."""
    fusion = snapshot.get("fusion", {})
    confidence = snapshot.get("confidence", {})
    conf_label = confidence.get("label", "LOW")

    est = estimate_range(fusion.get("fair_low"), fusion.get("fair_central"), fusion.get("fair_high"), conf_label)
    reserve = reserve_recommendation(est["estimate_low"], conf_label)
    starting = starting_bid_recommendation(reserve["reserve"])
    net_at_low = seller_net_proceeds(est["estimate_low"], seller_commission_pct, insurance_pct,
                                      photography_fee, listing_fee, other_fixed_fees)
    net_at_central = seller_net_proceeds(est["estimate_central"], seller_commission_pct, insurance_pct,
                                          photography_fee, listing_fee, other_fixed_fees)
    net_at_high = seller_net_proceeds(est["estimate_high"], seller_commission_pct, insurance_pct,
                                       photography_fee, listing_fee, other_fixed_fees)

    return {
        "estimate": est,
        "reserve": reserve,
        "starting_bid": starting,
        "net_proceeds": {
            "at_low_estimate": net_at_low,
            "at_central_estimate": net_at_central,
            "at_high_estimate": net_at_high,
        },
        "confidence": conf_label,
        "realized_sample_count": snapshot.get("realized_market", {}).get("count", 0),
    }


def _r(x, ndigits=2):
    return None if x is None else round(x, ndigits)