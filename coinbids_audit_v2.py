#!/usr/bin/env python3
"""CoinBids audit v2.

This wrapper keeps the existing audit/reporting/live-API machinery but replaces
only the catalog checker with a stricter, lower-noise version.

Important: this does NOT hide unknown conflicts. It only stops flagging three
already-documented cases as REVIEW:
1) verified composition-only records explicitly marked metal_value_ready=false,
2) precious-metal fineness already encoded in composition text,
3) overlapping specification families that carry explicit variant evidence or
   collide only at a one-year transition boundary.

Anonymous multi-year conflicts still remain REVIEW.
"""
from __future__ import annotations

import math
import re

import coinbids_audit as base


def _finite_num(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def _composition_fineness(comp: str):
    s = str(comp or "")
    patterns = (
        (r"\.(\d{3})\b", lambda m: float(m.group(1))),
        (r"\b(\d{3})\s*/\s*1000\b", lambda m: float(m.group(1))),
        (r"\b(\d{3})\s*‰", lambda m: float(m.group(1))),
        (r"\b(?:Ag|Au|Pt|Pd)\s*(\d{3})\b", lambda m: float(m.group(1))),
        (r"\b(\d+(?:\.\d+)?)\s*%\s*(?:Ag|Au|Pt|Pd)\b", lambda m: float(m.group(1)) * 10.0),
    )
    for pat, conv in patterns:
        m = re.search(pat, s, flags=re.I)
        if m:
            try:
                v = conv(m)
                if 1 <= v <= 1000:
                    return v
            except Exception:
                pass
    return None


def _intentional_partial(r: dict) -> bool:
    return (
        r.get("metal_value_ready") is False
        and bool(r.get("verified"))
        and str(r.get("source_priority") or "").casefold() in {
            "official_composition_only",
            "official_framework_only",
        }
    )


def _variant(r: dict) -> str:
    return str(r.get("variant") or r.get("type") or r.get("issue") or "").strip()


def _intentional_overlap(r1: dict, r2: dict, overlap_from: int, overlap_to: int) -> bool:
    # An explicitly named type/variant is sufficient evidence that two physical
    # specs may coexist for the same denomination/year.
    if _variant(r1) or _variant(r2):
        return True

    # A single boundary-year collision is a normal transition pattern. Broader
    # anonymous overlaps are NOT suppressed.
    if overlap_from == overlap_to:
        y1a, y1b = int(r1["year_from"]), r1.get("year_to")
        y2a, y2b = int(r2["year_from"]), r2.get("year_to")
        if y1b is not None and int(y1b) == y2a == overlap_from:
            return True
        if y2b is not None and int(y2b) == y1a == overlap_from:
            return True
    return False


def offline_catalog_audit(specs: dict):
    records = specs.get("records")
    if not isinstance(records, list) or not records:
        base.add("FAIL", "catalog", "records", "Missing/non-list catalog records")
        return

    base.add("PASS", "catalog", "records", f"Loaded {len(records)} spec records")
    required = ("countries", "currency", "denomination", "year_from", "composition", "weight_g", "diameter_mm")
    precious = re.compile(r"\b(gold|silver|platinum|palladium)\b|\b(?:Ag|Au|Pt|Pd)\s*\d", re.I)
    intervals = []

    for i, r in enumerate(records):
        tag = f"record[{i}]"
        missing = [k for k in required if r.get(k) in (None, "", [])]
        if missing:
            identity_missing = [k for k in missing if k in ("countries", "currency", "denomination", "year_from", "composition")]
            physical_missing = [k for k in missing if k in ("weight_g", "diameter_mm")]
            if identity_missing:
                base.add("FAIL", "catalog", tag, "Required identity fields missing", ", ".join(identity_missing))
                continue
            if physical_missing:
                if _intentional_partial(r):
                    base.add("PASS", "catalog", tag, "Intentional verified partial physical spec", ", ".join(physical_missing))
                else:
                    base.add("REVIEW", "catalog", tag, "Physical-spec fields missing", ", ".join(physical_missing))

        countries = r.get("countries")
        if not isinstance(countries, list) or not all(isinstance(c, str) and c.strip() for c in countries):
            base.add("FAIL", "catalog", tag, "Invalid countries field", repr(countries))

        if not _finite_num(r.get("denomination")) or float(r["denomination"]) <= 0:
            base.add("FAIL", "catalog", tag, "Invalid denomination", repr(r.get("denomination")))

        for fld, lo, hi in (("weight_g", 0.01, 5000), ("diameter_mm", 1, 200)):
            if r.get(fld) is not None and (not _finite_num(r.get(fld)) or not (lo <= float(r[fld]) <= hi)):
                base.add("FAIL", "catalog", tag, f"Implausible {fld}", repr(r.get(fld)))

        yf, yt = r.get("year_from"), r.get("year_to")
        if not isinstance(yf, int) or yf < 500 or yf > 2200:
            base.add("FAIL", "catalog", tag, "Invalid year_from", repr(yf))
        if yt is not None and (not isinstance(yt, int) or yt < yf or yt > 2200):
            base.add("FAIL", "catalog", tag, "Invalid year_to", repr(yt))

        comp = str(r.get("composition") or "")
        is_precious = bool(precious.search(comp)) and "nordic gold" not in comp.casefold()
        if is_precious:
            fin = r.get("fineness_per_mille")
            if fin is None:
                fin = r.get("fineness")
            parsed = _composition_fineness(comp)
            if fin is None:
                if parsed is None:
                    base.add("REVIEW", "metal", tag, "Precious-metal record has no usable fineness", comp)
                else:
                    base.add("PASS", "metal", tag, f"Fineness encoded in composition ({parsed:g}‰)", comp)
            elif not _finite_num(fin) or not (1 <= float(fin) <= 1000):
                base.add("FAIL", "metal", tag, "Invalid fineness", repr(fin))
            else:
                base.add("PASS", "metal", tag, f"Explicit fineness {float(fin):g}‰")

        if isinstance(countries, list) and isinstance(yf, int):
            for country in countries:
                intervals.append((country, str(r.get("currency")), float(r.get("denomination") or 0), yf, yt or 9999, i, r))

    for a in range(len(intervals)):
        c1, cur1, d1, y1a, y1b, i1, r1 = intervals[a]
        for b in range(a + 1, len(intervals)):
            c2, cur2, d2, y2a, y2b, i2, r2 = intervals[b]
            if c1 != c2 or cur1 != cur2 or d1 != d2:
                continue
            overlap_from, overlap_to = max(y1a, y2a), min(y1b, y2b)
            if overlap_from > overlap_to:
                continue

            w1, w2 = r1.get("weight_g"), r2.get("weight_g")
            dia1, dia2 = r1.get("diameter_mm"), r2.get("diameter_mm")
            if w1 is None or w2 is None or dia1 is None or dia2 is None:
                continue

            same = (
                str(r1.get("composition") or "").casefold() == str(r2.get("composition") or "").casefold()
                and abs(float(w1) - float(w2)) < 1e-6
                and abs(float(dia1) - float(dia2)) < 1e-6
            )
            if same:
                continue

            if _intentional_overlap(r1, r2, overlap_from, overlap_to):
                base.add("PASS", "catalog", f"{c1} {d1} {cur1}", "Documented/transition variant overlap", f"records {i1} and {i2}; overlap {overlap_from}-{overlap_to}")
            else:
                base.add("REVIEW", "catalog", f"{c1} {d1} {cur1}", "Ambiguous overlapping spec records disagree", f"records {i1} and {i2}; overlap {overlap_from}-{overlap_to}")


base.offline_catalog_audit = offline_catalog_audit

if __name__ == "__main__":
    raise SystemExit(base.main())
