"""
CoinBids Auction Intelligence 3.0 — canonical data model (Phase 2).

This module has NO external dependencies beyond the Python standard library
and defines the AuctionComparable record that every source adapter (manual,
CSV, or a future licensed automated adapter) must produce. The valuation
engine only ever sees AuctionComparable objects — it never knows or cares
where a record came from.

CRITICAL DISTINCTION preserved throughout (spec §4, §16, §47):
    HAMMER            = the winning bid itself, no buyer's premium included.
    REALIZED_INCL_PREMIUM = hammer + buyer's premium (what the buyer actually paid).
    FIXED_PRICE       = an after-sale/net-price/buy-now listing, not an auction bid.
    ESTIMATE          = a pre-sale estimate. NEVER a sale. Never enters a
                        realized-price statistic.
    UNKNOWN           = semantics not established; usable only as supporting
                        context until verified, never as a primary realized input.
"""
from __future__ import annotations
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Optional


class PriceSemantics(str, Enum):
    HAMMER = "HAMMER"
    REALIZED_INCL_PREMIUM = "REALIZED_INCL_PREMIUM"
    FIXED_PRICE = "FIXED_PRICE"
    ESTIMATE = "ESTIMATE"
    UNKNOWN = "UNKNOWN"


# Only these semantics may ever feed a "realized sale" statistic (median,
# quantiles, etc.). ESTIMATE and UNKNOWN are explicitly excluded per spec §47.
REALIZED_ELIGIBLE_SEMANTICS = {PriceSemantics.HAMMER, PriceSemantics.REALIZED_INCL_PREMIUM}


class ComparableTier(str, Enum):
    EXACT = "EXACT"
    STRONG = "STRONG"
    SUPPORTING = "SUPPORTING"
    REJECT = "REJECT"


class SourceStatus(str, Enum):
    """Feasibility status for a source adapter — see auction_source_matrix.md."""
    ENABLED_AUTOMATIC = "ENABLED_AUTOMATIC"
    ENABLED_MANUAL = "ENABLED_MANUAL"
    REQUIRES_LICENSE = "REQUIRES_LICENSE"
    REQUIRES_USER_LOGIN = "REQUIRES_USER_LOGIN"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNDER_REVIEW = "UNDER_REVIEW"


