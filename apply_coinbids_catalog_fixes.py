#!/usr/bin/env python3
"""Apply verified, idempotent CoinBids catalogue fixes.

This script changes only records that match exact country/currency/denomination
or exact known composition text. It is safe to run repeatedly.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "coin_specs_database_MASTER_EUROPE_v17.json"

# Official CBBH decisions:
# 10F/20F/50F: Decision on issuance/basic features, 1998.
# 1KM/2KM: Decision on issuance/basic features, 2000.
# 5F/5KM: Decision on issuance/basic features, 2006.
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


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    changed = 0

    for r in data.get("records", []):
        countries = r.get("countries") or []
        if "Bosnia and Herzegovina" in countries and r.get("currency") == "BAM":
            try:
                denom = float(r.get("denomination"))
            except Exception:
                denom = None
            if denom in BOSNIA:
                weight, diameter, source_url = BOSNIA[denom]
                if r.get("weight_g") != weight:
                    r["weight_g"] = weight
                    changed += 1
                if r.get("diameter_mm") != diameter:
                    r["diameter_mm"] = diameter
                    changed += 1
                if r.get("source_url") != source_url:
                    r["source_url"] = source_url
                    changed += 1
                if r.get("source_priority") != "official":
                    r["source_priority"] = "official"
                    changed += 1
                if r.get("metal_value_ready") is False:
                    # Common-metal/bimetallic records do not need precious-metal
                    # melt pricing, but their physical spec is now complete.
                    r["metal_value_ready"] = True
                    changed += 1

        comp = str(r.get("composition") or "")
        if comp in FINENESS_BY_COMPOSITION:
            fin = FINENESS_BY_COMPOSITION[comp]
            if r.get("fineness_per_mille") != fin:
                r["fineness_per_mille"] = fin
                changed += 1
            if r.get("weight_g") is not None:
                fine_g = round(float(r["weight_g"]) * fin / 1000.0, 6)
                if r.get("fine_metal_g") != fine_g:
                    r["fine_metal_g"] = fine_g
                    changed += 1
            r["metal_value_ready"] = True

    if changed:
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Applied {changed} catalogue field updates.")
    else:
        print("Catalogue already contains all verified fixes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
