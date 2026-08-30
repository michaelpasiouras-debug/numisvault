from pathlib import Path

src = Path('numisvault_backend.py').read_text(encoding='utf-8')
required = [
    'trace_enabled=bool(payload.get("qa_full_evidence"))',
    '"generated_queries":queries',
    '"per_query":query_trace',
    '"candidates":candidate_trace',
    '"winner":({',
    '"funnel_trace":funnel_trace if trace_enabled else None',
    'd.pop("_source_queries",None)',
]
missing=[x for x in required if x not in src]
if missing:
    print('FAIL — candidate funnel trace markers missing:')
    for x in missing: print(' -',x)
    raise SystemExit(1)
print('PASS — QA-only candidate funnel trace is wired into coin-search')
