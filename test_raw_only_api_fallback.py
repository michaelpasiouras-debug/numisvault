#!/usr/bin/env python3
import importlib
b=importlib.import_module('numisvault_backend')

def run():
    p={'raw_query':'Greece 10 euro 2022 antikythera mechanism','coin':{'raw':'Greece 10 euro 2022 antikythera mechanism'},'currency':'EUR','include_shipping':True,'ship_to':'Greece'}
    qs=b.make_queries(p)
    c=p['coin']
    errs=[]
    if c.get('country')!='Greece': errs.append(f"country not propagated: {c.get('country')!r}")
    if str(c.get('year'))!='2022': errs.append(f"year not propagated: {c.get('year')!r}")
    if not c.get('denom'): errs.append('denom not propagated')
    # Once make_queries resolves raw-only input, hard filtering must actually
    # enforce that identity rather than accepting unrelated cheap coins.
    if b.passes_hard_filter('Greece 2 Euro 2022 Erasmus UNC',p): errs.append('wrong denomination passed after raw-only fallback')
    if b.passes_hard_filter('Greece 10 Euro 2022 Lord Byron Silver Proof',p): errs.append('wrong issue passed after raw-only fallback')
    if not b.passes_hard_filter('Greece 10 Euro 2022 Antikythera Mechanism Silver Proof',p): errs.append('correct issue rejected after raw-only fallback')

    # coin_search() resolves the identity first and stores the resolver's
    # numeric denomination_value in the payload. This exact production path
    # previously raised: AttributeError: 'float' object has no attribute
    # 'strip'. Keep it as a permanent endpoint-boundary regression.
    numeric={'raw_query':'Greece 10 euro 2022 antikythera mechanism','coin':{
        'raw':'Greece 10 euro 2022 antikythera mechanism','country':'Greece',
        'year':2022,'denom':10.0},'currency':'EUR'}
    try:
        numeric_qs=b.make_queries(numeric)
        if not numeric_qs: errs.append('numeric denomination produced no queries')
        if not any('10' in q for q in numeric_qs): errs.append(f'numeric denomination missing from queries: {numeric_qs!r}')
        # Shared scoring/normalization must also tolerate a typed API value.
        b.score_title('Greece 10 Euro 2022 Antikythera Mechanism',numeric)
    except Exception as e:
        errs.append(f'numeric resolver denomination crashed query/scoring path: {type(e).__name__}: {e}')
    if errs:
        print('FAIL')
        for e in errs: print(' -',e)
        return 1
    print('PASS raw-only API fallback:',c,'queries=',qs)
    return 0

if __name__=='__main__': raise SystemExit(run())
