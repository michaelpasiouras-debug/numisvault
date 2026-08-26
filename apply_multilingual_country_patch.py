#!/usr/bin/env python3
from pathlib import Path

def patch(path, replacements):
    p=Path(path); s=p.read_text(encoding='utf-8'); orig=s
    for old,new in replacements:
        if new in s: continue
        if old not in s:
            raise SystemExit(f'{path}: expected patch anchor not found: {old[:80]!r}')
        s=s.replace(old,new,1)
    if s!=orig:
        p.write_text(s,encoding='utf-8')
        print(f'patched {path}')
    else: print(f'{path}: already patched')

patch('coin_identity_resolver.py',[
('from difflib import SequenceMatcher\n','from difflib import SequenceMatcher\nfrom multilingual_country_aliases import normalize_country_aliases_in_text\n'),
('    return s\n\ndef variants(s:str):','    return normalize_country_aliases_in_text(s)\n\ndef variants(s:str):'),
])

patch('numisvault_backend.py',[
('from difflib import SequenceMatcher\n','from difflib import SequenceMatcher\nfrom multilingual_country_aliases import normalize_country_aliases_in_text\n'),
('    s = s.translate(_GREEK_ACCENT_MAP)\n    return re.sub(r"\\s+"," ",s).strip()','    s = s.translate(_GREEK_ACCENT_MAP)\n    s = re.sub(r"\\s+"," ",s).strip()\n    return normalize_country_aliases_in_text(s)'),
('    "greece":["greece","greek","hellas","ellada","griechenland","griekenland","grèce","grece","grecia","ελλαδα"],','    "greece":["greece","greek","hellas","ellada","griechenland","griekenland","grèce","grece","grecia","grécia","grecja","recko","řecko","grecko","grécko","grcka","grčka","gorogorszag","görögország","yunanistan","graekenland","grækenland","grekland","kreikka","ελλαδα"],'),
('    "euro":["euro","euros","eur"],','    "euro":["euro","euros","eur","evro","ευρω","ευρώ"],'),
('    "dollar":["dollar","dollars","usd"],','    "dollar":["dollar","dollars","usd","dolar","dollaro","δολαριο","δολαρια"],'),
('    "drachma":["drachma","drachmas","drachmai","drachmae","drachme","drachmen"],','    "drachma":["drachma","drachmas","drachmai","drachmae","drachme","drachmen","drachmi","drakhma","drakhmai","δραχμη","δραχμες","δραχμαι"],'),
('            if not country and b.get("country"): country=b["country"]\n            if not year and b.get("year"): year=str(b["year"])\n            if not denom and b.get("denomination_value") is not None:\n                unit=b.get("currency") or b.get("currency_code") or ""\n                denom=f\'{b["denomination_value"]:g} {unit}\'.strip()','            if not country and b.get("country"):\n                country=b["country"]; coin["country"]=country\n            if not year and b.get("year"):\n                year=str(b["year"]); coin["year"]=year\n            if not denom and b.get("denomination_value") is not None:\n                unit=b.get("currency") or b.get("currency_code") or ""\n                denom=f\'{b["denomination_value"]:g} {unit}\'.strip()\n                coin["denom"]=denom'),
('    qs = []\n    # Exact user wording first.','    # Raw-only API callers do not have the frontend parser\'s separate theme\n    # field. Derive only the descriptive residue here so known same-year/same-\n    # denomination issues (e.g. Antikythera vs Lord Byron) remain strictly\n    # separated. This mirrors parseCoinText() without guessing a theme when the\n    # raw text contains only structural identity fields.\n    if raw and not str(coin.get("theme") or "").strip():\n        theme_text=norm(raw)\n        if country:\n            cn=norm(country)\n            if cn:\n                theme_text=re.sub(rf"(?<![a-z0-9]){re.escape(cn)}(?![a-z0-9])"," ",theme_text,count=1)\n        if year:\n            theme_text=re.sub(rf"(?<!\\d){re.escape(str(year))}(?!\\d)"," ",theme_text)\n        # Remove denomination numbers and every known unit/currency spelling;\n        # leave all other words exactly as normalized so multilingual issue\n        # matching can still use them.\n        theme_text=re.sub(r"(?<!\\d)\\d+(?:[.,]\\d+)?(?!\\d)"," ",theme_text)\n        for _aliases in CURRENCY_UNIT_ALIASES.values():\n            for _al in sorted(set(_aliases),key=len,reverse=True):\n                _an=norm(_al)\n                if _an:\n                    theme_text=re.sub(rf"(?<![a-z0-9]){re.escape(_an)}(?![a-z0-9])"," ",theme_text)\n        theme_text=re.sub(r"(?<![a-z0-9])(?:coin|coins|proof|pp|unc|uncirculated|bu|fdc|silver|gold|argento|argent|silber|zilver|ασημι|ασημένιο)(?![a-z0-9])"," ",theme_text,re.I)\n        theme_text=re.sub(r"\\s+"," ",theme_text).strip(" -")\n        if theme_text:\n            coin["theme"]=theme_text\n\n    qs = []\n    # Exact user wording first.'),
])
