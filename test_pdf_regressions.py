#!/usr/bin/env python3
import json
import importlib
from pathlib import Path

backend = importlib.import_module('numisvault_backend')
failures=[]

def check(label, cond):
    if not cond:
        failures.append(label)

# PDF bug 1/2: inferred Greece must not reject Cretan State 5 Drachmai 1901.
coin={'country':'Greece','denom':'5 drachma','year':'1901','theme':'','variant':''}
check('inferred-country Crete listing rejected',
      backend.passes_hard_filter('Kreta / Crete 5 Drachmai 1901 F-VF',
                                 {'coin':coin,'raw_query':'5 drachmai 1901'}) is True)
check('explicit Greece must remain strict',
      backend.passes_hard_filter('Kreta / Crete 5 Drachmai 1901 F-VF',
                                 {'coin':coin,'raw_query':'Greece 5 drachmai 1901'}) is False)

# Melt-value source must contain usable physical specs for the 1901 Cretan coin.
specs=json.loads(Path('coin_specs_database.json').read_text(encoding='utf-8'))['records']
crete=next((r for r in specs if float(r.get('denomination') or 0)==5.0
            and r.get('year_from')==1901 and r.get('year_to')==1901
            and any(str(c).lower() in {'crete','kreta'} for c in r.get('countries',[]))), None)
check('missing Crete 1901 spec record', crete is not None)
if crete:
    check('Crete 1901 fineness not 900', crete.get('fineness_per_mille')==900)
    check('Crete 1901 weight not 25g', crete.get('weight_g')==25.0)
    check('Crete 1901 fine metal not 22.5g', crete.get('fine_metal_g')==22.5)

# PDF bug 3: a request for non-existent Vatican 2e 2024 "Perugino" must never
# accept either of the two real Vatican 2024 commemoratives.
vcoin={'country':'Vatican','denom':'2 euro','year':'2024','theme':'perugino','variant':''}
for title in [
    "Vatican 2 Euro 2024 Thomas d'Aquin BU",
    'Vatican 2 Euro 2024 Saint Thomas Aquinas Proof',
    'Vatican 2 Euro 2024 Guglielmo Marconi BU',
]:
    check('wrong Vatican issue survived: '+title,
          backend.passes_hard_filter(title, {'coin':vcoin,'raw_query':'Vatican 2 euro 2024 perugino'}) is False)

issues=json.loads(Path('coin_issue_database.json').read_text(encoding='utf-8'))['issues']
v2024=[i for i in issues if i.get('country_code')=='VA' and i.get('year')==2024 and i.get('denomination_value')==2]
check('Vatican 2024 issue ontology incomplete', len(v2024)>=2)

if failures:
    print('FAIL')
    for f in failures: print(' -',f)
    raise SystemExit(1)
print('PASS — PDF regression cases locked')
