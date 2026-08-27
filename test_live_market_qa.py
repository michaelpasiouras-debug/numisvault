#!/usr/bin/env python3
"""Live CoinBids market QA.

Definition of PASS for a market search:
1) identity returned by /api/resolve-coin is the expected coin;
2) /api/coin-search returns every validated offer requested by this QA run;
3) CoinBids' cheapest_known_delivered equals the mathematical minimum delivered
   total among those validated offers;
4) multilingual aliases for the same coin return the same cheapest anchor.

This test intentionally uses the deployed production API because "cheapest" is
live market data. It consumes no Numista quota directly.
"""
from __future__ import annotations
import json, os, sys, time, urllib.request, urllib.error

BASE=os.environ.get('BASE_URL','https://www.coinbids.eu').rstrip('/')
SHIP_TO=os.environ.get('SHIP_TO','Greece')

CASES=[
    {
        'name':'Antikythera 2022',
        'query':'Greece 10 euro 2022 antikythera mechanism',
        'country':'Greece','denom':10.0,'year':2022,
        'aliases':[
            'Griekenland 10 euro 2022 antikythera mechanism',
            'Griechenland 10 euro 2022 antikythera mechanism',
            'Grecia 10 euro 2022 meccanismo anticitera',
            'Grèce 10 euro 2022 mecanisme anticythere',
            'Ελλάδα 10 Ευρώ 2022 μηχανισμός αντικυθήρων',
        ],
    },
    {
        'name':'Greek Erasmus 2 euro 2022',
        'query':'Greece 2 euro 2022 Erasmus',
        'country':'Greece','denom':2.0,'year':2022,
        'aliases':['Griechenland 2 euro 2022 Erasmus','Griekenland 2 euro 2022 Erasmus'],
    },
    {
        'name':'Greek 5 drachma 1901',
        'query':'Greece 5 drachma 1901',
        'country':'Greece','denom':5.0,'year':1901,
        'aliases':['Ελλάδα 5 δραχμές 1901'],
    },
]


def request_json(path,payload=None,timeout=100,attempts=3):
    data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode('utf-8')
    headers={'User-Agent':'CoinBids-Live-Market-QA/1.0','Accept':'application/json'}
    if data is not None: headers['Content-Type']='application/json'
    last=None
    for i in range(attempts):
        try:
            req=urllib.request.Request(BASE+path,data=data,headers=headers,method='POST' if data is not None else 'GET')
            with urllib.request.urlopen(req,timeout=timeout) as r:
                raw=r.read().decode('utf-8','replace')
                return r.status,json.loads(raw)
        except urllib.error.HTTPError as e:
            body=e.read().decode('utf-8','replace'); last=f'HTTP {e.code}: {body[:500]}'
            if e.code not in (502,503,504,429) or i==attempts-1: raise
        except Exception as e:
            last=f'{type(e).__name__}: {e}'
            if i==attempts-1: raise
        time.sleep(1.5*(i+1))
    raise RuntimeError(last)


def identity(q):
    st,d=request_json('/api/resolve-coin',{'text':q},timeout=40)
    b=d.get('best') or d.get('coin') or {}
    denom=b.get('denomination_value')
    return {
        'status':st,'country':b.get('country'),'denom':float(denom) if denom is not None else None,
        'year':int(b.get('year')) if b.get('year') is not None else None,
        'resolver_status':d.get('status')
    }


def delivered(o):
    if not isinstance(o,dict): return None
    for k in ('total','delivered_total','all_in','all_in_price'):
        v=o.get(k)
        if isinstance(v,(int,float)): return round(float(v),2)
    p=o.get('price'); s=o.get('shipping')
    if isinstance(p,(int,float)) and isinstance(s,(int,float)):
        return round(float(p)+float(s),2)
    return None


