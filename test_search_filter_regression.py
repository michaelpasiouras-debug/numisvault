#!/usr/bin/env python3
"""
COINBIDS — PRICE RESEARCH IDENTITY / SHIPPING REGRESSION SUITE
==============================================================

Permanent regression coverage for the 2026-08-24 production failure where:

  "5 drachma 1901" / "5 δραχμές 1901"

could accept:
  - Germany 1901 J 5 Pfennig VF
  - a 1901 German postcard

Root cause: the frontend resolver path converted the target into the
human-readable label "5 Greek drachma". The backend's old target parser could
not parse that form, and denomination_matches() treated an unparseable target
as if NO denomination constraint existed.

Run:
    python3 test_search_filter_regression.py

Exit 0 -> this regression suite passes.
Exit 1 -> DO NOT DEPLOY.
"""
from __future__ import annotations
import sys
import importlib

FAILURES=[]
PASS_COUNT=0

def check(label, condition, detail=""):
    global PASS_COUNT
    if condition:
        PASS_COUNT += 1
    else:
        FAILURES.append(f"{label}: {detail}")

def run():
    backend=importlib.import_module("numisvault_backend")

    print("[1/4] target denomination parsing")
    check("canonical drachma target",
          backend.parse_target_denomination("5 drachma")== (5.0,"drachma"),
          repr(backend.parse_target_denomination("5 drachma")))
    check("resolver display-label drachma target",
          backend.parse_target_denomination("5 Greek drachma")== (5.0,"drachma"),
          repr(backend.parse_target_denomination("5 Greek drachma")))
    check("resolver display-label Deutsche Mark target",
          backend.parse_target_denomination("5 Deutsche Mark")== (5.0,"mark"),
          repr(backend.parse_target_denomination("5 Deutsche Mark")))
    check("unparseable non-empty target fails closed",
          backend.denomination_matches("5 totally-unknown-unit","1901 J 5 Pfennig VF") is False)

    print("[2/4] denomination hard filter")
    check("5 drachma rejects 5 Pfennig",
          backend.denomination_matches("5 drachma","1901 J 5 Pfennig VF") is False)
    check("5 Greek drachma rejects 5 Pfennig",
          backend.denomination_matches("5 Greek drachma","1901 J 5 Pfennig VF") is False)
    check("5 Greek drachma accepts 5 Drachmai",
          backend.denomination_matches("5 Greek drachma","Greece 5 Drachmai 1901 VF") is True)
    check("5 Deutsche Mark rejects 5 Pfennig",
          backend.denomination_matches("5 Deutsche Mark","Deutschland 1901 J 5 Pfennig VF") is False)

    print("[3/4] explicit non-coin products")
    check("English postcard classified OTHER",
          backend.classify_asset("Germany postcard 1901 used")[0]=="OTHER")
    check("German Postkarte classified OTHER",
          backend.classify_asset("AK Deutschland Postkarte 1901 gebraucht")[0]=="OTHER")
    check("German Grußkarte classified OTHER",
          backend.classify_asset("AK Deutschland Grußkarte 1901 gebraucht")[0]=="OTHER")

    # Disable issue/theme DB dependency: this suite isolates core hard filters.
    old_resolver_available=backend.RESOLVER_AVAILABLE
    backend.RESOLVER_AVAILABLE=False
    try:
        payload={
            "coin":{"country":"Greece","denom":"5 Greek drachma","year":"1901",
                    "variant":"","theme":"","grade":""},
            "raw_query":"5 drachma 1901",
            "asset_type":"COIN",
        }
        print("[4/4] end-to-end hard-filter cases")
        check("Pfennig cannot survive Price Research hard filter",
              backend.passes_hard_filter("1901 J 5 Pfennig VF",payload) is False)
        check("postcard cannot survive Price Research hard filter",
              backend.passes_hard_filter("AK Deutschland Grußkarte 1901 gebraucht",payload) is False)
        check("correct Greece 5 Drachmai survives",
              backend.passes_hard_filter("Greece 5 Drachmai 1901 VF",payload) is True)
        # This preserves the intentional historical-authority behavior:
        # user typed no explicit country, so a Cretan-State 5 Drachmai listing
        # must not be rejected merely because it says Kreta instead of Greece.
        check("Kreta 5 Drachmai remains eligible when country was inferred",
              backend.passes_hard_filter("Kreta 5 Drachmai 1901 VF",payload) is True)
    finally:
        backend.RESOLVER_AVAILABLE=old_resolver_available

    if FAILURES:
        print("\nFAIL")
        for f in FAILURES:
            print(" -",f)
        print(f"\n{PASS_COUNT} passed, {len(FAILURES)} failed")
        return 1
    print(f"\nPASS — {PASS_COUNT}/{PASS_COUNT} checks")
    return 0

if __name__=="__main__":
    raise SystemExit(run())
