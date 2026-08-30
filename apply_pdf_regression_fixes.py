from pathlib import Path
import json

# 1) Curated physical specs: Crete 5 Drachmai 1901
spec_path = Path('coin_specs_database.json')
spec_db = json.loads(spec_path.read_text(encoding='utf-8'))
records = spec_db.setdefault('records', [])
crete_key = lambda r: (
    float(r.get('denomination') or 0) == 5.0
    and int(r.get('year_from') or 0) == 1901
    and int(r.get('year_to') or 0) == 1901
    and any(str(c).strip().lower() in {'crete','kreta'} for c in (r.get('countries') or []))
)
if not any(crete_key(r) for r in records):
    records.insert(1, {
        'countries': ['Crete', 'Kreta', 'Greece', 'GR'],
        'currency': 'GRD',
        'denomination': 5.0,
        'year_from': 1901,
        'year_to': 1901,
        'composition': 'Silver (.900)',
        'primary_metal': 'Silver',
        'fineness_per_mille': 900,
        'weight_g': 25.0,
        'diameter_mm': 37.0,
        'fine_metal_g': 22.5,
        'variant': 'Cretan State / Prince George, KM#9, N#19991',
        'source': 'Numista N#19991; Crete 5 Drachmai 1901',
        'source_url': 'https://en.numista.com/19991',
        'source_priority': 'catalogue_verified',
        'verified': True,
        'confidence': 1.0,
    })
spec_path.write_text(json.dumps(spec_db, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

# 2) Issue ontology: the two real Vatican 2-euro commemoratives of 2024.
issue_path = Path('coin_issue_database.json')
issue_db = json.loads(issue_path.read_text(encoding='utf-8'))
issues = issue_db.setdefault('issues', [])

def ensure_issue(canonical_title, aliases):
    existing = next((i for i in issues if i.get('canonical_title') == canonical_title), None)
    record = {
        'country_code': 'VA',
        'currency_code': 'EUR',
        'denomination_value': 2,
        'year': 2024,
        'canonical_title': canonical_title,
        'issuer': 'Vatican City State',
        'mint': 'IPZS (Italy)',
        'mintmark': 'R',
        'catalog_ids': {},
        'variants': ['BU', 'Proof'],
        'aliases': aliases,
        'confidence_source': 'official_vatican_2024_programme',
    }
    if existing is None:
        issues.append(record)
    else:
        existing.update(record)

ensure_issue(
    'Vatican 2 Euro 2024 — St. Thomas Aquinas',
    ['thomas aquinas', 'st thomas aquinas', 'saint thomas aquinas',
     "thomas d'aquin", 'thomas d aquin', 'tommaso d aquino',
     'san tommaso d aquino', 'doctor angelicus',
     '750th anniversary of the death of st thomas aquinas'],
)
ensure_issue(
    'Vatican 2 Euro 2024 — Guglielmo Marconi',
    ['guglielmo marconi', 'marconi',
     '150th anniversary of the birth of guglielmo marconi',
     '150 anniversario nascita guglielmo marconi'],
)
issue_path.write_text(json.dumps(issue_db, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

# 3) Backend issue gate: Vatican was missing from the bounded country->issue
# lookup, so the gate returned True before consulting the seeded VA issues.
# Also, when a user asks for an unknown theme (e.g. Perugino), explicitly
# named known same-year issues must be rejected rather than leaking through.
backend_path = Path('numisvault_backend.py')
src = backend_path.read_text(encoding='utf-8')
old_map = '_ISSUE_COUNTRY_NAME_TO_CODE={"greece":"GR","ελλαδα":"GR","ελλαs":"GR","hellas":"GR","hellenic republic":"GR"}'
new_map = '_ISSUE_COUNTRY_NAME_TO_CODE={"greece":"GR","ελλαδα":"GR","ελλαs":"GR","hellas":"GR","hellenic republic":"GR","vatican":"VA","vatican city":"VA","vatican city state":"VA","holy see":"VA"}'
if old_map in src:
    src = src.replace(old_map, new_map, 1)
elif '"vatican":"VA"' not in src:
    raise SystemExit('Vatican issue-country map anchor not found')

old_unmatched = '''    if not matched:\n        return True\n\n    # Mutual exclusion for same-country / same-denomination / same-year'''
new_unmatched = '''    if not matched:\n        # The requested theme does not identify any seeded issue. Keep the\n        # generic fallback for unrelated titles, but never accept a listing\n        # that explicitly names a different KNOWN issue sharing this exact\n        # country/denomination/year identity. This is the Perugino/Vatican\n        # regression: Thomas Aquinas and Marconi cannot satisfy Perugino.\n        title_norm=norm(title)\n        for known_iss in candidates:\n            known_pool=[known_iss.get("canonical_title","")]+list(known_iss.get("aliases") or [])\n            if any(al and norm(al) and norm(al) in title_norm for al in known_pool):\n                print(f"[Theme Gate] REJECTED (Unknown requested issue vs known issue): {title!r}")\n                return False\n        return True\n\n    # Mutual exclusion for same-country / same-denomination / same-year'''
if old_unmatched in src:
    src = src.replace(old_unmatched, new_unmatched, 1)
elif 'Unknown requested issue vs known issue' not in src:
    raise SystemExit('Theme unmatched anchor not found')
backend_path.write_text(src, encoding='utf-8')

print('PDF regression catalogue/issue fixes applied')
