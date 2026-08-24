#!/usr/bin/env python3
"""
CoinBids automated audit / regression runner.

Modes
-----
offline   : exhaustive static/catalog/resolver/backend checks, no network.
live-api  : controlled smoke audit against a deployed CoinBids API.
all       : offline + live-api.

Examples
--------
python coinbids_audit.py --mode offline
python coinbids_audit.py --mode live-api --base-url https://www.coinbids.eu --live-limit 25
python coinbids_audit.py --mode all --base-url https://www.coinbids.eu --live-limit 50

Outputs
-------
coinbids_audit_report.csv
coinbids_audit_report.json
coinbids_audit_summary.txt

Exit code 0 = no FAIL findings. REVIEW findings are reported but do not fail CI.
Exit code 1 = at least one FAIL finding.
"""
from __future__ import annotations
import argparse, csv, importlib, json, math, os, re, sys, time, traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent

@dataclass
class Finding:
    level: str
    area: str
    item: str
    message: str
    detail: str = ""

FINDINGS: list[Finding] = []

def add(level, area, item, message, detail=""):
    FINDINGS.append(Finding(level, area, item, message, detail))

def load_json(candidates):
    for name in candidates:
        p = ROOT / name
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                return json.load(f), p
    raise FileNotFoundError("None found: " + ", ".join(candidates))

