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
    if errs:
        print('FAIL')
        for e in errs: print(' -',e)
        return 1
    print('PASS raw-only API fallback:',c,'queries=',qs)
    return 0

if __name__=='__main__': raise SystemExit(run())
