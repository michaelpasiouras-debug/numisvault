#!/usr/bin/env python3
import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('backend',Path('numisvault_backend.py'))
backend=importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

payload={
    'raw_query':'Greece 10 euro 2022 mechanism',
    'coin':{'country':'Greece','denom':'10 euro','year':'2022','theme':'mechanism'}
}
qs=backend.make_queries(payload)
print('queries:',qs)
assert any(q.lower()=='greece 10 euro 2022 antikythera mechanism' for q in qs), qs
assert qs[0].lower()=='greece 10 euro 2022 mechanism', qs
print('PASS: issue-specific MA-Shops query keeps country and exact raw query stays first')
