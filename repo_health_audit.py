#!/usr/bin/env python3
from __future__ import annotations
import ast, json, re, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
FAIL=[]; WARN=[]; PASS=[]

def ok(msg): PASS.append(msg)
def fail(msg): FAIL.append(msg)
def warn(msg): WARN.append(msg)

TEXT_EXT={'.py','.json','.html','.js','.css','.yml','.yaml','.md','.txt','.sql','.xml','.webmanifest'}
SKIP_DIRS={'.git','__pycache__','.venv','venv','node_modules'}
files=[p for p in ROOT.rglob('*') if p.is_file() and not any(x in SKIP_DIRS for x in p.parts)]

# 1) Gross repository corruption / true unresolved merge markers / empty code files.
for p in files:
    rel=p.relative_to(ROOT)
    if p.suffix.lower() not in TEXT_EXT and p.name not in {'Procfile','requirements.txt','robots.txt'}: continue
    try: text=p.read_text(encoding='utf-8')
    except Exception as e:
        fail(f'{rel}: not valid UTF-8 ({e})'); continue
    # Decorative ===== headings are common in this repo; only flag a real
    # conflict when BOTH opening and closing Git conflict markers exist.
    if re.search(r'^<<<<<<<\s',text,re.M) and re.search(r'^>>>>>>>\s',text,re.M):
        fail(f'{rel}: unresolved merge-conflict markers')
    if p.suffix.lower() in {'.py','.js','.html'} and not text.strip(): fail(f'{rel}: empty executable/source file')
ok(f'scanned {len(files)} repository files for structural corruption')

# 2) Every Python file must parse.
pyfiles=[p for p in files if p.suffix=='.py']
for p in pyfiles:
    try: ast.parse(p.read_text(encoding='utf-8'),filename=str(p))
    except SyntaxError as e: fail(f'{p.name}: Python syntax error line {e.lineno}: {e.msg}')
if not any('Python syntax error' in x for x in FAIL): ok(f'{len(pyfiles)} Python files parse successfully')

# 3) Every JSON/webmanifest file must parse.
jsonfiles=[p for p in files if p.suffix in {'.json','.webmanifest'}]
for p in jsonfiles:
    try: json.loads(p.read_text(encoding='utf-8'))
    except Exception as e: fail(f'{p.name}: invalid JSON ({e})')
if not any('invalid JSON' in x for x in FAIL): ok(f'{len(jsonfiles)} JSON/webmanifest files parse successfully')

# 4) Flask frontend/static contracts: referenced server files and core routes.
backend=ROOT/'numisvault_backend.py'
if backend.exists():
    bt=backend.read_text(encoding='utf-8')
    static_refs=set(re.findall(r'send_from_directory\(APP_DIR\s*,\s*["\']([^"\']+)',bt))
    for name in sorted(static_refs):
        if not (ROOT/name).exists(): fail(f'backend route references missing static file: {name}')
    required_routes=['/api/coin-search','/api/resolve-coin','/api/coin-lookup','/api/metal-spot']
    for route in required_routes:
        (ok if route in bt else fail)(f'backend core route present: {route}' if route in bt else f'backend core route MISSING: {route}')
else: fail('numisvault_backend.py missing')

# 5) Procfile entrypoint must point to an existing Python module and Flask app name.
proc=ROOT/'Procfile'
if proc.exists():
    pt=proc.read_text(encoding='utf-8')
    m=re.search(r'gunicorn\s+([A-Za-z_][\w.]*)\s*:\s*([A-Za-z_]\w*)',pt)
    if not m: fail('Procfile: gunicorn module:app entrypoint not found')
    else:
        mod,app=m.groups(); target=ROOT/(mod.replace('.','/')+'.py')
        if not target.exists(): fail(f'Procfile points to missing module {mod}')
        else:
            t=target.read_text(encoding='utf-8')
            if not re.search(rf'\b{re.escape(app)}\s*=\s*Flask\b',t): fail(f'Procfile app object {app!r} not found in {target.name}')
            else: ok(f'Procfile entrypoint {mod}:{app} is structurally valid')
else: fail('Procfile missing')

# 6) Frontend API contracts.
idx=ROOT/'index.html'
if idx.exists() and backend.exists():
    it=idx.read_text(encoding='utf-8'); bt=backend.read_text(encoding='utf-8')
    endpoints=set(re.findall(r'["\'](/api/[A-Za-z0-9_./<>-]+)["\']',it))
    route_templates=set(re.findall(r'@app\.(?:get|post|delete|put|patch)\(["\']([^"\']+)',bt))
    for ep in sorted(endpoints):
        if '<' in ep: continue
        if ep not in route_templates: warn(f'frontend references API endpoint not found as exact Flask route: {ep}')
    ok(f'checked {len(endpoints)} frontend API endpoint references')
else: fail('index.html missing')

# 7) Country aliases must be non-conflicting in the identity DB.
idb=ROOT/'coin_identity_database.json'
if idb.exists():
    db=json.loads(idb.read_text(encoding='utf-8'))
    owner={}
    def n(s):
        import unicodedata
        s=unicodedata.normalize('NFKD',str(s).casefold())
        return ''.join(c for c in s if not unicodedata.combining(c)).strip()
    for c in db.get('countries',[]):
        for a in [c.get('name'),c.get('code'),*(c.get('aliases') or [])]:
            if not a: continue
            k=n(a); prev=owner.get(k)
            if prev and prev!=c.get('name'): fail(f'country alias collision: {a!r} -> both {prev} and {c.get("name")}')
            owner[k]=c.get('name')
    ok(f'checked {len(owner)} normalized country aliases for collisions')

# 8) Critical Auction/Price Research invariants must remain in frontend.
if idx.exists():
    it=idx.read_text(encoding='utf-8')
    invariants={
      'canonical dealer anchor':'bestDealerAllIn',
      'metal floor':'verifiedMetalFloorAllIn',
      'below-metal strong buy':'STRONG BUY — BELOW METAL VALUE',
      'price-research session snapshot':'lastPriceResearchSnapshot',
    }
    for label,needle in invariants.items():
        (ok if needle in it else fail)(f'{label} invariant present' if needle in it else f'{label} invariant MISSING')

print('COINBIDS FULL REPOSITORY HEALTH AUDIT')
print('='*72)
for x in FAIL: print('[FAIL]',x)
for x in WARN: print('[WARN]',x)
print(f'PASS={len(PASS)} WARN={len(WARN)} FAIL={len(FAIL)}')
for x in PASS: print('[PASS]',x)
Path('repo_health_audit_summary.txt').write_text('\n'.join([
    'COINBIDS FULL REPOSITORY HEALTH AUDIT',f'PASS={len(PASS)} WARN={len(WARN)} FAIL={len(FAIL)}','',
    *('[FAIL] '+x for x in FAIL),*('[WARN] '+x for x in WARN),*('[PASS] '+x for x in PASS)
]),encoding='utf-8')
sys.exit(1 if FAIL else 0)
