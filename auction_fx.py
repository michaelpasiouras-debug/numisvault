"""
CoinBids Auction Intelligence 3.0 — FX normalization (spec: normalized_price/
fx_source/fx_confidence fields on AuctionComparable).

Uses the Frankfurter API (https://api.frankfurter.app) — a free, keyless,
open-source service serving official European Central Bank (ECB) daily
reference rates for 30+ currencies, going back to 1999. Verified via live
fetches of the project's own current documentation (https://frankfurter.dev/v1/)
and of the live API itself, confirming the response format:
    GET https://api.frankfurter.app/{YYYY-MM-DD}?base=USD&symbols=EUR
    -> {"amount":1.0,"base":"USD","date":"2026-05-12","rates":{"EUR":0.867}}
IMPORTANT CORRECTION (found during a later verification pass): earlier
research had this module requesting `from=`/`to=` query parameters, based on
several third-party tutorial/blog posts using that older naming. A live
fetch of the CURRENT official docs (https://frankfurter.dev/v1/) showed the
real parameter names are `base=`/`symbols=` instead — `from=`/`to=` are
silently ignored by today's API rather than erroring, which is exactly the
dangerous kind of bug that doesn't announce itself: for some currency pairs
it would have quietly returned a different (EUR-base) rate mislabeled as the
requested pair, instead of failing loudly. Fixed to use `base=`/`symbols=`.
On a weekend/holiday date, Frankfurter returns the nearest earlier business
day's rate and reflects that in the response's own "date" field — this
module reads that field back to set fx_confidence honestly (exact date vs.
nearest-available fallback), rather than assuming an exact match.

IMPORTANT — testing note: the code in this module (its `requests`-based
`_default_http_get`) could not be exercised from the sandboxed bash
environment this module was written in — api.frankfurter.app is not on that
sandbox's network allow-list (only github.com/pypi.org/npmjs.com-family
domains are reachable from there). However, the API's response FORMAT and
correct query parameters were independently verified via a separate web-
fetch tool with its own network access: a live GET to
https://api.frankfurter.app/latest returned a real, current JSON payload
matching exactly what this module expects to parse — see the module
docstring above. What remains genuinely unverified from this development
environment is specifically the `requests` HTTP call path (network library,
timeout handling, TLS) as opposed to the API contract itself, which now has
real evidence behind it. Everything else — request URL construction,
response parsing, caching, conversion arithmetic, and fallback/error
handling — is covered by real unit tests using an injected fake HTTP layer
(see test_auction_fx.py). If the live `requests` call fails for any reason
once deployed, normalization safely no-ops (see FALLBACK behavior below)
rather than crashing anything.
"""
from __future__ import annotations
import threading
from datetime import date, datetime
from typing import Callable, Optional

try:
    import requests as _requests
except Exception:
    _requests = None

FRANKFURTER_BASE = "https://api.frankfurter.app"
_TIMEOUT_SECONDS = 6

# Simple in-memory cache: (from_ccy, to_ccy, date_str) -> rate. FX rates for a
# past date never change, so this cache never needs to expire for historical
# lookups. "latest" lookups are cached under the special key date_str="latest"
# and are refreshed once per process restart — acceptable for this use case
# (coin valuations are not high-frequency trading).
_RATE_CACHE = {}
_CACHE_LOCK = threading.Lock()


class FXFetchError(Exception):
    pass


