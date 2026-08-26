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
])
