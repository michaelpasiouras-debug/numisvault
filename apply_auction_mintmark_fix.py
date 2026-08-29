#!/usr/bin/env python3
"""Apply idempotent Auction Intelligence denomination + mintmark-year fixes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "numisvault_backend.py"
MATCHING = ROOT / "auction_matching.py"
SOURCES = ROOT / "auction_sources.py"


def replace_once(path: Path, old: str, new: str, label: str):
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"{label} already present")
        return
    if old not in text:
        raise SystemExit(f"Expected block for {label} not found in {path.name}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"Applied {label}")


# Price Research: remove year together with an optional mintmark suffix from theme residue.
replace_once(
    BACKEND,
    '        if year:\n            theme_text=re.sub(rf"(?<!\\d){re.escape(str(year))}(?!\\d)"," ",theme_text)\n',
    '        if year:\n            # Remove the year together with an optional mintmark suffix such as\n            # 1876A / 1876-A / 1876 A so it cannot leak into theme_text.\n            theme_text=re.sub(\n                rf"(?<!\\d){re.escape(str(year))}(?:\\s*[-.]?\\s*[A-Za-z]{1,2})?(?![A-Za-z0-9])",\n                " ", theme_text, flags=re.I\n            )\n',
    "mintmark-aware year cleanup",
)

# Auction matching: use title/description as a fallback denomination source when
# structured denomination_value is missing, with the same pre-euro spelling family.
insert_anchor = 'TIER_THRESHOLDS = {\n    "EXACT": 90,\n    "STRONG": 75,\n    "SUPPORTING": 55,\n}\n\n\n'
helper = '''TIER_THRESHOLDS = {\n    "EXACT": 90,\n    "STRONG": 75,\n    "SUPPORTING": 55,\n}\n\n\n_AUCTION_DENOM_ALIASES = {\n    "GRD": ["drachma", "drachmas", "drachmai", "drachmae", "drachme", "drachmen",\n            "drachmi", "drakhma", "drakhmai", "drachm", "δραχμη", "δραχμες", "δραχμαι", "δρχ"],\n    "ITL": ["lira", "lire", "liras", "litl"],\n    "ESP": ["peseta", "pesetas", "pta", "pts", "ptas"],\n    "DEM": ["mark", "marks", "deutsche mark", "reichsmark", "dm", "dmark", "d mark"],\n    "NLG": ["guilder", "guilders", "gulden", "fl", "florin", "nlg"],\n    "ATS": ["schilling", "schillings", "ats"],\n    "FIM": ["markka", "markkaa", "finnmark", "finnmarkka", "fim"],\n}\n\n\ndef validate_auction_denomination(target: dict, comp: AuctionComparable) -> bool:\n    """Validate denomination from structured data first, then auction text.\n\n    This intentionally avoids importing numisvault_backend.py because that file\n    already imports auction_matching.py; importing it back here would create a\n    circular dependency.  The text fallback is conservative: exact numeric face\n    value plus an alias associated with the target currency code.\n    """\n    target_value = target.get("denomination_value")\n    if target_value is None:\n        return True\n\n    if comp.denomination_value is not None:\n        try:\n            return abs(float(comp.denomination_value) - float(target_value)) < 1e-9\n        except (TypeError, ValueError):\n            return False\n\n    currency_code = str(target.get("currency_code") or "").upper()\n    aliases = _AUCTION_DENOM_ALIASES.get(currency_code) or []\n    if not aliases:\n        return False\n\n    text = " ".join(filter(None, [comp.title, comp.description or ""])).lower().replace(",", ".")\n    number = f"{float(target_value):g}"\n    for alias in sorted(set(aliases), key=len, reverse=True):\n        if __import__("re").search(\n            rf"(?<!\\d){__import__('re').escape(number)}\\s*{__import__('re').escape(alias)}(?![a-z])",\n            text, __import__("re").I\n        ):\n            return True\n    return False\n\n\n'''
replace_once(MATCHING, insert_anchor, helper, "auction denomination text fallback")

replace_once(
    MATCHING,
    '    t_denom = target.get("denomination_value")\n    if t_denom is not None:\n        applicable_weight += WEIGHTS["denomination"]\n        if comp.denomination_value is not None and abs(float(comp.denomination_value) - float(t_denom)) < 1e-9:\n            achieved_weight += WEIGHTS["denomination"]; reasons.append("denomination exact")\n        else:\n            hard_reject_reasons.append("wrong denomination")\n',
    '    t_denom = target.get("denomination_value")\n    if t_denom is not None:\n        applicable_weight += WEIGHTS["denomination"]\n        if validate_auction_denomination(target, comp):\n            achieved_weight += WEIGHTS["denomination"]; reasons.append("denomination exact")\n        else:\n            hard_reject_reasons.append("wrong denomination")\n',
    "classify_comparable denomination validation",
)

# CSV adapter: preserve identity columns instead of discarding them.
replace_once(
    SOURCES,
    '    KNOWN_COLUMNS = {"date", "hammer", "currency", "grade", "auction_house", "url",\n                      "auction_name", "lot_number", "grading_company", "cert_number"}\n',
    '    KNOWN_COLUMNS = {"date", "hammer", "currency", "grade", "auction_house", "url",\n                      "auction_name", "lot_number", "grading_company", "cert_number",\n                      "title", "description", "country", "country_code", "currency_code",\n                      "denomination", "denomination_value", "year", "coin_year", "issuer",\n                      "mint", "mintmark", "variant"}\n',
    "CSV identity columns",
)

old_comp = '''            comp = AuctionComparable(\n                source="csv",\n                title=f"CSV import — {get('auction_house') or 'unknown house'}",\n                auction_house=get("auction_house") or None,\n                auction_name=get("auction_name") or None,\n                auction_date=adate.isoformat() if adate else None,\n                lot_number=get("lot_number") or None,\n                source_url=get("url") or None,\n                grade_raw=get("grade") or None,\n                grading_company=get("grading_company") or None,\n                cert_number=get("cert_number") or None,\n                hammer_price=hammer,\n                currency=(get("currency") or default_currency).upper(),\n                price_semantics=PriceSemantics.HAMMER.value,\n                sold=True,\n            )\n'''
new_comp = '''            raw_denom = get("denomination_value") or get("denomination")\n            try:\n                denom_value = float(raw_denom.replace(",", ".")) if raw_denom else None\n            except ValueError:\n                denom_value = None\n            raw_year = get("coin_year") or get("year")\n            try:\n                coin_year = int(raw_year) if raw_year else None\n            except ValueError:\n                coin_year = None\n            comp = AuctionComparable(\n                source="csv",\n                title=get("title") or f"CSV import — {get('auction_house') or 'unknown house'}",\n                description=get("description") or None,\n                auction_house=get("auction_house") or None,\n                auction_name=get("auction_name") or None,\n                auction_date=adate.isoformat() if adate else None,\n                lot_number=get("lot_number") or None,\n                source_url=get("url") or None,\n                country=get("country") or None,\n                country_code=(get("country_code") or None),\n                currency_code=(get("currency_code") or None),\n                denomination_value=denom_value,\n                coin_year=coin_year,\n                issuer=get("issuer") or None,\n                mint=get("mint") or None,\n                mintmark=get("mintmark") or None,\n                variant=get("variant") or None,\n                grade_raw=get("grade") or None,\n                grading_company=get("grading_company") or None,\n                cert_number=get("cert_number") or None,\n                hammer_price=hammer,\n                currency=(get("currency") or default_currency).upper(),\n                price_semantics=PriceSemantics.HAMMER.value,\n                sold=True,\n            )\n'''
replace_once(SOURCES, old_comp, new_comp, "CSV comparable identity preservation")
