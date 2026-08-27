#!/usr/bin/env python3
"""Live CoinBids market QA: identity + provable cheapest delivered offer."""
from __future__ import annotations
import json, os, sys, time, urllib.request, urllib.error
BASE=os.environ.get('BASE_URL','https://www.coinbids.eu').rstrip('/')
SHIP_TO=os.environ.get('SHIP_TO','Greece')
CASES=[
 {'name':'Antikythera 2022','query':'Greece 10 euro 2022 antikythera mechanism','country':'Greece','denom':10.0,'year':2022,'aliases':['Griekenland 10 euro 2022 antikythera mechanism','Griechenland 10 euro 2022 antikythera mechanism','Grecia 10 euro 2022 meccanismo anticitera','Grèce 10 euro 2022 mecanisme anticythere','Ελλάδα 10 Ευρώ 2022 μηχανισμός αντικυθήρων']},
 {'name':'Greek Erasmus 2 euro 2022','query':'Greece 2 euro 2022 Erasmus','country':'Greece','denom':2.0,'year':2022,'aliases':['Griechenland 2 euro 2022 Erasmus','Griekenland 2 euro 2022 Erasmus']},
 {'name':'Greek 5 drachma 1901','query':'Greece 5 drachma 1901','country':'Greece','denom':5.0,'year':1901,'aliases':['Ελλάδα 5 δραχμές 1901']},
]
def request_json(path,payload=None,timeout=120,attempts=3):
 data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
 h={'User-Agent':'CoinBids-Live-Market-QA/2.0','Accept':'application/json'}
 if data is not None:h['Content-Type']='application/json'
 for i in range(attempts):
  try:
   r=urllib.request.Request(BASE+path,data=data,headers=h,method='POST' if data is not None else 'GET')
   with urllib.request.urlopen(r,timeout=timeout) as x:return x.status,json.loads(x.read().decode('utf-8','replace'))
  except (urllib.error.URLError,TimeoutError,urllib.error.HTTPError):
   if i==attempts-1:raise
   time.sleep(1.5*(i+1))
def identity(q):
 _,d=request_json('/api/resolve-coin',{'text':q},40);b=d.get('best') or d.get('coin') or {};v=b.get('denomination_value')
 return b.get('country'),float(v) if v is not None else None,int(b.get('year')) if b.get('year') is not None else None
def delivered(o):
 for k in ('total','delivered_total','all_in','all_in_price'):
  if isinstance(o.get(k),(int,float)):return round(float(o[k]),2)
 p,s=o.get('price'),o.get('shipping')
 return round(float(p)+float(s),2) if isinstance(p,(int,float)) and isinstance(s,(int,float)) else None
def market(q):
 payload={'raw_query':q,'coin':{'raw':q},'currency':'EUR','include_shipping':True,'ship_to':SHIP_TO,'limit':200,'sample_limit':15,'qa_full_evidence':True}
 _,d=request_json('/api/coin-search',payload)
 offers=d.get('offers') or [];valid=int(d.get('valid_count') or len(offers))
 if valid<1:raise AssertionError('no validated offers')
 if len(offers)<valid:raise AssertionError(f'only {len(offers)}/{valid} validated offers exposed')
 known=[(delivered(o),o) for o in offers];known=[x for x in known if x[0] is not None]
 if not known:raise AssertionError('no known delivered totals')
 actual,actual_offer=min(known,key=lambda x:x[0]);chosen=d.get('cheapest_known_delivered') or d.get('best_offer')
 chosen_total=delivered(chosen or {})
 if chosen_total is None or abs(chosen_total-actual)>.01:raise AssertionError(f'CHEAPEST WRONG chosen={chosen_total} actual={actual} title={actual_offer.get("title")!r}')
 return actual,valid
def main():
 rows=[];fails=[];ip=cp=ap=0
 for c in CASES:
  try:
   got=identity(c['query']);exp=(c['country'],c['denom'],c['year'])
   if got!=exp:raise AssertionError(f'identity {got} != {exp}')
   ip+=1;best,valid=market(c['query']);cp+=1
   for q in c['aliases']:
    if identity(q)!=exp:raise AssertionError(f'alias identity wrong: {q}')
    ab,_=market(q)
    if abs(ab-best)>.01:raise AssertionError(f'alias cheapest divergence canonical={best} alias={ab}: {q}')
   ap+=1;rows.append({'case':c['name'],'status':'PASS','cheapest':best,'valid_offers':valid});print(f'PASS | {c["name"]} | EUR {best:.2f} | {valid} offers')
  except Exception as e:
   fails.append(f'{c["name"]}: {type(e).__name__}: {e}');rows.append({'case':c['name'],'status':'FAIL','error':str(e)});print('FAIL |',c['name'],'|',e)
 summary={'identity_pass':ip,'cheapest_pass':cp,'alias_group_pass':ap,'cases':len(CASES),'rows':rows,'failures':fails,'overall':'PASS' if not fails else 'FAIL'}
 open('coin_search_live_qa.json','w',encoding='utf-8').write(json.dumps(summary,ensure_ascii=False,indent=2))
 with open('coin_search_live_qa_summary.md','w',encoding='utf-8') as f:
  f.write(f'# CoinBids Live Search QA\n\n**OVERALL: {summary["overall"]}**  \nIdentity: **{ip}/{len(CASES)}**  \nCheapest-offer proof: **{cp}/{len(CASES)}**  \nMultilingual equivalence: **{ap}/{len(CASES)}**\n\n')
  for r in rows:f.write(f'- **{r["case"]}**: {r["status"]}'+(f' — €{r["cheapest"]:.2f} among {r["valid_offers"]} validated offers' if 'cheapest' in r else f' — {r.get("error")}')+'\n')
 print(f'OVERALL {summary["overall"]}: identity={ip}/{len(CASES)} cheapest={cp}/{len(CASES)} aliases={ap}/{len(CASES)}')
 return 1 if fails else 0
if __name__=='__main__':sys.exit(main())
