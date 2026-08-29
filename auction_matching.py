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


_AUCTION_DENOM_ALIASES = {
    "GRD": ["drachma", "drachmas", "drachmai", "drachmae", "drachme", "drachmen",
            "drachmi", "drakhma", "drakhmai", "drachm", "δραχμη", "δραχμες", "δραχμαι", "δρχ"],
    "ITL": ["lira", "lire", "liras", "litl"],
    "ESP": ["peseta", "pesetas", "pta", "pts", "ptas"],
    "DEM": ["mark", "marks", "deutsche mark", "reichsmark", "dm", "dmark", "d mark"],
    "NLG": ["guilder", "guilders", "gulden", "fl", "florin", "nlg"],
    "ATS": ["schilling", "schillings", "ats"],
    "FIM": ["markka", "markkaa", "finnmark", "finnmarkka", "fim"],
}


def validate_auction_denomination(target: dict, comp: AuctionComparable) -> bool:
    """Validate denomination from structured data first, then auction text.

    This intentionally avoids importing numisvault_backend.py because that file
    already imports auction_matching.py; importing it back here would create a
    circular dependency.  The text fallback is conservative: exact numeric face
    value plus an alias associated with the target currency code.
    """
    target_value = target.get("denomination_value")
    if target_value is None:
        return True

    if comp.denomination_value is not None:
        try:
            return abs(float(comp.denomination_value) - float(target_value)) < 1e-9
        except (TypeError, ValueError):
            return False

    currency_code = str(target.get("currency_code") or "").upper()
    aliases = _AUCTION_DENOM_ALIASES.get(currency_code) or []
    if not aliases:
        return False

    text = " ".join(filter(None, [comp.title, comp.description or ""])).lower().replace(",", ".")
    number = f"{float(target_value):g}"
    for alias in sorted(set(aliases), key=len, reverse=True):
        if __import__("re").search(
            rf"(?<!\d){__import__('re').escape(number)}\s*{__import__('re').escape(alias)}(?![a-z])",
            text, __import__("re").I
        ):
            return True
    return False


def classify_comparable(target: dict, comp: AuctionComparable) -> AuctionComparable:
    reasons = []
    hard_reject_reasons = []
    applicable_weight = 0
    achieved_weight = 0

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
        if validate_auction_denomination(target, comp):
            achieved_weight += WEIGHTS["denomination"]; reasons.append("denomination exact")
        else:
            hard_reject_reasons.append("wrong denomination")

    t_variants = set(target.get("variants") or [])
    c_variant = (comp.variant or "").strip().lower()
    if t_variants:
        applicable_weight += WEIGHTS["variant"]
        if c_variant and any(v.lower() in c_variant or c_variant in v.lower() for v in t_variants):
            achieved_weight += WEIGHTS["variant"]; reasons.append("variant match")
        else:
            hard_reject_reasons.append("wrong/uncertain major variant")

    t_catalog = target.get("catalog_ids") or {}
    c_catalog = comp.catalog_ids or {}
    if t_catalog:
        applicable_weight += WEIGHTS["catalog_id"]
        matched_any = any(c_catalog.get(k) == v for k, v in t_catalog.items() if k in c_catalog)
        if matched_any:
            achieved_weight += WEIGHTS["catalog_id"]; reasons.append("catalog ID exact")

    t_mintmark = target.get("mintmark")
    if t_mintmark:
        applicable_weight += WEIGHTS["mintmark"]
        if comp.mintmark and comp.mintmark.upper() == str(t_mintmark).upper():
            achieved_weight += WEIGHTS["mintmark"]; reasons.append("mintmark exact")

    t_issuer = target.get("issuer")
    if t_issuer:
        applicable_weight += WEIGHTS["issuer"]
        if comp.issuer and t_issuer.lower() in comp.issuer.lower():
            achieved_weight += WEIGHTS["issuer"]; reasons.append("issuer exact")

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

        # BUG 13: Proof-to-Proof is an exact bucket relationship. If one or
        # both sides have no explicit PF/PR numeric grade, treat the grade
        # distance as zero rather than falling into the unknown relationship.
        if t_grade_bucket == "PROOF" and comp.grade_bucket == "PROOF":
            same_bucket = True
            bucket_distance = 0
            if t_numeric_grade is not None and c_numeric is not None:
                numeric_distance = abs(int(t_numeric_grade) - int(c_numeric))
            else:
                numeric_distance = 0

        comp.grade_distance = numeric_distance if numeric_distance is not None else bucket_distance
        gw = grade_weight_for_distance(numeric_distance, bucket_distance, same_bucket)
        comp.grade_weight = gw
        if numeric_distance == 0 or same_bucket:
            achieved_weight += WEIGHTS["grade_exact"]; reasons.append("grade exact")
        elif (numeric_distance is not None and numeric_distance <= 2) or (bucket_distance is not None and bucket_distance <= 1):
            achieved_weight += WEIGHTS["grade_near"]; reasons.append("grade near (±1 band)")

        if comp.grade_bucket == "DETAILS" and t_grade_bucket and t_grade_bucket != "DETAILS":
            hard_reject_reasons.append("details/cleaned coin vs straight-grade target")

    if target.get("grading_company"):
        applicable_weight += WEIGHTS["certification"]
        if comp.grading_company and comp.grading_company == target.get("grading_company"):
            achieved_weight += WEIGHTS["certification"]; reasons.append("certification match")

    if comp.withdrawn:
        hard_reject_reasons.append("withdrawn lot")
    if comp.unsold:
        hard_reject_reasons.append("unsold lot (no hammer)")

    # BUG 14: check for estimate presence explicitly rather than relying on
    # truthiness, so empty-string leaks cannot masquerade as missing data.
    has_est = (comp.estimate_low is not None and comp.estimate_low != "") or (comp.estimate_high is not None and comp.estimate_high != "")
    if comp.effective_price() is None and comp.hammer_price is None and comp.realized_price is None and not has_est:
        hard_reject_reasons.append("no usable price")

    comp.match_reasons = reasons + (["REJECT: " + ", ".join(hard_reject_reasons)] if hard_reject_reasons else [])

    if hard_reject_reasons:
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