# Canonical grade buckets (spec §10). Coarse-grained on purpose — a raw,
# non-certified description ("nice VF", "lightly cleaned XF") should map to
# one of these buckets, never to an invented precise numeric grade.
class GradeBucket(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"          # Poor / Fair / AG / G / VG
    FINE = "FINE"         # F
    VF = "VF"
    XF = "XF"            # XF / EF
    AU = "AU"
    UNC_LOW = "UNC_LOW"      # MS60-62 / low BU
    UNC_MID = "UNC_MID"      # MS63-65
    UNC_HIGH = "UNC_HIGH"     # MS66-70
    PROOF = "PROOF"        # PF/PR60-70, any proof
    DETAILS = "DETAILS"      # cleaned/damaged/details-graded — NOT a straight grade


@dataclass
class AuctionComparable:
    """One realized (or fixed-price, or estimate-only) auction record.

    id, dedupe_key and fetched_at are populated automatically if omitted.
    Nothing here is invented: if a source doesn't supply a field, it stays
    None — the statistics/valuation layer must handle missing data honestly
    rather than the adapter guessing a value.
    """
    # --- provenance ---
    source: str = "manual"                    # "manual" | "csv" | adapter name
    source_url: Optional[str] = None
    auction_house: Optional[str] = None
    auction_name: Optional[str] = None
    auction_date: Optional[str] = None         # "YYYY-MM-DD"
    lot_number: Optional[str] = None

    # --- listing text ---
    title: str = ""
    description: Optional[str] = None

    # --- identity (filled by auction_matching.py via the shared resolver) ---
    country: Optional[str] = None
    country_code: Optional[str] = None
    currency_code: Optional[str] = None
    denomination_value: Optional[float] = None
    coin_year: Optional[int] = None
    issuer: Optional[str] = None
    mint: Optional[str] = None
    mintmark: Optional[str] = None
    variant: Optional[str] = None
    catalog_ids: dict = field(default_factory=dict)

    # --- grade ---
    grade_raw: Optional[str] = None
    grade_bucket: str = GradeBucket.UNKNOWN.value
    grading_company: Optional[str] = None       # NGC / PCGS / raw / None
    cert_number: Optional[str] = None

    # --- price ---
    hammer_price: Optional[float] = None
    realized_price: Optional[float] = None      # hammer + buyer premium, if known
    price_semantics: str = PriceSemantics.UNKNOWN.value
    currency: Optional[str] = None              # original transaction currency

    # --- FX-normalized (to CoinBids' working currency, typically EUR) ---
    original_price: Optional[float] = None
    original_currency: Optional[str] = None
    normalized_price: Optional[float] = None
    fx_date: Optional[str] = None
    fx_source: Optional[str] = None
    fx_confidence: Optional[str] = None         # "auction_date" | "current_fallback"

    # --- estimate (never a sale) ---
    estimate_low: Optional[float] = None
    estimate_high: Optional[float] = None

    # --- lot status ---
    sold: Optional[bool] = None
    withdrawn: bool = False
    unsold: bool = False

    # --- matching (filled by auction_matching.py) ---
    identity_match_score: Optional[int] = None
    comparable_tier: str = ComparableTier.REJECT.value
    match_reasons: list = field(default_factory=list)
    grade_distance: Optional[int] = None

    # --- weighting (filled by auction_stats.py) ---
    age_days: Optional[int] = None
    recency_weight: Optional[float] = None
    source_weight: Optional[float] = None
    grade_weight: Optional[float] = None
    match_weight: Optional[float] = None
    final_weight: Optional[float] = None
    outlier_flag: Optional[str] = None          # None | "OUTLIER_LOW" | "OUTLIER_HIGH"

    # --- bookkeeping ---
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    dedupe_key: Optional[str] = None
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")
    notes: Optional[str] = None

    def realized_eligible(self) -> bool:
        """True only if this record's price can legitimately enter a
        realized-sale statistic: it must actually be a completed sale (not
        withdrawn/unsold/estimate) with HAMMER or REALIZED_INCL_PREMIUM
        semantics and a usable price."""
        if self.withdrawn or self.unsold:
            return False
        if self.sold is False:
            return False
        if self.price_semantics not in (PriceSemantics.HAMMER.value, PriceSemantics.REALIZED_INCL_PREMIUM.value):
            return False
        return self.effective_price() is not None

    def effective_price(self) -> Optional[float]:
        """The price to use for statistics: prefer the normalized (FX'd,
        currency-consistent) figure; fall back to the raw hammer/realized
        price only when no normalization has been performed (e.g. already
        in the target currency)."""
        if self.normalized_price is not None:
            return self.normalized_price
        if self.price_semantics == PriceSemantics.HAMMER.value and self.hammer_price is not None:
            return self.hammer_price
        if self.price_semantics == PriceSemantics.REALIZED_INCL_PREMIUM.value and self.realized_price is not None:
            return self.realized_price
        return None

    def compute_dedupe_key(self) -> str:
        """auction house + date + lot number is the strongest key; falls back
        to a normalized title+date+price+house combination when lot number
        is unavailable (spec §45)."""
        house = (self.auction_house or "").strip().lower()
        adate = (self.auction_date or "").strip()
        lot = (self.lot_number or "").strip()
        if house and adate and lot:
            key = f"{house}|{adate}|{lot}"
        else:
            title_norm = re.sub(r"\s+", " ", (self.title or "").strip().lower())
            price = self.effective_price()
            price_s = f"{price:.2f}" if price is not None else ""
            key = f"{title_norm}|{adate}|{price_s}|{house}"
        self.dedupe_key = key
        return key

    def to_dict(self) -> dict:
        return asdict(self)


def parse_auction_date(raw: str) -> Optional[date]:
    """Parse a YYYY-MM-DD auction date string. Returns None (never raises,
    never guesses) if the string doesn't match — a missing/invalid date must
    stay missing, not silently become "today"."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
