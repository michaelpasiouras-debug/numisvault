"""
CoinBids Auction Intelligence 3.0 — grade normalization (spec §10).

Maps a raw grade string (free text, certified numeric grade, or bucket word)
to a canonical GradeBucket, WITHOUT inventing precision that isn't there.
Critically: "UNC" is never silently treated as "MS65" — an uncertified
description maps to a coarse bucket (UNC_MID as a reasonable center-of-mass
default for bare "UNC"/"BU"), while an actual certified numeric grade
(e.g. "MS64", "PCGS MS64") maps to the correct narrower bucket.
"""
from __future__ import annotations
import re
from auction_models import GradeBucket

_NUMERIC_MS_RE = re.compile(r"\b(?:MS|UNC)\s*-?\s*(\d{2})\b", re.I)
_NUMERIC_PF_RE = re.compile(r"\b(?:PF|PR|PROOF)\s*-?\s*(\d{2})\b", re.I)
_GRADING_CO_RE = re.compile(r"\b(NGC|PCGS|ANACS|ICG|CGC)\b", re.I)
_CERT_RE = re.compile(r"\b(?:cert(?:ification)?\s*#?|cert\s*no\.?)\s*([A-Za-z0-9\-]+)", re.I)

_DETAILS_TERMS = ("details", "cleaned", "damaged", "corroded", "holed", "polished",
                   "environmental damage", "improperly cleaned", "scratched")

_WORD_BUCKETS = [
    # (regex, GradeBucket) — ordered most-specific first.
    (re.compile(r"\bpoor\b|\bfair\b|\bag\b|\babout good\b|\bg-?\d?\b(?!ood)|\bgood\b|\bvg\b|\bvery good\b", re.I), GradeBucket.LOW),
    (re.compile(r"\bfine\b|\bf-?\d{0,2}\b", re.I), GradeBucket.FINE),
    (re.compile(r"\bvf\b|\bvery fine\b", re.I), GradeBucket.VF),
    (re.compile(r"\bxf\b|\bef\b|\bextremely fine\b|\bextra fine\b", re.I), GradeBucket.XF),
    (re.compile(r"\bau\b|\babout uncirculated\b|\balmost uncirculated\b", re.I), GradeBucket.AU),
    (re.compile(r"\bproof\b|\bpf\b|\bpr\b|\bpolierte platte\b", re.I), GradeBucket.PROOF),
    (re.compile(r"\bunc\b|\buncirculated\b|\bbu\b|\bbrilliant uncirculated\b|\bfleur de coin\b|\bfdc\b|\bstempelglanz\b", re.I), GradeBucket.UNC_MID),
]


def _ms_number_to_bucket(n: int) -> GradeBucket:
    if n <= 62:
        return GradeBucket.UNC_LOW
    if n <= 65:
        return GradeBucket.UNC_MID
    return GradeBucket.UNC_HIGH


def normalize_grade(raw: str) -> dict:
    """Returns {"bucket": GradeBucket.value, "grading_company": str|None,
    "cert_number": str|None, "numeric_grade": int|None, "is_details": bool}.

    Never returns a fabricated numeric grade for a raw/non-certified
    description — numeric_grade stays None unless the text itself contained
    an explicit MS/PF number."""
    text = raw or ""
    lower = text.lower()

    is_details = any(term in lower for term in _DETAILS_TERMS)

    grading_company = None
    m = _GRADING_CO_RE.search(text)
    if m:
        grading_company = m.group(1).upper()

    cert_number = None
    m = _CERT_RE.search(text)
    if m:
        cert_number = m.group(1)

    numeric_grade = None
    m = _NUMERIC_PF_RE.search(text)
    if m:
        numeric_grade = int(m.group(1))
        bucket = GradeBucket.PROOF
    else:
        m = _NUMERIC_MS_RE.search(text)
        if m:
            numeric_grade = int(m.group(1))
            bucket = _ms_number_to_bucket(numeric_grade)
        else:
            bucket = GradeBucket.UNKNOWN
            for rx, b in _WORD_BUCKETS:
                if rx.search(text):
                    bucket = b
                    break

    if is_details and bucket != GradeBucket.UNKNOWN:
        # A "details"/cleaned/damaged coin is NOT a straight grade of that
        # tier even if the raw text also mentions e.g. "XF details" — flag it
        # distinctly so match scoring can penalize it appropriately (spec §8:
        # "cleaned/damaged details coin versus straight grade when not
        # adjusted" is a REJECT-tier mismatch unless the target is also details).
        bucket = GradeBucket.DETAILS

    return {
        "bucket": bucket.value,
        "grading_company": grading_company,
        "cert_number": cert_number,
        "numeric_grade": numeric_grade,
        "is_details": is_details,
    }


# Ordinal ordering used for grade_distance calculations (spec §9/§13).
_BUCKET_ORDER = [
    GradeBucket.LOW, GradeBucket.FINE, GradeBucket.VF, GradeBucket.XF, GradeBucket.AU,
    GradeBucket.UNC_LOW, GradeBucket.UNC_MID, GradeBucket.UNC_HIGH,
]


def grade_bucket_distance(a: str, b: str) -> "int|None":
    """Ordinal distance between two grade buckets (0 = identical bucket).
    Returns None if either bucket is UNKNOWN/DETAILS/PROOF (not placed on the
    circulated->uncirculated ordinal scale) or not recognized — these need
    dedicated handling by the caller rather than a fabricated numeric gap."""
    try:
        ia = _BUCKET_ORDER.index(GradeBucket(a))
        ib = _BUCKET_ORDER.index(GradeBucket(b))
    except (ValueError, KeyError):
        return None
    return abs(ia - ib)


def grade_weight_for_distance(numeric_distance: "int|None", bucket_distance: "int|None", same_bucket: bool) -> float:
    """Spec §13 example weighting. Prefers exact numeric-grade distance when
    both records have a certified numeric grade; otherwise falls back to
    bucket-level comparison."""
    if numeric_distance is not None:
        if numeric_distance == 0:
            return 1.00
        if numeric_distance == 1:
            return 0.90
        if numeric_distance == 2:
            return 0.75
        return 0.30
    if same_bucket:
        return 0.70
    if bucket_distance is None:
        return 0.55  # unknown grade relationship
    if bucket_distance == 1:
        return 0.45
    return 0.30
