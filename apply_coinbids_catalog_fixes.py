#!/usr/bin/env python3
"""Apply verified, idempotent CoinBids catalogue/backend fixes.

This script changes only records that match exact country/currency/denomination
or exact known composition text, plus narrowly-scoped code replacements whose
old/new forms are asserted exactly. It is safe to run repeatedly.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "coin_specs_database_MASTER_EUROPE_v17.json"
BACKEND_PATH = ROOT / "numisvault_backend.py"
RESOLVER_PATH = ROOT / "coin_identity_resolver.py"

BOSNIA = {
    0.05: (2.70, 18.00, "https://cbbh.ba/content/DownloadAttachment/?id=68732829-d3b5-4949-91b4-22140860e813&langTag=bs"),
    0.10: (3.90, 20.00, "https://www.cbbh.ba/content/DownloadAttachment/?id=e009346a-213a-47f4-b78b-e82393907fc6&langTag=en"),
    0.20: (4.50, 22.00, "https://www.cbbh.ba/content/DownloadAttachment/?id=e009346a-213a-47f4-b78b-e82393907fc6&langTag=en"),
    0.50: (5.20, 24.50, "https://www.cbbh.ba/content/DownloadAttachment/?id=e009346a-213a-47f4-b78b-e82393907fc6&langTag=en"),
    1.00: (4.90, 23.25, "https://www.cbbh.ba/content/DownloadAttachment/?id=2666ce3e-8da5-417b-aa8d-0fc0e58c3f32&langTag=en"),
    2.00: (6.90, 25.75, "https://www.cbbh.ba/content/DownloadAttachment/?id=2666ce3e-8da5-417b-aa8d-0fc0e58c3f32&langTag=en"),
    5.00: (10.33, 30.00, "https://cbbh.ba/content/DownloadAttachment/?id=68732829-d3b5-4949-91b4-22140860e813&langTag=bs"),
}

FINENESS_BY_COMPOSITION = {
    "Silver-copper alloy (Ag 625, Cu 375)": 625,
    "Silver alloy (40% Ag minimum)": 400,
    "Silver alloy (40% Ag)": 400,
}

OLD_SEARCH_CACHE_KEY = '''def _search_cache_key(payload):
    coin=payload.get("coin") or {}
    # Include the raw free-text query / canonical resolved identity, not only
    # the structured coin fields — otherwise two semantically different raw
    # queries with an empty or identical structured "coin" object (e.g. both
    # relying entirely on server-side resolver inference) would collide on
    # the same cache key and one would silently serve the other's cached
    # results for up to _SEARCH_CACHE_TTL seconds.
    raw_query=str(payload.get("raw_query") or coin.get("raw") or "").strip().lower()
    # Price Research and Auction Intelligence are two views of the SAME live
    # purchase market. Same raw query + destination + currency must share one
    # snapshot so the canonical dealer anchor cannot contradict itself.
    if raw_query:
        parts=[raw_query]
    else:
        parts=[str(coin.get(k) or "").strip().lower() for k in
               ("country","denom","denomination","year","variant","grade")]
    parts+=[str(bool(payload.get("include_shipping"))),
            str(payload.get("currency") or "EUR").upper(),
            str(payload.get("ship_to") or "").strip().lower()]
    return "|".join(parts)
'''

NEW_SEARCH_CACHE_KEY = '''def _search_cache_key(payload):
    """Return a cache key for the *exact response contract* of coin-search.

    Search-result caching must separate both market identity inputs and response
    projection inputs.  A normal UI request (top 2) must never satisfy a QA
    request (full evidence), and a QA response must never leak back into the UI.
    Likewise, shipping weight can change dealer shipping tiers and therefore the
    delivered-price ordering, so it is part of the market identity.
    """
    coin=payload.get("coin") or {}
    raw_query=str(payload.get("raw_query") or coin.get("raw") or "").strip().lower()
    parts=["raw="+raw_query]
    for k in ("country","countryEN","denom","denomination","year","variant","grade","theme","currency"):
        parts.append(f"coin.{k}="+str(coin.get(k) or "").strip().lower())
    weight=(payload.get("weight_g") if payload.get("weight_g") is not None else
            payload.get("coin_weight_g") if payload.get("coin_weight_g") is not None else
            payload.get("physical_weight_g"))
    parts += [
        "include_shipping="+str(bool(payload.get("include_shipping"))),
        "currency="+str(payload.get("currency") or "EUR").upper(),
        "ship_to="+str(payload.get("ship_to") or "").strip().lower(),
        "weight_g="+str(weight if weight is not None else ""),
        "limit="+str(int(payload.get("limit") or 2)),
        "sample_limit="+str(int(payload.get("sample_limit") or 10)),
        "qa_full_evidence="+str(bool(payload.get("qa_full_evidence"))),
    ]
    return "|".join(parts)
'''

OLD_SUBUNIT_NORMALIZATION = '''            # Canonicalize cent-denominated inputs for EUR/USD so "25 cents"
            # becomes 0.25 of the major currency unit rather than 25 dollars/euros.
            if candidate_denom is not None and curcode in ("USD","EUR") and re.search(r"(?<![a-z])cents?(?![a-z])",text,re.I):
                candidate_denom=candidate_denom/100.0
'''

NEW_SUBUNIT_NORMALIZATION = '''            # Canonicalize decimal subunit inputs into the major currency unit.
            # 25 cents -> 0.25 USD/EUR; 50 pence -> 0.50 GBP.  Keep this
            # deliberately scoped to modern decimal currencies with an exact
            # lexical subunit signal so historical pre-decimal values are not
            # silently rescaled.
            _decimal_subunit=(
                curcode in ("USD","EUR") and re.search(r"(?<![a-z])cents?(?![a-z])",text,re.I)
            ) or (
                curcode=="GBP" and re.search(r"(?<![a-z])(?:pence|penn(?:y|ies))(?![a-z])",text,re.I)
            )
            if candidate_denom is not None and _decimal_subunit:
                candidate_denom=candidate_denom/100.0
'''


def exact_patch(path: Path, old: str, new: str, label: str) -> int:
    if not path.exists():
        raise SystemExit(f"{path.name} missing; cannot apply {label}")
    text=path.read_text(encoding="utf-8")
    if new in text:
        print(f"{label} already present.")
        return 0
    if old not in text:
        raise SystemExit(f"Expected old block for {label} not found; refusing unsafe patch")
    path.write_text(text.replace(old,new,1),encoding="utf-8")
    print(f"Applied {label}.")
    return 1


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    changed = 0
    for r in data.get("records", []):
        countries = r.get("countries") or []
        if "Bosnia and Herzegovina" in countries and r.get("currency") == "BAM":
            try: denom = float(r.get("denomination"))
            except Exception: denom = None
            if denom in BOSNIA:
                weight, diameter, source_url = BOSNIA[denom]
                for key,value in (("weight_g",weight),("diameter_mm",diameter),("source_url",source_url),("source_priority","official")):
                    if r.get(key)!=value:
                        r[key]=value;changed+=1
                if r.get("metal_value_ready") is False:
                    r["metal_value_ready"] = True;changed+=1
        comp = str(r.get("composition") or "")
        if comp in FINENESS_BY_COMPOSITION:
            fin = FINENESS_BY_COMPOSITION[comp]
            if r.get("fineness_per_mille") != fin:
                r["fineness_per_mille"] = fin;changed += 1
            if r.get("weight_g") is not None:
                fine_g = round(float(r["weight_g"]) * fin / 1000.0, 6)
                if r.get("fine_metal_g") != fine_g:
                    r["fine_metal_g"] = fine_g;changed += 1
            r["metal_value_ready"] = True
    if changed:
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Applied {changed} catalogue field updates.")
    else:
        print("Catalogue already contains all verified fixes.")

    exact_patch(BACKEND_PATH,OLD_SEARCH_CACHE_KEY,NEW_SEARCH_CACHE_KEY,"coin-search cache-key isolation fix")
    exact_patch(RESOLVER_PATH,OLD_SUBUNIT_NORMALIZATION,NEW_SUBUNIT_NORMALIZATION,"GBP pence denomination normalization fix")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