def _default_http_get(url: str) -> dict:
    if _requests is None:
        raise FXFetchError("requests library not available")
    resp = _requests.get(url, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def fetch_rate(from_currency: str, to_currency: str, on_date: Optional[date] = None,
                http_get: Callable[[str], dict] = _default_http_get) -> dict:
    """Returns {"rate": float, "date_used": "YYYY-MM-DD", "requested_date":
    "YYYY-MM-DD"|None, "is_exact_date": bool}. `http_get` is injectable so
    this function is fully unit-testable without real network access — see
    test_auction_fx.py, which passes a fake http_get and never hits the
    network.

    Never fabricates a rate: if the request fails or the response is
    malformed, this raises FXFetchError rather than returning a guessed
    value — callers (normalize_comparable) must decide how to handle that
    (currently: skip normalization for that record, leaving
    normalized_price as None rather than a wrong number)."""
    from_currency = (from_currency or "").upper().strip()
    to_currency = (to_currency or "").upper().strip()
    if not from_currency or not to_currency:
        raise FXFetchError("from_currency and to_currency are required")
    if from_currency == to_currency:
        return {"rate": 1.0, "date_used": (on_date or date.today()).isoformat(),
                "requested_date": on_date.isoformat() if on_date else None, "is_exact_date": True}

    date_key = on_date.isoformat() if on_date else "latest"
    cache_key = (from_currency, to_currency, date_key)
    with _CACHE_LOCK:
        cached = _RATE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    path = on_date.isoformat() if on_date else "latest"
    # Query parameter names verified against the CURRENT official Frankfurter
    # docs (fetched live: https://frankfurter.dev/v1/ — "Change the base
    # currency using the base parameter... Limit the response to specific
    # target currencies" with symbols=...). Numerous third-party tutorial/
    # blog posts show an older from=/to= naming instead; those were not
    # trusted here since the project's own current documentation takes
    # precedence over unofficial secondary sources, especially for something
    # this easy to silently get wrong (a from/to mismatch wouldn't always
    # error — for some currency pairs it can silently return a DIFFERENT,
    # wrong base's rates without raising, since the target currency is often
    # present in the default EUR-base response too).
    url = f"{FRANKFURTER_BASE}/{path}?base={from_currency}&symbols={to_currency}"
    try:
        data = http_get(url)
    except Exception as e:
        raise FXFetchError(f"FX request failed: {e}") from e

    rates = data.get("rates") or {}
    if to_currency not in rates:
        raise FXFetchError(f"No rate for {to_currency} in Frankfurter response")
    rate = float(rates[to_currency])
    date_used = data.get("date") or date_key
    is_exact = bool(on_date) and (date_used == on_date.isoformat())

    result = {"rate": rate, "date_used": date_used,
              "requested_date": on_date.isoformat() if on_date else None,
              "is_exact_date": is_exact}
    with _CACHE_LOCK:
        _RATE_CACHE[cache_key] = result
    return result


def convert(amount: Optional[float], rate: Optional[float]) -> Optional[float]:
    """Pure arithmetic — no I/O. Kept separate from fetch_rate so the actual
    conversion math is trivially, deterministically testable."""
    if amount is None or rate is None:
        return None
    return round(amount * rate, 2)


def normalize_amount(amount: Optional[float], from_currency: Optional[str], to_currency: str,
                      on_date: Optional[date] = None,
                      http_get: Callable[[str], dict] = _default_http_get) -> dict:
    """High-level entry point: fetch the rate (with caching) and convert in
    one call. Returns a dict matching AuctionComparable's FX fields:
    {"normalized_price", "fx_source", "fx_date", "fx_confidence"}. On any
    failure, returns all-None fields (safe no-op) rather than raising past
    the boundary of this function — a bad FX lookup for one comparable must
    never crash the whole valuation snapshot."""
    if amount is None or not from_currency or not to_currency:
        return {"normalized_price": None, "fx_source": None, "fx_date": None, "fx_confidence": None}
    from_currency = from_currency.upper().strip()
    to_currency = to_currency.upper().strip()
    if from_currency == to_currency:
        return {"normalized_price": round(amount, 2), "fx_source": "same_currency",
                "fx_date": on_date.isoformat() if on_date else None, "fx_confidence": "auction_date" if on_date else "current_fallback"}
    try:
        rate_info = fetch_rate(from_currency, to_currency, on_date, http_get=http_get)
    except FXFetchError:
        return {"normalized_price": None, "fx_source": None, "fx_date": None, "fx_confidence": None}
    normalized = convert(amount, rate_info["rate"])
    confidence = "auction_date" if rate_info["is_exact_date"] else "current_fallback"
    return {"normalized_price": normalized, "fx_source": "frankfurter_ecb",
            "fx_date": rate_info["date_used"], "fx_confidence": confidence}


def normalize_comparable(comp, target_currency: str, http_get: Callable[[str], dict] = _default_http_get):
    """Mutates and returns an AuctionComparable in place, filling
    original_price/original_currency/normalized_price/fx_date/fx_source/
    fx_confidence. Uses hammer_price or realized_price (whichever the
    comparable's price_semantics indicates) as the amount to convert; leaves
    everything untouched (safe no-op) if the comparable has no usable price
    or currency."""
    amount = comp.hammer_price if comp.price_semantics == "HAMMER" else (
        comp.realized_price if comp.price_semantics == "REALIZED_INCL_PREMIUM" else None)
    if amount is None or not comp.currency:
        return comp
    comp.original_price = amount
    comp.original_currency = comp.currency.upper()
    if comp.original_currency == target_currency.upper():
        comp.normalized_price = round(amount, 2)
        comp.fx_source = "same_currency"
        comp.fx_confidence = "auction_date"
        return comp
    on_date = _parse_date(comp.auction_date)
    result = normalize_amount(amount, comp.currency, target_currency, on_date, http_get=http_get)
    comp.normalized_price = result["normalized_price"]
    comp.fx_source = result["fx_source"]
    comp.fx_date = result["fx_date"]
    comp.fx_confidence = result["fx_confidence"]
    return comp


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
