"""
CoinBids Auction Intelligence 3.0 — FX normalization tests.
Run: python3 test_auction_fx.py

CRITICAL: all tests use an injected FAKE http_get function — none of these
tests make a real network call to api.frankfurter.app. This verifies the
request-building, caching, conversion, and fallback LOGIC is correct. It
does NOT verify that api.frankfurter.app actually responds as documented —
that must be confirmed separately once this runs somewhere with open
internet access (e.g. Render), since this sandboxed dev environment cannot
reach that domain. See the module docstring in auction_fx.py.
"""
import sys
from datetime import date
sys.path.insert(0, ".")

import auction_fx as fx
from auction_models import AuctionComparable, PriceSemantics

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + ": " + name)


# A fake Frankfurter response, shaped exactly like the real documented API
# (verified via live web search of api.frankfurter.app's own docs):
# {"amount":1.0,"base":"USD","date":"2026-05-12","rates":{"EUR":0.867}}
CALL_LOG = []

def fake_http_get_exact(url):
    CALL_LOG.append(url)
    return {"amount": 1.0, "base": "USD", "date": "2026-05-12", "rates": {"EUR": 0.867}}

def fake_http_get_weekend_fallback(url):
    # Simulates Frankfurter's real documented behavior: a weekend/holiday
    # date request returns the NEAREST EARLIER business day in the "date"
    # field rather than the requested date.
    return {"amount": 1.0, "base": "USD", "date": "2026-05-08", "rates": {"EUR": 0.865}}

def fake_http_get_missing_currency(url):
    return {"amount": 1.0, "base": "USD", "date": "2026-05-12", "rates": {"GBP": 0.75}}

def fake_http_get_network_error(url):
    raise ConnectionError("simulated network failure")


print("=== fetch_rate: request construction & response parsing ===")
CALL_LOG.clear()
r = fx.fetch_rate("USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_exact)
check(f"correct rate parsed from response (got {r['rate']})", r["rate"] == 0.867)
check(f"exact-date match correctly detected (got is_exact_date={r['is_exact_date']})", r["is_exact_date"] is True)
check(f"URL built with ISO date and currency codes (got {CALL_LOG[0]})",
      CALL_LOG[0] == "https://api.frankfurter.app/2026-05-12?from=USD&to=EUR")

print()
print("=== fetch_rate: weekend/holiday fallback honesty ===")
r2 = fx.fetch_rate("USD", "EUR", date(2026, 5, 10), http_get=fake_http_get_weekend_fallback)  # a Sunday
check(f"fallback date correctly flagged as NOT exact (got is_exact_date={r2['is_exact_date']})", r2["is_exact_date"] is False)
check(f"date_used reflects the actual date Frankfurter used, not the requested one (got {r2['date_used']})",
      r2["date_used"] == "2026-05-08")

print()
print("=== fetch_rate: same currency short-circuits without any HTTP call ===")
CALL_LOG.clear()
r3 = fx.fetch_rate("EUR", "EUR", date(2026, 5, 12), http_get=fake_http_get_exact)
check(f"same-currency rate is 1.0, no HTTP call made (rate={r3['rate']}, calls={len(CALL_LOG)})",
      r3["rate"] == 1.0 and len(CALL_LOG) == 0)

print()
print("=== fetch_rate: missing target currency in response -> honest error, not a guess ===")
fx._RATE_CACHE.clear()
try:
    fx.fetch_rate("USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_missing_currency)
    check("missing currency raises FXFetchError", False)
except fx.FXFetchError:
    check("missing currency raises FXFetchError", True)

print()
print("=== fetch_rate: network failure -> honest error, not a guess ===")
fx._RATE_CACHE.clear()
try:
    fx.fetch_rate("USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_network_error)
    check("network failure raises FXFetchError", False)
except fx.FXFetchError:
    check("network failure raises FXFetchError", True)
fx._RATE_CACHE.clear()

print()
print("=== fetch_rate: caching avoids repeated HTTP calls for the same (pair, date) ===")
fx._RATE_CACHE.clear()
CALL_LOG.clear()
fx.fetch_rate("USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_exact)
fx.fetch_rate("USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_exact)
fx.fetch_rate("USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_exact)
check(f"3 identical requests -> only 1 real HTTP call (got {len(CALL_LOG)})", len(CALL_LOG) == 1)
fx._RATE_CACHE.clear()

print()
print("=== convert(): pure arithmetic ===")
check("convert 100 USD at rate 0.867 -> 86.70", fx.convert(100, 0.867) == 86.70)
check("convert with None amount -> None", fx.convert(None, 0.867) is None)
check("convert with None rate -> None", fx.convert(100, None) is None)


print()
print("=== normalize_amount(): high-level entry point ===")
res = fx.normalize_amount(400, "USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_exact)
check(f"normalized_price computed correctly ({res['normalized_price']})", res["normalized_price"] == round(400 * 0.867, 2))
check("fx_confidence 'auction_date' for exact-date match", res["fx_confidence"] == "auction_date")
check("fx_source correctly attributed to frankfurter_ecb", res["fx_source"] == "frankfurter_ecb")

fx._RATE_CACHE.clear()
res_fail = fx.normalize_amount(400, "USD", "EUR", date(2026, 5, 12), http_get=fake_http_get_network_error)
check("network failure -> safe no-op (normalized_price None, not crashed)", res_fail["normalized_price"] is None)
fx._RATE_CACHE.clear()

res_same = fx.normalize_amount(400, "EUR", "EUR", date(2026, 5, 12), http_get=fake_http_get_exact)
check("same currency -> passthrough, no FX call needed", res_same["normalized_price"] == 400.0 and res_same["fx_source"] == "same_currency")


print()
print("=== normalize_comparable(): mutates an AuctionComparable correctly ===")
c = AuctionComparable(title="test", hammer_price=400, currency="USD", price_semantics=PriceSemantics.HAMMER.value,
                       auction_date="2026-05-12", sold=True)
fx.normalize_comparable(c, "EUR", http_get=fake_http_get_exact)
check(f"comparable's normalized_price set ({c.normalized_price})", c.normalized_price == round(400 * 0.867, 2))
check("original_price/original_currency preserved", c.original_price == 400 and c.original_currency == "USD")
check("effective_price() prefers the normalized (FX-converted) price", c.effective_price() == c.normalized_price)

c_no_currency = AuctionComparable(title="test", hammer_price=400, price_semantics=PriceSemantics.HAMMER.value, sold=True)
fx.normalize_comparable(c_no_currency, "EUR", http_get=fake_http_get_exact)
check("missing currency -> safe no-op, doesn't crash", c_no_currency.normalized_price is None)


print()
passed = sum(1 for _, ok in RESULTS if ok)
total = len(RESULTS)
print(f"TOTAL: {passed}/{total}")
if passed != total:
    print("FAILURES:", [n for n, ok in RESULTS if not ok])
    sys.exit(1)
