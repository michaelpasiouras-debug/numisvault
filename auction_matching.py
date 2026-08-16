"""
CoinBids Auction Intelligence 3.0 — comparable matching & classification
(Phase 5, spec §8/§9).

Classifies an AuctionComparable against a resolved target coin identity into
EXACT / STRONG / SUPPORTING / REJECT, with an explainable 0-100 match score.
Reuses the existing shared Coin Identity Resolver (coin_identity_resolver.py)
for negative-term detection so "replica"/"banknote"/"set"/"lot" are rejected
consistently with the rest of CoinBids, rather than a second, divergent list.

Hard validation always precedes fuzzy scoring (spec §14): a wrong year, wrong
denomination, wrong country, or an excluded product-type term forces REJECT
regardless of how well anything else lines up.
"""
from __future__ import annotations
from typing import Optional

from auction_models import AuctionComparable, ComparableTier
from auction_grades import normalize_grade, grade_bucket_distance, grade_weight_for_distance

try:
    from coin_identity_resolver import get_resolver
    _RESOLVER_AVAILABLE = True
except Exception:
    _RESOLVER_AVAILABLE = False


WEIGHTS = {
    "country": 15,
    "denomination": 15,
    "year": 15,
    "variant": 15,
    "catalog_id": 15,
    "mintmark": 5,
    "grade_exact": 10,
    "grade_near": 6,
    "issuer": 5,
    "certification": 5,
}

TIER_THRESHOLDS = {
    "EXACT": 90,
    "STRONG": 75,
    "SUPPORTING": 55,
}


