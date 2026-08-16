"""
CoinBids Auction Intelligence 3.0 — valuation engine & bid advisor tests.
Run: python3 test_auction_valuation.py
All inputs are clearly-synthetic numbers used to test the MATH, not claims
about any real coin or real auction — per spec §58-63's own testing model.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from auction_models import AuctionComparable, PriceSemantics, ComparableTier
from auction_matching import classify_comparable
import auction_valuation as val
import auction_bid_advisor as bid

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


def mk_realized(price, days_ago, house, tier_score=95, semantics=PriceSemantics.HAMMER.value):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    c = AuctionComparable(title=f"Comparable {price}", auction_house=house, auction_date=d,
                           hammer_price=price, price_semantics=semantics, sold=True)
    c.identity_match_score = tier_score
    c.comparable_tier = ComparableTier.EXACT.value if tier_score >= 90 else ComparableTier.STRONG.value
    c.match_reasons = ["synthetic test fixture"]
    return c


# =========================================================================
# §61 FAIR VALUE FUSION
# =========================================================================
print("=== §61 Fair Value Fusion ===")

# Dealer median = 500, Realized median = 400, 10 good realized -> Fair Value
# should be close to realized, not 450/500.
comps_10 = [mk_realized(390 + i * 5, 30 * (i + 1), f"House{i%3}") for i in range(10)]
realized = val.realized_market_summary(comps_10)
dealer = val.dealer_market_summary([490, 500, 510, 495, 505])
weights = val.fusion_weights(realized)
fair = val.fair_value_range(realized, dealer, weights)
check(f"10 good realized (median~{realized['weighted_median']}) dominates dealer(500) fusion (central={fair['fair_central']}, weight={weights['realized_weight']})",
      weights["realized_weight"] >= 0.85 and fair["fair_central"] < 460)

# 0 realized -> dealer-only, confidence capped LOW
realized_0 = val.realized_market_summary([])
weights_0 = val.fusion_weights(realized_0)
fair_0 = val.fair_value_range(realized_0, dealer, weights_0)
conf_0 = val.confidence_score(0.95, realized_0)
check(f"0 realized -> dealer-only fusion (realized_weight={weights_0['realized_weight']})",
      weights_0["realized_weight"] == 0.0 and fair_0["fair_central"] == dealer["median"])
check(f"0 realized -> confidence capped LOW (got {conf_0['label']}/{conf_0['score']})",
      conf_0["label"] in ("LOW", "VERY LOW") and conf_0["score"] < 40)

# 1 realized -> must not claim robust auction distribution
comps_1 = [mk_realized(400, 60, "SoleHouse")]
realized_1 = val.realized_market_summary(comps_1)
conf_1 = val.confidence_score(0.95, realized_1)
check(f"1 realized -> confidence capped LOW, not robust (got {conf_1['label']})",
      conf_1["label"] == "LOW" and "n=1" in realized_1["sample_note"])


# =========================================================================
# §62 LIVE BID
# =========================================================================
print()
print("=== §62 Live Bid ===")

snapshot = val.compute_valuation_snapshot(
    identity={"country": "Greece", "denomination_value": 5, "year": 1976},
    identity_quality=0.95,
    comparables=comps_10,
    dealer_sample_prices=[490, 500, 510, 495, 505],
)
check(f"snapshot built with a real fair_central ({snapshot['fusion']['fair_central']})",
      snapshot["fusion"]["fair_central"] is not None)

advice = bid.bid_advice(snapshot, "320", buyer_premium_pct=20, shipping=15, fixed_fees=5)
expected_all_in = 320 * 1.20 + 15 + 5
check(f"current_all_in matches manual calc (got {advice['current_all_in']}, expect {round(expected_all_in,2)})",
      abs(advice["current_all_in"] - expected_all_in) < 0.01)

check("value_buy_max_hammer < fair_buy_max_hammer < collector_max_hammer (ceilings ordered correctly)",
      advice["value_buy_max_hammer"] is not None
      and advice["value_buy_max_hammer"] <= advice["fair_buy_max_hammer"] <= advice["collector_max_hammer"])

empty_advice = bid.bid_advice(snapshot, "", buyer_premium_pct=20, shipping=15, fixed_fees=5)
check("empty bid -> no verdict", empty_advice["recommendation"] is None and "Enter the current hammer bid" in empty_advice["reason"])

neg_advice = bid.bid_advice(snapshot, "-50", buyer_premium_pct=20)
check("negative bid -> no verdict", neg_advice["recommendation"] is None)

zero_advice = bid.bid_advice(snapshot, "0", buyer_premium_pct=20)
check("zero bid -> no verdict", zero_advice["recommendation"] is None)

nan_advice = bid.bid_advice(snapshot, "abc", buyer_premium_pct=20)
check("non-numeric bid -> no verdict, does not crash", nan_advice["recommendation"] is None)

zero_prem_advice = bid.bid_advice(snapshot, "320", buyer_premium_pct=0, shipping=0, fixed_fees=0)
check(f"buyer premium 0 works (all_in == hammer, got {zero_prem_advice['current_all_in']})",
      zero_prem_advice["current_all_in"] == 320.0)

low_prem = bid.bid_advice(snapshot, "320", buyer_premium_pct=5, shipping=0, fixed_fees=0)
high_prem = bid.bid_advice(snapshot, "320", buyer_premium_pct=50, shipping=0, fixed_fees=0)
check(f"large premium decreases hammer ceilings appropriately (low_prem fair_hammer={low_prem['fair_buy_max_hammer']:.1f} > high_prem={high_prem['fair_buy_max_hammer']:.1f})",
      low_prem["fair_buy_max_hammer"] > high_prem["fair_buy_max_hammer"])

# recommendation ordering sanity: cheap bid should be at least as good a
# recommendation tier as an expensive one, for the same snapshot.
cheap = bid.bid_advice(snapshot, "50", buyer_premium_pct=10)
expensive = bid.bid_advice(snapshot, "2000", buyer_premium_pct=10)
order = ["STRONG BUY", "BUY", "FAIR", "CAUTION", "STOP", "OVERPAY"]
check(f"cheap bid recommendation ({cheap['recommendation']}) ranked at least as good as expensive bid ({expensive['recommendation']})",
      order.index(cheap["recommendation"]) <= order.index(expensive["recommendation"]))


# =========================================================================
# Demand / Trend — sanity checks (no invented data; pure function tests)
# =========================================================================
print()
print("=== Demand / Trend sanity ===")

d = val.demand_score(comparables_last_12m=8, comparables_last_24m=10, houses=4,
                      latest_sale=date.today().isoformat(), dispersion=0.05)
check(f"demand_score with strong recent activity -> HIGH (got {d})", d["label"] == "HIGH")

d0 = val.demand_score(comparables_last_12m=0, comparables_last_24m=0, houses=0, latest_sale=None, dispersion=None)
check(f"demand_score with zero activity -> LOW, score 0 (got {d0})", d0["label"] == "LOW" and d0["score"] == 0)

tr_insufficient = val.trend([mk_realized(400, 10, "H1")])
check(f"trend with 1 comparable -> INSUFFICIENT_DATA (got {tr_insufficient['label']})",
      tr_insufficient["label"] == "INSUFFICIENT_DATA")

# Rising trend: older half cheaper, recent half pricier, spanning >180 days
rising_comps = ([mk_realized(300, 400 + i * 10, f"H{i}") for i in range(4)]
                + [mk_realized(400, 20 + i * 10, f"H{i}") for i in range(4)])
tr_rising = val.trend(rising_comps)
check(f"synthetic rising price series -> RISING trend (got {tr_rising})", tr_rising["label"] == "RISING")


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
