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


REALIZED_ELIGIBLE_SEMANTICS = {PriceSemantics.HAMMER, PriceSemantics.REALIZED_INCL_PREMIUM}


class ComparableTier(str, Enum):
    EXACT = "EXACT"
    STRONG = "STRONG"
    SUPPORTING = "SUPPORTING"
    REJECT = "REJECT"


class SourceStatus(str, Enum):
    ENABLED_AUTOMATIC = "ENABLED_AUTOMATIC"
    ENABLED_MANUAL = "ENABLED_MANUAL"
    REQUIRES_LICENSE = "REQUIRES_LICENSE"
    REQUIRES_USER_LOGIN = "REQUIRES_USER_LOGIN"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNDER_REVIEW = "UNDER_REVIEW"


class GradeBucket(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    FINE = "FINE"
    VF = "VF"
    XF = "XF"
    AU = "AU"
    UNC_LOW = "UNC_LOW"
    UNC_MID = "UNC_MID"
    UNC_HIGH = "UNC_HIGH"
    PROOF = "PROOF"
    DETAILS = "DETAILS"


@dataclass
class AuctionComparable:
    source: str = "manual"
    source_url: Optional[str] = None
    auction_house: Optional[str] = None
    auction_name: Optional[str] = None
    auction_date: Optional[str] = None
    lot_number: Optional[str] = None

    title: str = ""
    description: Optional[str] = None

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

    grade_raw: Optional[str] = None
    grade_bucket: str = GradeBucket.UNKNOWN.value
    grading_company: Optional[str] = None
    cert_number: Optional[str] = None

    hammer_price: Optional[float] = None
    realized_price: Optional[float] = None
    price_semantics: str = PriceSemantics.UNKNOWN.value
    currency: Optional[str] = None

    original_price: Optional[float] = None
    original_currency: Optional[str] = None
    normalized_price: Optional[float] = None
    fx_date: Optional[str] = None
    fx_source: Optional[str] = None
    fx_confidence: Optional[str] = None

    estimate_low: Optional[float] = None
    estimate_high: Optional[float] = None

    sold: Optional[bool] = None
    withdrawn: bool = False
    unsold: bool = False

    identity_match_score: Optional[int] = None
    comparable_tier: str = ComparableTier.REJECT.value
    match_reasons: list = field(default_factory=list)
    grade_distance: Optional[int] = None

    age_days: Optional[int] = None
    recency_weight: Optional[float] = None
    source_weight: Optional[float] = None
    grade_weight: Optional[float] = None
    match_weight: Optional[float] = None
    final_weight: Optional[float] = None
    outlier_flag: Optional[str] = None

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    dedupe_key: Optional[str] = None
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")
    notes: Optional[str] = None

    def realized_eligible(self) -> bool:
        if self.withdrawn or self.unsold:
            return False
        if self.sold is False:
            return False
        if self.price_semantics not in (PriceSemantics.HAMMER.value, PriceSemantics.REALIZED_INCL_PREMIUM.value):
            return False
        return self.effective_price() is not None

    def effective_price(self) -> Optional[float]:
        if self.normalized_price is not None:
            return self.normalized_price
        if self.price_semantics == PriceSemantics.HAMMER.value and self.hammer_price is not None:
            return self.hammer_price
        if self.price_semantics == PriceSemantics.REALIZED_INCL_PREMIUM.value and self.realized_price is not None:
            return self.realized_price
        return None

    def compute_dedupe_key(self) -> str:
        house = (self.auction_house or "").strip().lower()
        adate = (self.auction_date or "").strip()
        lot = (self.lot_number or "").strip()
        if house and adate and lot:
            key = f"{house}|{adate}|{lot}"
        else:
            title_norm = re.sub(r"\s+", " ", (self.title or "").strip().lower())
            price = self.effective_price()
            import math
            is_valid_price = price is not None and isinstance(price, (int, float)) and math.isfinite(price)
            price_s = f"{price:.2f}" if is_valid_price else ""
            key = f"{title_norm}|{adate}|{price_s}|{house}"
        self.dedupe_key = key
        return key

    def to_dict(self) -> dict:
        return asdict(self)


def parse_auction_date(raw: str) -> Optional[date]:
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