def classify_comparable(target: dict, comp: AuctionComparable) -> AuctionComparable:
    """Mutates and returns `comp` with identity_match_score, comparable_tier,
    match_reasons and grade_distance populated. `target` is a resolved
    identity dict in the same shape as coin_identity_resolver's `best`
    (country_code, currency_code, year, denomination_value, issuer,
    mintmark, variants, catalog_ids).

    The score is normalized over only the fields actually PRESENT/applicable
    in the target identity — a target with no catalog ID or variant
    requirement can still reach EXACT on a perfect country+year+denomination
    (+grade, when relevant) match; it is not artificially capped below the
    EXACT threshold merely because some optional field wasn't part of this
    particular identity (same principle as coin_identity_resolver's
    listing_match_score)."""
    reasons = []
    hard_reject_reasons = []
    applicable_weight = 0
    achieved_weight = 0

    # --- negative product-type terms: hard reject, reuse shared resolver ---
    if _RESOLVER_AVAILABLE:
        neg_text = " ".join(filter(None, [comp.title, comp.description or ""]))
        try:
            neg = get_resolver()._negative_flags(neg_text)
        except Exception:
            neg = []
        if neg:
            comp.identity_match_score = 0
            comp.comparable_tier = ComparableTier.REJECT.value
            comp.match_reasons = ["negative product term: " + ",".join(neg)]
            return comp

    # --- hard fields ---
    t_country = target.get("country_code")
    if t_country:
        applicable_weight += WEIGHTS["country"]
        if comp.country_code and comp.country_code == t_country:
            achieved_weight += WEIGHTS["country"]; reasons.append("country exact")
        else:
            hard_reject_reasons.append("wrong country")

    t_year = target.get("year")
    if t_year:
        applicable_weight += WEIGHTS["year"]
        if comp.coin_year == t_year:
            achieved_weight += WEIGHTS["year"]; reasons.append("year exact")
        else:
            hard_reject_reasons.append("wrong year")

    t_denom = target.get("denomination_value")
    if t_denom is not None:
        applicable_weight += WEIGHTS["denomination"]
        if comp.denomination_value is not None and abs(float(comp.denomination_value) - float(t_denom)) < 1e-9:
            achieved_weight += WEIGHTS["denomination"]; reasons.append("denomination exact")
        else:
            hard_reject_reasons.append("wrong denomination")

    # --- variant ---
    t_variants = set(target.get("variants") or [])
    c_variant = (comp.variant or "").strip().lower()
    if t_variants:
        applicable_weight += WEIGHTS["variant"]
        if c_variant and any(v.lower() in c_variant or c_variant in v.lower() for v in t_variants):
            achieved_weight += WEIGHTS["variant"]; reasons.append("variant match")
        else:
            hard_reject_reasons.append("wrong/uncertain major variant")

    # --- catalog ID ---
    t_catalog = target.get("catalog_ids") or {}
    c_catalog = comp.catalog_ids or {}
    if t_catalog:
        applicable_weight += WEIGHTS["catalog_id"]
        matched_any = any(c_catalog.get(k) == v for k, v in t_catalog.items() if k in c_catalog)
        if matched_any:
            achieved_weight += WEIGHTS["catalog_id"]; reasons.append("catalog ID exact")

    # --- mint/mintmark ---
    t_mintmark = target.get("mintmark")
    if t_mintmark:
        applicable_weight += WEIGHTS["mintmark"]
        if comp.mintmark and comp.mintmark.upper() == str(t_mintmark).upper():
            achieved_weight += WEIGHTS["mintmark"]; reasons.append("mintmark exact")

    # --- issuer ---
    t_issuer = target.get("issuer")
    if t_issuer:
        applicable_weight += WEIGHTS["issuer"]
        if comp.issuer and t_issuer.lower() in comp.issuer.lower():
            achieved_weight += WEIGHTS["issuer"]; reasons.append("issuer exact")

    # --- grade ---
    comp_grade_info = normalize_grade(comp.grade_raw or "")
    comp.grade_bucket = comp_grade_info["bucket"]
    if not comp.grading_company:
        comp.grading_company = comp_grade_info["grading_company"]
    if not comp.cert_number:
        comp.cert_number = comp_grade_info["cert_number"]

    t_grade_bucket = target.get("grade_bucket")
    t_numeric_grade = target.get("grade_numeric")
    if t_grade_bucket:
        applicable_weight += WEIGHTS["grade_exact"]
        c_numeric = comp_grade_info["numeric_grade"]
        numeric_distance = None
        if t_numeric_grade is not None and c_numeric is not None:
            numeric_distance = abs(int(t_numeric_grade) - int(c_numeric))
        bucket_distance = grade_bucket_distance(t_grade_bucket, comp.grade_bucket)
        same_bucket = (t_grade_bucket == comp.grade_bucket)
        comp.grade_distance = numeric_distance if numeric_distance is not None else bucket_distance
        gw = grade_weight_for_distance(numeric_distance, bucket_distance, same_bucket)
        comp.grade_weight = gw
        if numeric_distance == 0 or same_bucket:
            achieved_weight += WEIGHTS["grade_exact"]; reasons.append("grade exact")
        elif (numeric_distance is not None and numeric_distance <= 2) or (bucket_distance is not None and bucket_distance <= 1):
            achieved_weight += WEIGHTS["grade_near"]; reasons.append("grade near (±1 band)")
        # A details/cleaned/damaged coin compared against a straight-grade
        # target is a material mismatch unless the target is ALSO details.
        if comp.grade_bucket == "DETAILS" and t_grade_bucket != "DETAILS":
            hard_reject_reasons.append("details/cleaned coin vs straight-grade target")
    else:
        # Target has no grade requirement at all — still compute a bucket for
        # display, but it never penalizes or inflates the applicable score.
        pass

    if target.get("grading_company"):
        applicable_weight += WEIGHTS["certification"]
        if comp.grading_company and comp.grading_company == target.get("grading_company"):
            achieved_weight += WEIGHTS["certification"]; reasons.append("certification match")

    # --- lot status: unsold/withdrawn/estimate-only never validate as a
    # comparable sale (spec §46), independent of identity fields.
    if comp.withdrawn:
        hard_reject_reasons.append("withdrawn lot")
    if comp.unsold:
        hard_reject_reasons.append("unsold lot (no hammer)")
    if comp.effective_price() is None and comp.hammer_price is None and comp.realized_price is None \
            and not (comp.estimate_low or comp.estimate_high):
        hard_reject_reasons.append("no usable price")

    comp.match_reasons = reasons + (["REJECT: " + ", ".join(hard_reject_reasons)] if hard_reject_reasons else [])

    if hard_reject_reasons:
        # Still visible in evidence (partial score), but always REJECT tier.
        comp.identity_match_score = int(round(100.0 * achieved_weight / applicable_weight)) if applicable_weight else 0
        comp.identity_match_score = min(comp.identity_match_score, 30)
        comp.comparable_tier = ComparableTier.REJECT.value
        return comp

    score = int(round(100.0 * achieved_weight / applicable_weight)) if applicable_weight else 0
    score = max(0, min(100, score))
    comp.identity_match_score = score
    if score >= TIER_THRESHOLDS["EXACT"]:
        comp.comparable_tier = ComparableTier.EXACT.value
    elif score >= TIER_THRESHOLDS["STRONG"]:
        comp.comparable_tier = ComparableTier.STRONG.value
    elif score >= TIER_THRESHOLDS["SUPPORTING"]:
        comp.comparable_tier = ComparableTier.SUPPORTING.value
    else:
        comp.comparable_tier = ComparableTier.REJECT.value
    return comp
