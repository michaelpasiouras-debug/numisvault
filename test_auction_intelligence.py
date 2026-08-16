"""
CoinBids Auction Intelligence 3.0 — unit test suite.
Run: python3 test_auction_intelligence.py
Every test is a real, executed assertion — no claims-only test list.
"""
import sys
import math
from datetime import date

sys.path.insert(0, ".")

import auction_stats as stats
import auction_grades as grades
from auction_models import AuctionComparable, PriceSemantics, ComparableTier, GradeBucket, parse_auction_date
from auction_matching import classify_comparable
from auction_sources import ManualComparableAdapter, CSVComparableAdapter, get_enabled_adapters

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


# =========================================================================
# SECTION 58 — STATISTICS
# =========================================================================
print("=== §58 Statistics ===")

check("weighted_median equal weights [100,200,300] -> 200",
      stats.weighted_median([100, 200, 300], [1, 1, 1]) == 200)

check("weighted_median empty -> None", stats.weighted_median([], []) is None)

check("weighted_median single value -> that value",
      stats.weighted_median([450], [1]) == 450)

# heavier weight should pull the median toward that value
wm = stats.weighted_median([100, 200, 300], [1, 1, 10])
check(f"weighted_median heavy-weighted 300 pulls result up (got {wm})", wm is not None and wm > 200)

check("weighted_quantile p=0 -> min", stats.weighted_quantile([10, 20, 30], [1, 1, 1], 0.0) == 10)
check("weighted_quantile p=1 -> max", stats.weighted_quantile([10, 20, 30], [1, 1, 1], 1.0) == 30)

p25 = stats.weighted_quantile([1, 2, 3, 4, 5], [1, 1, 1, 1, 1], 0.25)
p75 = stats.weighted_quantile([1, 2, 3, 4, 5], [1, 1, 1, 1, 1], 0.75)
check(f"weighted_quantile P25/P75 symmetric around median (P25={p25}, P75={p75})",
      p25 is not None and p75 is not None and abs((p25 + p75) / 2 - 3) < 0.5)

outlier_vals = [100, 105, 110, 115, 500]
flags = stats.flag_outliers(outlier_vals)
check(f"outlier detection flags the 500 as high (flags={flags})",
      flags[-1] == "OUTLIER_HIGH" and all(f is None for f in flags[:-1]))

flags_small = stats.flag_outliers([100, 105, 500])
check("outlier detection skips flagging below min sample size (n<5)",
      all(f is None for f in flags_small))

w_recent = stats.recency_weight(90, half_life_days=730)
w_old = stats.recency_weight(1460, half_life_days=730)
check(f"recency: 3-month-old sale weighted higher than 4-year-old sale ({w_recent:.3f} > {w_old:.3f})",
      w_recent > w_old)
check("recency: exactly one half-life -> weight 0.5",
      abs(stats.recency_weight(730, half_life_days=730) - 0.5) < 1e-9)
check("recency: missing age_days -> low floor weight, not full weight",
      stats.recency_weight(None) < 0.5)

check("dispersion_ratio: no divide-by-zero on degenerate all-equal sample",
      stats.dispersion_ratio([100, 100, 100, 100]) == 0.0)
check("dispersion_ratio: single value -> None (not enough data)",
      stats.dispersion_ratio([100]) is None)

med, mad = stats.median_absolute_deviation([100, 105, 110, 115, 500])
check(f"MAD computed without error (median={med}, MAD={mad})", med is not None and mad is not None and mad >= 0)


# =========================================================================
# SECTION 59 — COMPARABLES
# =========================================================================
print()
print("=== §59 Comparables ===")

TARGET = {
    "country_code": "GR", "currency_code": "GRD", "year": 1976,
    "denomination_value": 5, "variants": [], "catalog_ids": {}, "issuer": None, "mintmark": None,
}

