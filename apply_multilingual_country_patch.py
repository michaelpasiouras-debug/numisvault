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
])
