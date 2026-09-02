#!/usr/bin/env python3
"""Regression checks for database-first, write-through coin specifications."""
import importlib
from unittest.mock import patch

b=importlib.import_module("numisvault_backend")


COIN={"countryEN":"Greece","denom":"10 euro","year":"2022","variant":"Mechanism"}
COMPLETE={
    "composition":"Silver (.925)","primary_metal":"silver",
    "fineness_per_mille":925.0,"weight_g":34.1,"fine_metal_g":31.5425,
    "match_class":"LOCAL_SPEC_PG",
}


def run():
    b.app.testing=True
    client=b.app.test_client()

    # A sufficient PostgreSQL hit must prevent every external catalogue call.
    with patch.object(b,"pg_coin_spec_match",return_value=COMPLETE), \
         patch.object(b,"local_coin_spec_match") as local, \
         patch.object(b,"mashops_spec_fallback") as ma, \
         patch.object(b,"numista_search") as numista:
        response=client.post("/api/coin-lookup",json={"coin":COIN})
    assert response.status_code==200,response.get_data(as_text=True)
    assert response.get_json()["provider"]=="local_pg",response.get_json()
    local.assert_not_called();ma.assert_not_called();numista.assert_not_called()

    # A miss is enriched from MA-Shops and written through immediately.
    discovered=dict(COMPLETE,source_url="https://example.test/validated-listing")
    with patch.object(b,"pg_coin_spec_match",return_value=None), \
         patch.object(b,"local_coin_spec_match",return_value=None), \
         patch.object(b,"mashops_spec_fallback",return_value=discovered), \
         patch.object(b,"persist_mashops_spec",return_value=True) as persist, \
         patch.object(b,"numista_search") as numista:
        response=client.post("/api/coin-lookup",json={"coin":COIN})
    data=response.get_json()
    assert response.status_code==200,response.get_data(as_text=True)
    assert data["provider"]=="ma_shops" and data["catalogue_enriched"] is True,data
    persist.assert_called_once_with(COIN,discovered)
    numista.assert_not_called()

    print("PASS coin specifications are database-first with MA-Shops write-through")
    return 0


if __name__=="__main__":raise SystemExit(run())