def mk(title, **kw):
    kw.setdefault("title", title)
    kw.setdefault("country_code", "GR")
    kw.setdefault("coin_year", 1976)
    kw.setdefault("denomination_value", 5)
    kw.setdefault("hammer_price", 100.0)
    kw.setdefault("price_semantics", PriceSemantics.HAMMER.value)
    kw.setdefault("sold", True)
    return AuctionComparable(**kw)

exact = classify_comparable(TARGET, mk("Greece 5 Drachmai 1976 UNC", grade_raw="UNC"))
check("EXACT: Greece 5 Drachmai 1976 UNC -> EXACT or STRONG tier",
      exact.comparable_tier in (ComparableTier.EXACT.value, ComparableTier.STRONG.value))

reject_denom = classify_comparable(TARGET, mk("Greece 10 Drachmai 1976", denomination_value=10))
check("REJECT: wrong denomination (10 vs 5)", reject_denom.comparable_tier == ComparableTier.REJECT.value)

reject_year = classify_comparable(TARGET, mk("Greece 5 Drachmai 1978", coin_year=1978))
check("REJECT: wrong year (1978 vs 1976)", reject_year.comparable_tier == ComparableTier.REJECT.value)

reject_set = classify_comparable(TARGET, mk("5 Drachmai 1976 coin set"))
check("REJECT: coin set", reject_set.comparable_tier == ComparableTier.REJECT.value)

reject_replica = classify_comparable(TARGET, mk("Greece 5 Drachmai 1976 replica"))
check("REJECT: replica", reject_replica.comparable_tier == ComparableTier.REJECT.value)

strong = classify_comparable(TARGET, mk("Greece 5 Drachmai 1976 XF", grade_raw="XF"))
check(f"STRONG-or-better: same exact issue, plain grade present (tier={strong.comparable_tier})",
      strong.comparable_tier in (ComparableTier.EXACT.value, ComparableTier.STRONG.value))


# =========================================================================
# SECTION 60 — PRICE SEMANTICS
# =========================================================================
print()
print("=== §60 Price Semantics ===")

hammer_comp = AuctionComparable(title="x", hammer_price=400, price_semantics=PriceSemantics.HAMMER.value, sold=True)
check("HAMMER €400 -> effective_price is 400, realized_eligible True",
      hammer_comp.effective_price() == 400 and hammer_comp.realized_eligible())

realized_comp = AuctionComparable(title="x", realized_price=480, price_semantics=PriceSemantics.REALIZED_INCL_PREMIUM.value, sold=True)
check("REALIZED_INCL_PREMIUM €480 must not be silently treated as hammer",
      realized_comp.hammer_price is None and realized_comp.effective_price() == 480)

estimate_comp = AuctionComparable(title="x", estimate_low=400, estimate_high=500, price_semantics=PriceSemantics.ESTIMATE.value)
check("ESTIMATE €400-500 must NOT be realized_eligible (not a sale)",
      not estimate_comp.realized_eligible())

unsold_comp = AuctionComparable(title="x", estimate_low=500, price_semantics=PriceSemantics.ESTIMATE.value, unsold=True)
check("UNSOLD estimate €500 must NOT be realized_eligible",
      not unsold_comp.realized_eligible())

withdrawn_comp = AuctionComparable(title="x", hammer_price=300, price_semantics=PriceSemantics.HAMMER.value, sold=True, withdrawn=True)
check("Withdrawn lot must NOT be realized_eligible even with a hammer price",
      not withdrawn_comp.realized_eligible())

fixed_price_comp = AuctionComparable(title="x", hammer_price=300, price_semantics=PriceSemantics.FIXED_PRICE.value, sold=True)
check("FIXED_PRICE record must NOT be realized_eligible (not an auction sale)",
      not fixed_price_comp.realized_eligible())


# =========================================================================
# GRADE NORMALIZATION
# =========================================================================
print()
print("=== Grade normalization ===")

g1 = grades.normalize_grade("PCGS MS64")
check(f"'PCGS MS64' -> UNC_MID bucket, numeric_grade=64, company=PCGS (got {g1})",
      g1["bucket"] == GradeBucket.UNC_MID.value and g1["numeric_grade"] == 64 and g1["grading_company"] == "PCGS")