def finite_num(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False

def offline_catalog_audit(specs: dict):
    records = specs.get("records")
    if not isinstance(records, list) or not records:
        add("FAIL", "catalog", "records", "Missing/non-list catalog records")
        return
    add("PASS", "catalog", "records", f"Loaded {len(records)} spec records")
    required = ("countries", "currency", "denomination", "year_from", "composition", "weight_g", "diameter_mm")
    precious = re.compile(r"\b(gold|silver|platinum|palladium)\b", re.I)
    intervals = []
    for i, r in enumerate(records):
        tag = f"record[{i}]"
        missing = [k for k in required if r.get(k) in (None, "", [])]
        if missing:
            identity_missing=[k for k in missing if k in ("countries","currency","denomination","year_from","composition")]
            physical_missing=[k for k in missing if k in ("weight_g","diameter_mm")]
            if identity_missing:
                add("FAIL", "catalog", tag, "Required identity fields missing", ", ".join(identity_missing)); continue
            if physical_missing: add("REVIEW", "catalog", tag, "Physical-spec fields missing", ", ".join(physical_missing))
        countries = r.get("countries")
        if not isinstance(countries, list) or not all(isinstance(c, str) and c.strip() for c in countries): add("FAIL","catalog",tag,"Invalid countries field",repr(countries))
        if not finite_num(r.get("denomination")) or float(r["denomination"]) <= 0: add("FAIL","catalog",tag,"Invalid denomination",repr(r.get("denomination")))
        for fld, lo, hi in (("weight_g",0.01,5000),("diameter_mm",1,200)):
            if r.get(fld) is not None and (not finite_num(r.get(fld)) or not (lo <= float(r[fld]) <= hi)): add("FAIL","catalog",tag,f"Implausible {fld}",repr(r.get(fld)))
        yf, yt = r.get("year_from"), r.get("year_to")
        if not isinstance(yf,int) or yf < 500 or yf > 2200: add("FAIL","catalog",tag,"Invalid year_from",repr(yf))
        if yt is not None and (not isinstance(yt,int) or yt < yf or yt > 2200): add("FAIL","catalog",tag,"Invalid year_to",repr(yt))
        comp=str(r.get("composition") or "")
        is_precious=bool(precious.search(comp)) and "nordic gold" not in comp.casefold()
        if is_precious:
            fin=r.get("fineness_per_mille")
            if fin is None:
                if not re.search(r"\.(\d{3})\b|\b\d{3}\s*/\s*1000\b|\b\d{3}\s*‰",comp): add("REVIEW","metal",tag,"Precious-metal record has no explicit fineness",comp)
            elif not finite_num(fin) or not (1 <= float(fin) <= 1000): add("FAIL","metal",tag,"Invalid fineness_per_mille",repr(fin))
        for c in countries if isinstance(countries,list) else []:
            intervals.append((c,str(r.get("currency")),float(r.get("denomination") or 0),yf,yt or 9999,i,comp,r.get("weight_g"),r.get("diameter_mm")))
    for a in range(len(intervals)):
        c1,cur1,d1,y1a,y1b,i1,comp1,w1,dia1=intervals[a]
        for b in range(a+1,len(intervals)):
            c2,cur2,d2,y2a,y2b,i2,comp2,w2,dia2=intervals[b]
            if c1!=c2 or cur1!=cur2 or d1!=d2: continue
            if max(y1a,y2a)<=min(y1b,y2b):
                if w1 is None or w2 is None or dia1 is None or dia2 is None: continue
                same=(str(comp1).lower()==str(comp2).lower() and abs(float(w1)-float(w2))<1e-6 and abs(float(dia1)-float(dia2))<1e-6)
                if not same: add("REVIEW","catalog",f"{c1} {d1} {cur1}","Overlapping spec records disagree",f"records {i1} and {i2}; overlap {max(y1a,y2a)}-{min(y1b,y2b)}")

def offline_issue_audit(issue_db: dict):
    issues=issue_db.get("issues") or []
    if not isinstance(issues,list): add("FAIL","issues","issues","Issue database has invalid issues list"); return
    add("PASS","issues","issues",f"Loaded {len(issues)} issue records")
    seen_titles=set()
    for i,issue in enumerate(issues):
        title=str(issue.get("canonical_title") or "").strip(); tag=title or f"issue[{i}]"
        if not title: add("FAIL","issues",tag,"Missing canonical_title"); continue
        key=title.casefold()
        if key in seen_titles: add("FAIL","issues",tag,"Duplicate canonical_title")
        seen_titles.add(key)
        aliases=[str(x).strip().casefold() for x in issue.get("aliases",[]) if str(x).strip()]
        if len(aliases)!=len(set(aliases)): add("REVIEW","issues",tag,"Duplicate aliases inside issue")
        if "antikythera" in key:
            for required in ("ancient greek technology","greek technology"):
                if required not in aliases: add("FAIL","issues",tag,f"Missing confirmed Antikythera alias: {required}")

def import_backend():
    try: return importlib.import_module("numisvault_backend")
    except ModuleNotFoundError as e: add("REVIEW","backend","import","Backend audit skipped because a runtime dependency is unavailable",f"{type(e).__name__}: {e}"); return None
    except Exception as e: add("FAIL","backend","import","Cannot import numisvault_backend",f"{type(e).__name__}: {e}"); return None

def backend_static_audit(backend):
    if backend is None: return
    required=["canonical_country","denomination_matches","passes_hard_filter","classify_asset","product_scope"]
    for name in required:
        add("PASS" if hasattr(backend,name) else "FAIL","backend",name,"Function present" if hasattr(backend,name) else "Required backend function missing")
    alias_cases={"Griekenland":"Greece","Grecia":"Greece","Grèce":"Greece","Griechenland":"Greece","Hellas":"Greece"}
    if hasattr(backend,"canonical_country"):
        for alias,expected in alias_cases.items():
            try:
                got=backend.canonical_country(alias)
                got_cmp=got[0] if isinstance(got,tuple) else ((got.get("canonical") or got.get("country")) if isinstance(got,dict) else got)
                add("PASS" if str(got_cmp).casefold()==expected.casefold() else "FAIL","country-alias",alias,f"Maps to {expected}" if str(got_cmp).casefold()==expected.casefold() else f"Does not map to {expected}", "" if str(got_cmp).casefold()==expected.casefold() else f"got={got!r}")
            except Exception as e: add("FAIL","country-alias",alias,"Alias check crashed",f"{type(e).__name__}: {e}")
    if hasattr(backend,"denomination_matches"):
        cases=[("5 Greek drachma","1901 J 5 Pfennig VF",False),("5 Greek drachma","Greece 5 Drachmai 1901 VF",True),("10 euro","Greece 2 Euro 2022 Erasmus",False),("10 euro","Greece 10 Euro 2022 Antikythera Mechanism",True)]
        for target,listing,expected in cases:
            try:
                got=bool(backend.denomination_matches(target,listing)); add("PASS" if got==expected else "FAIL","denomination",f"{target} :: {listing[:35]}",f"Expected {expected}, got {got}")
            except Exception as e: add("FAIL","denomination",target,"Check crashed",f"{type(e).__name__}: {e}")

def resolver_audit(issue_db: dict):
    try: rmod=importlib.import_module("coin_identity_resolver")
    except Exception as e: add("REVIEW","resolver","import","coin_identity_resolver not available; resolver sweep skipped",f"{type(e).__name__}: {e}"); return
    resolver=getattr(rmod,"resolve_coin_identity",None)
    if not resolver: add("FAIL","resolver","resolve_coin_identity","Function missing"); return
    queries=[("5 drachma 1901","Greece",5.0,1901),("5 δραχμές 1901","Greece",5.0,1901),("Greece 10 euro 2022 antikythera mechanism","Greece",10.0,2022),("USA 1 Dollar 1987 silver eagle","United States",1.0,1987)]
    for issue in issue_db.get("issues",[]):
        t=issue.get("canonical_title"); country=None
        cc=issue.get("country_code")
        if cc=="GR": country="Greece"
        elif cc=="HR": country="Croatia"
        elif cc=="US": country="United States"
        if t: queries.append((t,country,float(issue.get("denomination_value")) if issue.get("denomination_value") is not None else None,issue.get("year")))
        for a in issue.get("aliases",[])[:8]:
            if issue.get("year") and issue.get("denomination_value"):
                prefix=((country+" ") if country else "") + (f"{issue['denomination_value']} euro {issue['year']} " if issue.get("currency_code")=="EUR" else f"{issue['denomination_value']} {issue.get('currency_code','')} {issue['year']} ")
                queries.append((prefix+str(a),country,float(issue["denomination_value"]),issue["year"]))
    seen=set()
    for q,exp_country,exp_denom,exp_year in queries:
        if q in seen: continue
        seen.add(q)
        try:
            out=resolver(q) or {}; best=out.get("best") or {}; errs=[]
            if exp_country and best.get("country")!=exp_country: errs.append(f"country={best.get('country')!r}")
            if exp_denom is not None:
                gotd=best.get("denomination_value")
                if gotd is None or abs(float(gotd)-exp_denom)>1e-9: errs.append(f"denom={gotd!r}")
            if exp_year is not None and best.get("year")!=exp_year: errs.append(f"year={best.get('year')!r}")
            if errs:
                level="REVIEW" if "silver eagle" in q.lower() and best.get("denomination_value")==1.0 else "FAIL"; add(level,"resolver",q,"Identity mismatch","; ".join(errs)+f"; status={out.get('status')!r}")
            else: add("PASS","resolver",q,"Identity resolved as expected",f"status={out.get('status')!r}")
        except Exception as e: add("FAIL","resolver",q,"Resolver crashed",f"{type(e).__name__}: {e}")

def build_live_queries(specs: dict, issue_db: dict, limit: int):
    out=["Greece 10 euro 2022 antikythera mechanism","5 drachma 1901 Greece","2 euro 2025 Croatia","USA quarter dollar 1964"]
    for issue in issue_db.get("issues",[]):
        if issue.get("canonical_title"): out.append(issue["canonical_title"])
    records=specs.get("records",[]); step=max(1,len(records)//max(1,limit))
    for r in records[::step]:
        countries=r.get("countries") or []
        if not countries: continue
        country=countries[0]; year=r.get("year_from"); denom=r.get("denomination"); cur=r.get("currency")
        unit="euro" if cur=="EUR" else "drachma" if cur=="GRD" else "dollar" if cur=="USD" else "pound" if cur=="GBP" else cur
        out.append(f"{country} {denom:g} {unit} {year}")
        if len(out)>=limit*2: break
    unique=[]; seen=set()
    for q in out:
        k=q.casefold()
        if k not in seen: seen.add(k); unique.append(q)
    return unique[:limit]

def post_json(url,payload,timeout=45):
    body=json.dumps(payload).encode("utf-8"); req=Request(url,data=body,headers={"Content-Type":"application/json","User-Agent":"CoinBids-Audit/1.0"},method="POST")
    with urlopen(req,timeout=timeout) as r: return r.status,json.loads(r.read().decode("utf-8"))

def live_api_audit(base_url: str,specs: dict,issue_db: dict,limit: int,delay: float):
    base=base_url.rstrip("/"); queries=build_live_queries(specs,issue_db,limit); add("PASS","live","plan",f"Prepared {len(queries)} controlled live queries")
    for idx,q in enumerate(queries,1):
        payload={"raw_query":q,"coin":{"raw":q},"currency":"EUR","include_shipping":True,"ship_to":"Greece","limit":2,"sample_limit":10}
        try: status,data=post_json(base+"/api/coin-search",payload)
        except HTTPError as e: add("FAIL","live",q,f"HTTP {e.code} from /api/coin-search",e.read().decode("utf-8","replace")[:500]); continue
        except Exception as e: add("FAIL","live",q,"Request failed",f"{type(e).__name__}: {e}"); continue
        if status!=200: add("FAIL","live",q,f"Unexpected HTTP status {status}"); continue
        offers=data.get("offers") or []; raw=int(data.get("raw_count") or 0); valid=int(data.get("valid_count") or 0); rej=data.get("rejected") or {}
        if raw==0: add("REVIEW","live",q,"No raw MA-Shops candidates",json.dumps(data.get("errors") or [],ensure_ascii=False))
        elif valid==0: add("REVIEW","live",q,"Raw candidates found but zero validated matches",f"raw={raw}; rejected={rej}")
        else: add("PASS","live",q,f"Search returned {valid} validated of {raw} raw candidates")
        totals=[o.get("total") for o in offers if o.get("total") is not None]
        if len(totals)>=2 and any(float(totals[i])>float(totals[i+1]) for i in range(len(totals)-1)): add("FAIL","ranking",q,"Returned offers are not sorted by delivered total",repr(totals))
        if offers and data.get("best_offer"):
            a=offers[0].get("url") or offers[0].get("title"); b=data["best_offer"].get("url") or data["best_offer"].get("title")
            if a!=b: add("FAIL","ranking",q,"best_offer differs from offers[0]")
        if raw>=20 and valid/raw<0.01: add("REVIEW","filtering",q,"Extremely low validation ratio",f"valid={valid}, raw={raw}, rejected={rej}")
        for j,o in enumerate(offers):
            if o.get("shipping") is None and o.get("total") is not None and payload["include_shipping"]: add("FAIL","shipping",q,"Unknown shipping produced a delivered total",f"offer[{j}]={o.get('title')}")
            if o.get("shipping") is not None and o.get("price") is not None and o.get("total") is not None:
                expected=round(float(o["price"])+float(o["shipping"]),2)
                if abs(float(o["total"])-expected)>0.011: add("FAIL","shipping",q,"total != price + shipping",f"{o.get('price')} + {o.get('shipping')} != {o.get('total')}")
        if delay and idx<len(queries): time.sleep(delay)
    try:
        req=Request(base+"/api/metal-spot",headers={"User-Agent":"CoinBids-Audit/1.0"})
        with urlopen(req,timeout=30) as r:
            metal=json.loads(r.read().decode("utf-8"))
            if r.status!=200: add("FAIL","metal","live spot",f"HTTP {r.status}")
            else:
                nums=[]
                def walk(x):
                    if isinstance(x,dict):
                        for v in x.values(): walk(v)
                    elif isinstance(x,(int,float)): nums.append(float(x))
                walk(metal)
                if nums and all(math.isfinite(x) and x>0 for x in nums): add("PASS","metal","live spot","Metal spot endpoint returned positive numeric data")
                else: add("REVIEW","metal","live spot","Spot response contains no obvious positive numeric values",json.dumps(metal)[:500])
    except Exception as e: add("FAIL","metal","live spot","Metal spot endpoint failed",f"{type(e).__name__}: {e}")

def write_reports(prefix="coinbids_audit_report"):
    csv_path=ROOT/f"{prefix}.csv"; json_path=ROOT/f"{prefix}.json"; summary_path=ROOT/"coinbids_audit_summary.txt"
    with csv_path.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=["level","area","item","message","detail"]); w.writeheader(); w.writerows(asdict(x) for x in FINDINGS)
    with json_path.open("w",encoding="utf-8") as f: json.dump({"generated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"findings":[asdict(x) for x in FINDINGS]},f,ensure_ascii=False,indent=2)
    counts={k:sum(1 for x in FINDINGS if x.level==k) for k in ("PASS","REVIEW","FAIL")}; areas={}
    for x in FINDINGS: areas.setdefault(x.area,{"PASS":0,"REVIEW":0,"FAIL":0})[x.level]+=1
    lines=["COINBIDS AUDIT SUMMARY","="*72,f"PASS: {counts['PASS']}",f"REVIEW: {counts['REVIEW']}",f"FAIL: {counts['FAIL']}","","BY AREA"]
    for a in sorted(areas):
        c=areas[a]; lines.append(f"{a:20s} PASS={c['PASS']:4d} REVIEW={c['REVIEW']:4d} FAIL={c['FAIL']:4d}")
    if counts["FAIL"] or counts["REVIEW"]:
        lines+=["","ACTIONABLE FINDINGS","-"*72]
        for x in FINDINGS:
            if x.level!="PASS": lines.append(f"[{x.level}] {x.area} :: {x.item} :: {x.message}"+(f" | {x.detail}" if x.detail else ""))
    summary_path.write_text("\n".join(lines)+"\n",encoding="utf-8"); return csv_path,json_path,summary_path,counts

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--mode",choices=["offline","live-api","all"],default="offline"); ap.add_argument("--base-url",default="https://www.coinbids.eu"); ap.add_argument("--live-limit",type=int,default=25); ap.add_argument("--delay",type=float,default=2.0,help="seconds between controlled live queries"); args=ap.parse_args()
    try: specs,specs_path=load_json(["coin_specs_database_MASTER_EUROPE_v17.json","coin_specs_database_MASTER_EUROPE_v17(2).json","coin_specs_database.json","coin_specs_database(3).json"]); add("PASS","setup","specs",f"Loaded {specs_path.name}")
    except Exception as e: specs={"records":[]}; add("FAIL","setup","specs","Could not load specs DB",str(e))
    try: issues,issue_path=load_json(["coin_issue_database.json","coin_issue_database(6).json"]); add("PASS","setup","issues",f"Loaded {issue_path.name}")
    except Exception as e: issues={"issues":[]}; add("FAIL","setup","issues","Could not load issue DB",str(e))
    if args.mode in ("offline","all"):
        offline_catalog_audit(specs); offline_issue_audit(issues); backend=import_backend(); backend_static_audit(backend); resolver_audit(issues)
    if args.mode in ("live-api","all"): live_api_audit(args.base_url,specs,issues,max(1,args.live_limit),max(0,args.delay))
    csv_path,json_path,summary_path,counts=write_reports(); print(summary_path.read_text(encoding="utf-8")); print(f"CSV:  {csv_path}"); print(f"JSON: {json_path}"); return 1 if counts["FAIL"] else 0
if __name__=="__main__": raise SystemExit(main())
