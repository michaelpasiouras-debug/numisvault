"""
CoinBids Auction Intelligence 3.0 — FX normalization.

Uses Frankfurter/ECB-compatible rates. FX failures must never crash valuation;
failed normalization leaves the comparable in its original currency.
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
    if amount is None or rate is None:
        return None
    return round(amount * rate, 2)


def normalize_amount(amount: Optional[float], from_currency: Optional[str], to_currency: str,
                      on_date: Optional[date] = None,
                      http_get: Callable[[str], dict] = _default_http_get) -> dict:
    if amount is None or not from_currency or not to_currency:
        return {"normalized_price": None, "fx_source": None, "fx_date": None, "fx_confidence": None}
    from_currency = from_currency.upper().strip()
    to_currency = to_currency.upper().strip()
    if from_currency == to_currency:
        return {"normalized_price": round(amount, 2), "fx_source": "same_currency",
                "fx_date": on_date.isoformat() if on_date else None,
                "fx_confidence": "auction_date" if on_date else "current_fallback"}
    try:
        rate_info = fetch_rate(from_currency, to_currency, on_date, http_get=http_get)
    except FXFetchError:
        return {"normalized_price": None, "fx_source": None, "fx_date": None, "fx_confidence": None}
    normalized = convert(amount, rate_info["rate"])
    confidence = "auction_date" if rate_info["is_exact_date"] else "current_fallback"
    return {"normalized_price": normalized, "fx_source": "frankfurter_ecb",
            "fx_date": rate_info["date_used"], "fx_confidence": confidence}


def normalize_comparable(comp, target_currency: str, http_get: Callable[[str], dict] = _default_http_get):
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

    # BUG 8 fix: top-level safety boundary for the batch-normalization entry
    # point. Any timeout/5xx/malformed-response/unexpected normalization error
    # becomes a safe no-op for this comparable instead of bubbling into HTTP 500.
    try:
        result = normalize_amount(amount, comp.currency, target_currency, on_date, http_get=http_get)
        comp.normalized_price = result["normalized_price"]
        comp.fx_source = result["fx_source"]
        comp.fx_date = result["fx_date"]
        comp.fx_confidence = result["fx_confidence"]
    except Exception:
        comp.normalized_price = None
        comp.fx_source = None

    return comp


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