g2 = grades.normalize_grade("UNC")
check(f"'UNC' (bare, uncertified) -> bucket assigned, NOT a fabricated MS65 (got {g2})",
      g2["bucket"] == GradeBucket.UNC_MID.value and g2["numeric_grade"] is None)

g3 = grades.normalize_grade("XF details, cleaned")
check(f"'XF details, cleaned' -> DETAILS bucket, is_details True (got {g3})",
      g3["bucket"] == GradeBucket.DETAILS.value and g3["is_details"])

g4 = grades.normalize_grade("VF")
check(f"'VF' -> VF bucket (got {g4})", g4["bucket"] == GradeBucket.VF.value)


# =========================================================================
# DEDUPLICATION
# =========================================================================
print()
print("=== Deduplication ===")

c1 = AuctionComparable(auction_house="Kunker", auction_date="2026-05-12", lot_number="123", title="Same lot")
c2 = AuctionComparable(auction_house="kunker", auction_date="2026-05-12", lot_number="123", title="Same lot via aggregator")
check("Same auction house+date+lot -> identical dedupe key regardless of casing/source",
      c1.compute_dedupe_key() == c2.compute_dedupe_key())

c3 = AuctionComparable(auction_house="Heritage", auction_date="2026-05-12", lot_number="999", title="Different lot")
check("Different lot number -> different dedupe key", c1.compute_dedupe_key() != c3.compute_dedupe_key())


# =========================================================================
# MANUAL / CSV ADAPTERS (§7)
# =========================================================================
print()
print("=== Manual/CSV adapters ===")

manual = ManualComparableAdapter()
legacy_text = "430\n455\n410\n"
legacy_results = manual.parse(legacy_text)
check(f"legacy one-number-per-line mode still works ({[c.hammer_price for c in legacy_results]})",
      [c.hammer_price for c in legacy_results] == [430.0, 455.0, 410.0])
check("legacy entries default to HAMMER semantics",
      all(c.price_semantics == PriceSemantics.HAMMER.value for c in legacy_results))

structured_text = "2026-05-12 | 430 | EUR | MS64 | Künker | https://example.com/lot1\n2026-03-08 | 455 | EUR | MS65 | Heritage | https://example.com/lot2"
structured_results = manual.parse(structured_text)
check(f"structured Date|Hammer|Currency|Grade|House|URL parsing works ({len(structured_results)} rows)",
      len(structured_results) == 2 and structured_results[0].hammer_price == 430.0 and structured_results[0].auction_house == "Künker")

mixed_text = "430\n2026-05-12 | 455 | EUR | MS64 | Künker | https://example.com/lot1"
mixed_results = manual.parse(mixed_text)
check("mixed legacy+structured lines in the same textarea both parse",
      len(mixed_results) == 2)

csv_adapter = CSVComparableAdapter()
csv_text = "date,hammer,currency,grade,auction_house,url\n2026-05-12,430,EUR,MS64,Kunker,https://x\n2026-03-08,455,EUR,MS65,Heritage,https://y\n"
csv_results = csv_adapter.parse(csv_text)
check(f"CSV import parses expected rows ({len(csv_results)})", len(csv_results) == 2)

try:
    csv_adapter.parse("date,currency\n2026-05-12,EUR\n")
    check("CSV missing required 'hammer' column raises", False)
except ValueError:
    check("CSV missing required 'hammer' column raises", True)

check("only Manual/CSV adapters are enabled (no scraping adapters active)",
      {a.name for a in get_enabled_adapters()} == {"manual", "csv"})


# =========================================================================
# SUMMARY
# =========================================================================
print()
passed = sum(1 for _, ok in RESULTS if ok)
total = len(RESULTS)
print(f"TOTAL: {passed}/{total}")
if passed != total:
    print("FAILURES:", [n for n, ok in RESULTS if not ok])
    sys.exit(1)
