from pathlib import Path

PATH=Path('numisvault_backend.py')
text=PATH.read_text(encoding='utf-8')
old='''                    q=" ".join(x for x in [denom,year,alias] if x)\n'''
new='''                    # Keep country in issue-specific queries. Historical live QA\n                    # proved that MA-Shops returns the Antikythera listing for\n                    # "Greece 10 euro 2022 antikythera mechanism"; dropping the\n                    # country broadens the search unnecessarily and can bury the\n                    # exact issue among unrelated same-denomination results.\n                    q=" ".join(str(x).strip() for x in [country,denom,year,alias] if str(x or "").strip())\n'''
if new in text:
    print('Country-qualified issue alias queries already applied.')
    raise SystemExit(0)
if old not in text:
    raise SystemExit('Expected issue-alias query construction not found; refusing unsafe patch.')
text=text.replace(old,new,1)
PATH.write_text(text,encoding='utf-8')
print('Applied country-qualified issue alias MA-Shops queries.')
