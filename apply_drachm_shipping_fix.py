#!/usr/bin/env python3
"""Apply the CoinBids marketplace drachm/shipping compatibility fixes.

Idempotent and deliberately narrow:
- allow the marketplace spelling "drachm" to satisfy a modern drachma target
  without deleting the separate ancient "drachm" denomination;
- parse shipping text such as "Tax included + 9,00 EUR shipping";
- keep the Greek numismatic country exception from overriding an explicitly
  conflicting historical issuing authority such as Kreta/Crete.
"""
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "numisvault_backend.py"

OLD_DRACHMA = '"drachma":["drachma","drachmas","drachmai","drachmae","drachme","drachmen","drachmi","drakhma","drakhmai","δραχμη","δραχμες","δραχμαι"],'
NEW_DRACHMA = '"drachma":["drachma","drachmas","drachmai","drachmae","drachme","drachmen","drachmi","drakhma","drakhmai","drachm","δραχμη","δραχμες","δραχμαι"],'

OLD_DENOM_MATCH = '''        d=parse_denomination(f"{v} {u}")
        if d and abs(d[0]-td[0])<1e-9 and d[1]==td[1]: return True
    return False
'''
NEW_DENOM_MATCH = '''        d=parse_denomination(f"{v} {u}")
        if d and abs(d[0]-td[0])<1e-9:
            if d[1]==td[1]:
                return True
            # Marketplace shorthand: modern Greek "drachma" listings are
            # sometimes titled "drachm". Treat only this pair as equivalent
            # at comparison time, without globally collapsing the distinct
            # ancient "drachm" denomination in the alias table.
            if {d[1], td[1]} == {"drachma", "drachm"}:
                return True
    return False
'''

OLD_SHIPPING = '''    ship_patterns=[
        rf"{ship_word}(?:[^€$£0-9]{{0,60}}){money_seg}",
        rf"\\+\\s*{money_seg}\\s*{ship_word}",
        rf"{money_seg}\\s*{ship_word}\\s*\\(?\\s*(?:to\\s*)?(?:Greece|Griechenland|Gr[eè]ce|Ελλάδα)",
    ]
'''
NEW_SHIPPING = '''    ship_patterns=[
        rf"{ship_word}(?:[^€$£0-9]{{0,60}}){money_seg}",
        rf"\\+\\s*{money_seg}\\s*{ship_word}",
        # Handles text such as "Tax included + 9,00 EUR shipping".
        rf"(?:tax included|tax)\\s*\\+?\\s*{money_seg}\\s*{ship_word}",
        rf"{money_seg}\\s*{ship_word}\\s*\\(?\\s*(?:to\\s*)?(?:Greece|Griechenland|Gr[eè]ce|Ελλάδα)",
    ]
'''

OLD_COUNTRY_EXCEPTION = '''        numismatic_exceptions = [
            "drachma", "drachmai", "drachmas", "lepta", "george i", "georgios"
        ]
        has_exception = any(ex in a for ex in numismatic_exceptions)
        if not country_in_title(country, a) and not has_exception:
            return False
'''
NEW_COUNTRY_EXCEPTION = '''        numismatic_exceptions = [
            "drachma", "drachmai", "drachmas", "lepta", "george i", "georgios"
        ]
        # The terminology exception is evidence for a Greek coin only when the
        # title does not explicitly name a conflicting historical issuer. In
        # particular, a user who typed Greece must not receive Cretan State /
        # Kreta / Crete issues merely because their title also says Drachmai.
        conflicting_greek_authority = (
            canonical_country(country) == "greece"
            and any(term in a for term in ("kreta", "crete", "cretan state", "cretan"))
        )
        has_exception = any(ex in a for ex in numismatic_exceptions) and not conflicting_greek_authority
        if not country_in_title(country, a) and not has_exception:
            return False
'''


def replace_once(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    if new in text:
        print(f"{label} already present")
        return text, False
    if old not in text:
        raise SystemExit(f"Expected block for {label} not found; refusing unsafe patch")
    print(f"Applied {label}")
    return text.replace(old, new, 1), True


def main() -> int:
    text = BACKEND.read_text(encoding="utf-8")
    changed = False
    text, c = replace_once(text, OLD_DRACHMA, NEW_DRACHMA, "drachm alias")
    changed |= c
    text, c = replace_once(text, OLD_DENOM_MATCH, NEW_DENOM_MATCH, "drachma/drachm contextual equivalence")
    changed |= c
    text, c = replace_once(text, OLD_SHIPPING, NEW_SHIPPING, "Tax-included shipping parser")
    changed |= c
    text, c = replace_once(text, OLD_COUNTRY_EXCEPTION, NEW_COUNTRY_EXCEPTION, "explicit-country historical-authority guard")
    changed |= c
    if changed:
        BACKEND.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
