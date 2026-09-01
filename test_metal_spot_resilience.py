#!/usr/bin/env python3
"""Offline regression checks for Metal Value spot-price resilience."""
import importlib
from unittest.mock import patch

b=importlib.import_module("numisvault_backend")


class FakeResponse:
    def __init__(self, price):
        self.status_code=200
        self._price=price

    def raise_for_status(self):
        return None

    def json(self):
        return {"price":self._price}


def run():
    b.app.testing=True
    with b._METAL_SPOT_CACHE_LOCK:
        b._METAL_SPOT_CACHE.update(at=0.0,data=None)

    def live_get(url, **kwargs):
        return FakeResponse(4300.0 if url.endswith("XAU") else 64.0)

    with patch.object(b.requests,"get",side_effect=live_get), patch.object(b,"fx_rates",return_value={"EUR":1.0,"USD":1.16}):
        first=b.app.test_client().get("/api/metal-spot")
    assert first.status_code==200,first.get_data(as_text=True)
    data=first.get_json()
    assert data["silver_usd_oz"]==64.0 and data["usd_to_eur"]>0

    # A subsequent provider outage must use the last verified quote rather
    # than blanking melt value in the UI.
    with b._METAL_SPOT_CACHE_LOCK:
        b._METAL_SPOT_CACHE["at"]-=b._METAL_SPOT_FRESH_SECONDS+1
    with patch.object(b.requests,"get",side_effect=RuntimeError("provider down")), patch.object(b.SESSION,"get",side_effect=RuntimeError("fallback down")):
        second=b.app.test_client().get("/api/metal-spot")
    assert second.status_code==200,second.get_data(as_text=True)
    stale=second.get_json()
    assert stale["cache"]=="stale" and stale["silver_usd_oz"]==64.0
    print("PASS metal spot live quote and stale-cache fallback")
    return 0


if __name__=="__main__":raise SystemExit(run())
