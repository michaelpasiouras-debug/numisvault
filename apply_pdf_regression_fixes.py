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
# This lets the theme gate distinguish them and reject a non-existent
# "Perugino" 2024 request instead of showing Thomas Aquinas or Marconi.
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
    [
        'thomas aquinas', 'st thomas aquinas', 'saint thomas aquinas',
        "thomas d'aquin", 'thomas d aquin', 'tommaso d aquino',
        'san tommaso d aquino', 'doctor angelicus',
        '750th anniversary of the death of st thomas aquinas',
    ],
)
ensure_issue(
    'Vatican 2 Euro 2024 — Guglielmo Marconi',
    [
        'guglielmo marconi', 'marconi',
        '150th anniversary of the birth of guglielmo marconi',
        '150 anniversario nascita guglielmo marconi',
    ],
)
issue_path.write_text(json.dumps(issue_db, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

print('PDF regression catalogue/issue fixes applied')