def market(q):
    # High limits are deliberate: QA must see all validated results returned by
    # the source scan, not merely the first UI cards.
    payload={'raw_query':q,'coin':{'raw':q},'currency':'EUR','include_shipping':True,
             'ship_to':SHIP_TO,'limit':200,'sample_limit':250}
    st,d=request_json('/api/coin-search',payload,timeout=120)
    offers=d.get('offers') or []
    valid=int(d.get('valid_count') or len(offers))
    if valid<1: raise AssertionError(f'no validated offers; response={str(d)[:900]}')
    # If backend says it validated more offers than it returned, this QA cannot
    # prove global minimum. Treat that as failure rather than pretending.
    if len(offers)<valid:
        raise AssertionError(f'only {len(offers)}/{valid} validated offers exposed; cannot verify cheapest globally')
    known=[(delivered(o),o) for o in offers]
    known=[x for x in known if x[0] is not None]
    if not known: raise AssertionError('no validated offer has known delivered total')
    true_min,true_offer=min(known,key=lambda x:x[0])
    chosen=d.get('cheapest_known_delivered') or d.get('best_offer')
    if not isinstance(chosen,dict): raise AssertionError('API did not expose chosen cheapest offer')
    chosen_total=delivered(chosen)
    if chosen_total is None: raise AssertionError(f'chosen cheapest has no delivered total: {chosen}')
    if abs(chosen_total-true_min)>0.01:
        raise AssertionError(f'CHEAPEST WRONG: chosen={chosen_total:.2f}, actual min={true_min:.2f}, true offer={true_offer.get("title")!r}')
    return {'best':true_min,'valid':valid,'url':true_offer.get('url'),'title':true_offer.get('title'),'cache':d.get('cache')}


def main():
    failures=[]; rows=[]; identity_pass=0; cheapest_pass=0; alias_pass=0
    for case in CASES:
        q=case['query']; name=case['name']
        try:
            got=identity(q)
            ok=(got['country']==case['country'] and got['denom'] is not None and abs(got['denom']-case['denom'])<1e-9 and got['year']==case['year'])
            if not ok: raise AssertionError(f'identity got={got}')
            identity_pass+=1
            m=market(q); cheapest_pass+=1
            alias_results=[]
            for aq in case.get('aliases',[]):
                ai=identity(aq)
                if not (ai['country']==case['country'] and ai['denom'] is not None and abs(ai['denom']-case['denom'])<1e-9 and ai['year']==case['year']):
                    raise AssertionError(f'alias identity wrong for {aq!r}: {ai}')
                am=market(aq); alias_results.append((aq,am['best']))
                if abs(am['best']-m['best'])>0.01:
                    raise AssertionError(f'alias cheapest divergence: canonical={m["best"]}, {aq!r}={am["best"]}')
            alias_pass+=1
            rows.append({'case':name,'status':'PASS','identity':f'{case["country"]} {case["denom"]} {case["year"]}','cheapest':m['best'],'valid_offers':m['valid'],'aliases':len(alias_results)})
            print(f'PASS | {name} | identity OK | cheapest EUR {m["best"]:.2f} among {m["valid"]} validated offers | aliases OK')
        except Exception as e:
            failures.append(f'{name}: {type(e).__name__}: {e}')
            rows.append({'case':name,'status':'FAIL','error':str(e)})
            print(f'FAIL | {name} | {type(e).__name__}: {e}')

    summary={
        'base_url':BASE,'ship_to':SHIP_TO,'cases':len(CASES),
        'identity_pass':identity_pass,'cheapest_pass':cheapest_pass,'alias_group_pass':alias_pass,
        'failures':failures,'rows':rows,'overall':'PASS' if not failures else 'FAIL'
    }
    with open('coin_search_live_qa.json','w',encoding='utf-8') as f: json.dump(summary,f,ensure_ascii=False,indent=2)
    with open('coin_search_live_qa_summary.md','w',encoding='utf-8') as f:
        f.write('# CoinBids Live Search QA\n\n')
        f.write(f'**OVERALL: {summary["overall"]}**  \n')
        f.write(f'Identity: **{identity_pass}/{len(CASES)}**  \n')
        f.write(f'Cheapest-offer proof: **{cheapest_pass}/{len(CASES)}**  \n')
        f.write(f'Multilingual market equivalence: **{alias_pass}/{len(CASES)}**\n\n')
        f.write('| Case | Result | Cheapest | Validated offers |\n|---|---:|---:|---:|\n')
        for r in rows:
            f.write(f'| {r["case"]} | {r["status"]} | {("€%.2f"%r["cheapest"]) if "cheapest" in r else "—"} | {r.get("valid_offers","—")} |\n')
        if failures:
            f.write('\n## Failures\n')
            for x in failures:f.write(f'- {x}\n')
    print(f'OVERALL: {summary["overall"]}; identity={identity_pass}/{len(CASES)}; cheapest={cheapest_pass}/{len(CASES)}; aliases={alias_pass}/{len(CASES)}')
    return 1 if failures else 0

if __name__=='__main__':
    sys.exit(main())
