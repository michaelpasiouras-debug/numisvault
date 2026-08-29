#!/usr/bin/env python3
"""Regression tests for /api/coin-search cache isolation.

A normal UI request must never collide with QA full-evidence requests, and
price-forming identity/shipping inputs must produce distinct cache keys.
"""
import importlib

b=importlib.import_module("numisvault_backend")

BASE={
    "raw_query":"Greece 10 euro 2022 antikythera mechanism",
    "coin":{"raw":"Greece 10 euro 2022 antikythera mechanism","country":"Greece","denom":10,"year":2022},
    "currency":"EUR","include_shipping":True,"ship_to":"Greece",
}

def key(**updates):
    p={**BASE,**updates}
    if "coin" not in updates:
        p["coin"]=dict(BASE["coin"])
    return b._search_cache_key(p)

def assert_ne(label,a,c):
    if a==c:
        raise AssertionError(f"cache collision: {label}\n{a}")

def main():
    normal=key(limit=2,sample_limit=10,qa_full_evidence=False)
    qa=key(limit=200,sample_limit=15,qa_full_evidence=True)
    assert_ne("normal UI vs full QA evidence",normal,qa)

    assert_ne("limit",key(limit=2),key(limit=200))
    assert_ne("sample_limit",key(sample_limit=10),key(sample_limit=15))
    assert_ne("qa_full_evidence",key(qa_full_evidence=False),key(qa_full_evidence=True))
    assert_ne("weight",key(weight_g=8.0),key(weight_g=31.0))
    assert_ne("destination",key(ship_to="Greece"),key(ship_to="Germany"))

    corrected=dict(BASE["coin"]); corrected["denom"]=2
    assert_ne("structured identity with same raw query",key(coin=BASE["coin"]),key(coin=corrected))

    print("PASS — coin-search cache keys isolate response shape, identity and shipping inputs")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
