#!/usr/bin/env python3
"""CoinBids catalog builder — safe test-mode MA-Shops consensus harvester.

Imports the existing numisvault_backend.py as an engine. It does NOT modify the
production backend and defaults to dry-run. Database writes require --write.
"""
from __future__ import annotations
import argparse, importlib.util, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

HERE=Path(__file__).resolve().parent
DEFAULT_BACKEND=os.getenv("COINBIDS_BACKEND_PATH", str(HERE/"numisvault_backend.py"))

TEST_COINS=[
 {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1922","variant":"Peace Dollar"},
 {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1923","variant":"Peace Dollar"},
 {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1924","variant":"Peace Dollar"},
 {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1925","variant":"Peace Dollar"},
 {"country":"United States","countryEN":"United States","denom":"1/2 Dollar","year":"1964","variant":"Kennedy Half Dollar"},
]

def load_backend(path):
    p=Path(path)
    if not p.exists(): raise FileNotFoundError(f"Backend not found: {p}")
    spec=importlib.util.spec_from_file_location("coinbids_backend_for_catalog",p)
    mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
    if not hasattr(mod,"mashops_spec_fallback"): raise RuntimeError("Backend lacks mashops_spec_fallback()")
    return mod

def coin_key(c): return "|".join(str(c.get(k) or "").strip().lower() for k in ("countryEN","denom","year","variant"))
def query_for(c): return " ".join(str(c.get(k) or "").strip() for k in ("countryEN","denom","year","variant") if c.get(k)).strip()

def normalized_observations(coin,spec):
    out=[]; seen=set()
    for field, rows in (spec.get("spec_evidence_by_field") or {}).items():
        for r in rows or []:
            sig=(r.get("dealer"),r.get("url"),field,str(r.get("value")))
            if sig in seen: continue
            seen.add(sig)
            out.append({"coin_key":coin_key(coin),"country":coin.get("countryEN") or coin.get("country"),"denomination":coin.get("denom"),"year":coin.get("year"),"variant":coin.get("variant"),"field_name":field,"field_value":r.get("value"),"dealer":r.get("dealer"),"source_url":r.get("url"),"listing_title":r.get("title"),"evidence_text":r.get("evidence"),"observed_at":datetime.now(timezone.utc).isoformat()})
    return out

def print_result(c,spec):
    print("\n"+"="*72); print("Coin:",query_for(c))
    if not spec: print("STATUS: NO 2-DEALER CONSENSUS"); return
    print("Item pages checked:",spec.get("listings_checked","?")); print("Evidence counts:",json.dumps(spec.get("spec_evidence_counts",{}),ensure_ascii=False))
    for label,key,suffix in [("Metal","primary_metal",""),("Fineness","fineness_per_mille"," ‰"),("Weight","weight_g"," g"),("Diameter","diameter_mm"," mm"),("Fine metal","fine_metal_g"," g")]:
        if spec.get(key) is not None: print(f"{label}: {spec[key]}{suffix}")
    print("STATUS: VERIFIED CONSENSUS");
    for u in spec.get("spec_source_urls") or []: print("  source:",u)

def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS coin_catalog_builder (
          coin_key text PRIMARY KEY, country text NOT NULL, denomination text NOT NULL,
          year_text text, variant text, composition text, primary_metal text,
          fineness_per_mille integer, weight_g double precision, diameter_mm double precision,
          fine_metal_g double precision, confidence double precision, spec_source text,
          source_urls jsonb NOT NULL DEFAULT '[]'::jsonb, evidence_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
          listings_checked integer, verified boolean NOT NULL DEFAULT false,
          updated_at timestamptz NOT NULL DEFAULT now());''')
        cur.execute('''CREATE TABLE IF NOT EXISTS coin_observations_builder (
          id bigserial PRIMARY KEY, coin_key text NOT NULL, country text, denomination text,
          year_text text, variant text, field_name text NOT NULL, field_value text,
          dealer text, source_url text, listing_title text, evidence_text text,
          observed_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(coin_key,field_name,dealer,source_url,field_value));''')
    conn.commit()

def write_result(backend,coin,spec):
    if not spec: return 0
    conn=backend._get_pg_connection()
    if conn is None: raise RuntimeError("DATABASE_URL unavailable or PostgreSQL connection failed")
    try:
        ensure_schema(conn); obs=normalized_observations(coin,spec)
        with conn.cursor() as cur:
            for r in obs:
                cur.execute('''INSERT INTO coin_observations_builder
                (coin_key,country,denomination,year_text,variant,field_name,field_value,dealer,source_url,listing_title,evidence_text,observed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING''',(r["coin_key"],r["country"],r["denomination"],str(r["year"] or ""),r["variant"],r["field_name"],str(r["field_value"]),r["dealer"],r["source_url"],r["listing_title"],r["evidence_text"],r["observed_at"]))
            cur.execute('''INSERT INTO coin_catalog_builder
            (coin_key,country,denomination,year_text,variant,composition,primary_metal,fineness_per_mille,weight_g,diameter_mm,fine_metal_g,confidence,spec_source,source_urls,evidence_counts,listings_checked,verified,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,true,now())
            ON CONFLICT (coin_key) DO UPDATE SET composition=EXCLUDED.composition,primary_metal=EXCLUDED.primary_metal,
            fineness_per_mille=EXCLUDED.fineness_per_mille,weight_g=EXCLUDED.weight_g,diameter_mm=EXCLUDED.diameter_mm,
            fine_metal_g=EXCLUDED.fine_metal_g,confidence=EXCLUDED.confidence,spec_source=EXCLUDED.spec_source,
            source_urls=EXCLUDED.source_urls,evidence_counts=EXCLUDED.evidence_counts,listings_checked=EXCLUDED.listings_checked,
            verified=true,updated_at=now()''',(coin_key(coin),coin.get("countryEN") or coin.get("country"),coin.get("denom"),str(coin.get("year") or ""),coin.get("variant"),spec.get("composition"),spec.get("primary_metal"),spec.get("fineness_per_mille"),spec.get("weight_g"),spec.get("diameter_mm"),spec.get("fine_metal_g"),spec.get("confidence"),spec.get("spec_source"),json.dumps(spec.get("spec_source_urls") or []),json.dumps(spec.get("spec_evidence_counts") or {}),spec.get("listings_checked")))
        conn.commit(); return len(obs)
    except Exception: conn.rollback(); raise
    finally: backend._release_pg_connection(conn)

def main():
    ap=argparse.ArgumentParser(description="CoinBids MA-Shops catalog builder (dry-run by default)")
    ap.add_argument("--backend",default=DEFAULT_BACKEND); ap.add_argument("--write",action="store_true",help="write to isolated *_builder tables")
    ap.add_argument("--limit",type=int,default=5); ap.add_argument("--country"); ap.add_argument("--denom"); ap.add_argument("--year"); ap.add_argument("--variant",default="")
    args=ap.parse_args(); backend=load_backend(args.backend)
    coins=[{"country":args.country,"countryEN":args.country,"denom":args.denom,"year":args.year,"variant":args.variant}] if args.country and args.denom and args.year else TEST_COINS[:max(1,args.limit)]
    print("CoinBids catalog builder"); print("MODE:","WRITE (isolated builder tables)" if args.write else "TEST / DRY-RUN — database unchanged"); print("Backend:",args.backend)
    ok=0
    for c in coins:
        try:
            spec=backend.mashops_spec_fallback(c,query_for(c)); print_result(c,spec)
            if spec: ok+=1
            if args.write and spec: print("DB observations inserted/attempted:",write_result(backend,c,spec))
        except KeyboardInterrupt: raise
        except Exception as e: print(f"ERROR for {query_for(c)}: {type(e).__name__}: {e}")
    print(f"\nDone. Consensus records: {ok}/{len(coins)}")
    if not args.write: print("No database writes were performed.")

if __name__=="__main__": main()
