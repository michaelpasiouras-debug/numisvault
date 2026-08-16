"""
CoinBids Auction Intelligence 3.0 — Sell Engine tests.
Run: python3 test_auction_sell_engine.py
"""
import sys
sys.path.insert(0, ".")

import auction_sell_engine as sell

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


print("=== Estimate range ===")
est_high_conf = sell.estimate_range(365, 414, 434, "HIGH")
est_low_conf = sell.estimate_range(365, 414, 434, "LOW")
check(f"lower confidence widens estimate range (HIGH span={est_high_conf['estimate_high']-est_high_conf['estimate_low']:.1f} < LOW span={est_low_conf['estimate_high']-est_low_conf['estimate_low']:.1f})",
      (est_high_conf["estimate_high"] - est_high_conf["estimate_low"]) < (est_low_conf["estimate_high"] - est_low_conf["estimate_low"]))

est_none = sell.estimate_range(None, None, None, "LOW")
check("no fair value data -> no fabricated estimate", est_none["estimate_central"] is None)


print()
print("=== Reserve recommendation ===")
reserve_high = sell.reserve_recommendation(365, "HIGH")
reserve_low = sell.reserve_recommendation(365, "LOW")
check(f"lower confidence -> more conservative (lower) reserve ({reserve_high['reserve']} > {reserve_low['reserve']})",
      reserve_high["reserve"] > reserve_low["reserve"])
check("reserve is never above the low estimate", reserve_high["reserve"] <= 365)

starting = sell.starting_bid_recommendation(300)
check(f"starting bid is a fraction of reserve, below it ({starting['starting_bid']} < 300)",
      starting["starting_bid"] < 300)


print()
print("=== Seller net proceeds ===")
net = sell.seller_net_proceeds(expected_hammer=400, seller_commission_pct=15, insurance_pct=1,
                                photography_fee=5, listing_fee=3)
expected_net = 400 - (400*0.15) - (400*0.01) - 5 - 3
check(f"net proceeds arithmetic correct (got {net['net_proceeds']}, expect {round(expected_net,2)})",
      abs(net["net_proceeds"] - expected_net) < 0.01)
check("net proceeds is always less than expected hammer when any fees apply",
      net["net_proceeds"] < net["expected_hammer"])

net_zero_hammer = sell.seller_net_proceeds(None, 15)
check("None hammer -> no fabricated net figure", net_zero_hammer["net_proceeds"] is None)

net_zero_fees = sell.seller_net_proceeds(expected_hammer=400, seller_commission_pct=0)
check("zero commission -> net proceeds equals hammer", net_zero_fees["net_proceeds"] == 400.0)


print()
print("=== Full sell_advice pipeline ===")
snapshot = {
    "fusion": {"fair_low": 365.33, "fair_central": 413.82, "fair_high": 433.95},
    "confidence": {"label": "MEDIUM", "score": 82},
    "realized_market": {"count": 3},
}
advice = sell.sell_advice(snapshot, seller_commission_pct=15, insurance_pct=1, photography_fee=5)
check("sell_advice produces a coherent estimate/reserve/starting-bid/net chain",
      advice["estimate"]["estimate_central"] is not None
      and advice["reserve"]["reserve"] is not None
      and advice["starting_bid"]["starting_bid"] is not None
      and advice["net_proceeds"]["at_central_estimate"]["net_proceeds"] is not None)
check("starting_bid < reserve < estimate_low (sane ordering)",
      advice["starting_bid"]["starting_bid"] < advice["reserve"]["reserve"] <= advice["estimate"]["estimate_low"])
check("net proceeds at high estimate > net proceeds at low estimate",
      advice["net_proceeds"]["at_high_estimate"]["net_proceeds"] > advice["net_proceeds"]["at_low_estimate"]["net_proceeds"])

no_evidence_snapshot = {"fusion": {"fair_low": None, "fair_central": None, "fair_high": None},
                         "confidence": {"label": "LOW"}, "realized_market": {"count": 0}}
advice_none = sell.sell_advice(no_evidence_snapshot, seller_commission_pct=15)
check("no evidence -> no fabricated sell advice", advice_none["estimate"]["estimate_central"] is None)


print()
passed = sum(1 for _, ok in RESULTS if ok)
total = len(RESULTS)
print(f"TOTAL: {passed}/{total}")
if passed != total:
    print("FAILURES:", [n for n, ok in RESULTS if not ok])
    sys.exit(1)
