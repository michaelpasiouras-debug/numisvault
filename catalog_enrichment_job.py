#!/usr/bin/env python3
"""
CoinBids catalogue enrichment worker.

Purpose
-------
Process ONE queued canonical coin per invocation so the job is:
- resumable
- safe on Render Free
- bounded in runtime
- independent of the live Price Research request path

It reuses the existing MA-Shops identity/spec parser in numisvault_backend.py,
stores source evidence in coin_observations, and recomputes conservative
canonical fields in coin_catalog.

No automatic loop is started on import.
"""

import os
import json
import time
import re
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

import numisvault_backend as nv

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

PRECIOUS_METALS = {"Silver", "Gold", "Platinum", "Palladium"}


def _conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL)


def _identity_key(country, year, denomination, currency, variant=""):
    def n(s):
        return " ".join(str(s or "").strip().lower().split())
    return "|".join([
        n(country),
        str(int(year)),
        f"{float(denomination):.6f}".rstrip("0").rstrip("."),
        n(currency),
        n(variant),
    ])


def _parse_denom(denom_text):
    raw = " ".join(str(denom_text or "").strip().split())
    if not raw:
        raise ValueError("Empty denomination")
    for symbol, fraction in {"½":"1/2","¼":"1/4","¾":"3/4","⅒":"1/10","⅕":"1/5","⅖":"2/5","⅛":"1/8"}.items():
        raw = raw.replace(symbol, fraction)
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s+(.+?)\s*$", raw)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b == 0:
            raise ValueError(f"Invalid denomination fraction: {denom_text!r}")
        return a / b, m.group(3).strip()
    parsed = nv.parse_denomination(raw)
    if not parsed:
        raise ValueError(f"Could not parse denomination: {denom_text!r}")
    return float(parsed[0]), str(parsed[1])


def ensure_catalog_coin(cur, coin):
    country = str(coin.get("countryEN") or coin.get("country") or "").strip()
    year = int(str(coin.get("year")).strip())
    denomination, currency = _parse_denom(coin.get("denom") or coin.get("denomination") or "")
    variant = str(coin.get("variant") or "").strip()
    series = str(coin.get("series") or variant or "").strip() or None
    key = _identity_key(country, year, denomination, currency, variant)

    cur.execute(
        """
        insert into public.coin_catalog
            (country, year, denomination, currency, series, coin_type, variant,
             identity_key, specs_confidence, metadata)
        values
            (%s,%s,%s,%s,%s,%s,%s,%s,'UNVERIFIED',%s::jsonb)
        on conflict (identity_key) do update
        set updated_at=now()
        returning id
        """,
        (
            country, year, denomination, currency, series, variant or None,
            variant or None, key,
            json.dumps({"enrichment_status": "QUEUED"})
        ),
    )
    return cur.fetchone()[0], key


def enqueue_coin(coin, priority=100):
    with _conn() as conn:
        with conn.cursor() as cur:
            coin_id, key = ensure_catalog_coin(cur, coin)
            cur.execute(
                """
                insert into public.catalog_enrichment_queue
                    (coin_id, identity_key, country, year, denomination_text,
                     variant, priority, status, attempts, next_run_at)
                values
                    (%s,%s,%s,%s,%s,%s,%s,'PENDING',0,now())
                on conflict (identity_key) do update
                set priority=least(public.catalog_enrichment_queue.priority, excluded.priority),
                    status=case
                        when public.catalog_enrichment_queue.status='DONE'
                        then public.catalog_enrichment_queue.status
                        else 'PENDING'
                    end,
                    updated_at=now()
                returning id
                """,
                (
                    coin_id, key,
                    str(coin.get("countryEN") or coin.get("country") or "").strip(),
                    int(str(coin.get("year")).strip()),
                    str(coin.get("denom") or coin.get("denomination") or "").strip(),
                    str(coin.get("variant") or "").strip() or None,
                    int(priority),
                ),
            )
            return str(cur.fetchone()[0])


def enqueue_test_batch():
    tests = [
        {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1922","variant":"Peace Dollar"},
        {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1923","variant":"Peace Dollar"},
        {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1924","variant":"Peace Dollar"},
        {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1925","variant":"Peace Dollar"},
        {"country":"United States","countryEN":"United States","denom":"1/2 Dollar","year":"1964","variant":"Kennedy Half Dollar"},
    ]
    ids = []
    for i, coin in enumerate(tests):
        ids.append(enqueue_coin(coin, priority=10+i))
    return ids



def enqueue_real_batch(limit=20, priority_start=1000):
    """Enqueue up to `limit` real coins already present in coin_catalog.

    Safety properties:
    - excludes the five historical test identities
    - excludes identities already present in catalog_enrichment_queue
    - prefers UNVERIFIED / incomplete catalogue rows
    - does not alter existing DONE jobs
    - creates only PENDING queue rows
    """
    limit = max(1, min(int(limit), 100))

    test_keys = {
        _identity_key("United States", 1922, 1, "dollar", "Peace Dollar"),
        _identity_key("United States", 1923, 1, "dollar", "Peace Dollar"),
        _identity_key("United States", 1924, 1, "dollar", "Peace Dollar"),
        _identity_key("United States", 1925, 1, "dollar", "Peace Dollar"),
        _identity_key("United States", 1964, 0.5, "dollar", "Kennedy Half Dollar"),
    }

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select c.id, c.identity_key, c.country, c.year,
                       c.denomination, c.currency, c.variant,
                       c.specs_confidence, c.primary_metal,
                       c.fineness_per_mille, c.weight_g, c.diameter_mm
                from public.coin_catalog c
                where not exists (
                    select 1
                    from public.catalog_enrichment_queue q
                    where q.identity_key=c.identity_key
                )
                order by
                    case when coalesce(c.specs_confidence,'UNVERIFIED')='UNVERIFIED'
                         then 0 else 1 end,
                    case when c.primary_metal is null then 0 else 1 end,
                    case when c.weight_g is null then 0 else 1 end,
                    c.country asc, c.year asc, c.denomination asc
                limit %s
                """,
                (limit + len(test_keys) + 20,),
            )
            candidates = [dict(r) for r in cur.fetchall()]

            enqueued = []
            skipped = []
            priority = int(priority_start)

            for row in candidates:
                if len(enqueued) >= limit:
                    break

                key = str(row.get("identity_key") or "")
                if key in test_keys:
                    skipped.append({"identity_key": key, "reason": "TEST_IDENTITY"})
                    continue

                denom = float(row["denomination"])
                denom_text = f"{denom:g} {row['currency']}".strip()

                cur.execute(
                    """
                    insert into public.catalog_enrichment_queue
                        (coin_id, identity_key, country, year, denomination_text,
                         variant, priority, status, attempts, next_run_at)
                    values
                        (%s,%s,%s,%s,%s,%s,%s,'PENDING',0,now())
                    on conflict (identity_key) do nothing
                    returning id
                    """,
                    (
                        row["id"], key, row["country"], int(row["year"]),
                        denom_text, row.get("variant") or None, priority,
                    ),
                )
                inserted = cur.fetchone()
                if not inserted:
                    skipped.append({"identity_key": key, "reason": "ALREADY_QUEUED"})
                    continue

                enqueued.append({
                    "job_id": str(inserted["id"]),
                    "coin_id": str(row["id"]),
                    "identity_key": key,
                    "country": row["country"],
                    "year": int(row["year"]),
                    "denomination_text": denom_text,
                    "variant": row.get("variant"),
                    "priority": priority,
                })
                priority += 1

            return {
                "ok": True,
                "requested": limit,
                "enqueued": len(enqueued),
                "jobs": enqueued,
                "skipped": skipped,
            }


def _coin_specs_candidates(limit=20):
    """Return issue-specific production records that genuinely need enrichment.

    Eligibility is metal-aware:
    - UNKNOWN metal: enrich (we need composition / primary metal).
    - PRECIOUS metal: enrich when fineness, weight, or diameter is missing.
    - BASE metal/alloy: fineness is NOT required; enrich only when weight or
      diameter is missing.
    - Never expand year ranges into invented issue years.
    """
    limit=max(1,min(int(limit),100))

    precious_tokens=(
        "silver","gold","platinum","palladium","electrum",
        "argent","silber","oro","or ","gold-","platin"
    )

    def metal_class(primary_metal):
        m=str(primary_metal or "").strip().lower()
        if not m:
            return "UNKNOWN"
        if any(tok in m for tok in precious_tokens):
            return "PRECIOUS"
        return "BASE"

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Pull a wider issue-specific pool, then apply the metal-aware rule
            # in Python so NULL fineness on base metals is not treated as missing.
            cur.execute(
                """
                select id,record_id,country,currency_code,denomination_value,
                       denomination_label,year_from,year_to,issue_year,
                       coin_type,variant,title,primary_metal,fineness_per_mille,
                       weight_g,diameter_mm,confidence,verified
                from public.coin_specs
                where country is not null
                  and denomination_value is not null
                  and (
                        issue_year is not null
                        or (year_from is not null and year_to is not null and year_from=year_to)
                      )
                order by
                    case when verified then 1 else 0 end,
                    case when primary_metal is null then 0 else 1 end,
                    case when weight_g is null then 0 else 1 end,
                    case when diameter_mm is null then 0 else 1 end,
                    country asc,
                    coalesce(issue_year,year_from) asc,
                    denomination_value asc
                limit %s
                """,
                (max(limit*25,250),),
            )

            rows=[]
            seen=set()
            for r in cur.fetchall():
                d=dict(r)
                year=d.get("issue_year")
                if year is None and d.get("year_from")==d.get("year_to"):
                    year=d.get("year_from")
                if year is None:
                    continue

                variant=(d.get("variant") or "").strip()
                bad_variant=variant.lower()
                if "framework" in bad_variant or "not issue-specific" in bad_variant:
                    continue

                cls=metal_class(d.get("primary_metal"))
                missing=[]
                if cls=="UNKNOWN":
                    missing.append("primary_metal")
                    # Physical dimensions are useful too, but unknown metal alone
                    # is enough to justify enrichment.
                    if d.get("weight_g") is None:
                        missing.append("weight_g")
                    if d.get("diameter_mm") is None:
                        missing.append("diameter_mm")
                elif cls=="PRECIOUS":
                    if d.get("fineness_per_mille") is None:
                        missing.append("fineness_per_mille")
                    if d.get("weight_g") is None:
                        missing.append("weight_g")
                    if d.get("diameter_mm") is None:
                        missing.append("diameter_mm")
                else:  # BASE
                    if d.get("weight_g") is None:
                        missing.append("weight_g")
                    if d.get("diameter_mm") is None:
                        missing.append("diameter_mm")

                if not missing:
                    continue

                denom_label=(d.get("denomination_label") or "").strip()
                if not denom_label:
                    denom_label=f"{float(d['denomination_value']):g} {d.get('currency_code') or ''}".strip()

                key=(str(d.get("country") or "").strip().lower(),int(year),
                     round(float(d["denomination_value"]),8),
                     str(d.get("currency_code") or "").lower(),variant.lower())
                if key in seen:
                    continue
                seen.add(key)

                rows.append({
                    "source_spec_id":str(d["id"]),
                    "record_id":d.get("record_id"),
                    "country":d.get("country"),
                    "countryEN":d.get("country"),
                    "denom":denom_label,
                    "year":str(int(year)),
                    "variant":variant,
                    "title":d.get("title"),
                    "metal_class":cls,
                    "missing_fields":missing,
                    "existing":{
                        "primary_metal":d.get("primary_metal"),
                        "fineness_per_mille":float(d["fineness_per_mille"]) if d.get("fineness_per_mille") is not None else None,
                        "weight_g":float(d["weight_g"]) if d.get("weight_g") is not None else None,
                        "diameter_mm":float(d["diameter_mm"]) if d.get("diameter_mm") is not None else None,
                        "verified":bool(d.get("verified")),
                        "confidence":float(d["confidence"]) if d.get("confidence") is not None else None,
                    }
                })
                if len(rows)>=limit:
                    break
            return rows

def preview_production_batch(limit=20):
    """READ ONLY preview of real issue-specific rows from coin_specs."""
    rows=_coin_specs_candidates(limit)
    return {
        "ok":True,
        "read_only":True,
        "source":"public.coin_specs",
        "selection_rule":"issue-specific only; metal-aware completeness; base metals do not require fineness; no year-range expansion",
        "requested":max(1,min(int(limit),100)),
        "count":len(rows),
        "coins":rows,
    }


def enqueue_production_batch(limit=20, priority_start=1000):
    """Bridge real coin_specs identities into coin_catalog + enrichment queue."""
    candidates=_coin_specs_candidates(limit)
    jobs=[]
    skipped=[]
    priority=int(priority_start)
    for c in candidates:
        coin={
            "country":c["country"],
            "countryEN":c["countryEN"],
            "denom":c["denom"],
            "year":c["year"],
            "variant":c.get("variant") or "",
        }
        try:
            job_id=enqueue_coin(coin,priority=priority)
            jobs.append({
                "job_id":job_id,
                "source_spec_id":c["source_spec_id"],
                "record_id":c.get("record_id"),
                "country":c["country"],
                "denom":c["denom"],
                "year":c["year"],
                "variant":c.get("variant") or "",
            })
            priority+=1
        except Exception as e:
            skipped.append({
                "source_spec_id":c.get("source_spec_id"),
                "record_id":c.get("record_id"),
                "reason":type(e).__name__,
                "message":str(e)[:250],
            })
    return {
        "ok":True,
        "source":"public.coin_specs",
        "requested":max(1,min(int(limit),100)),
        "eligible":len(candidates),
        "enqueued":len(jobs),
        "jobs":jobs,
        "skipped":skipped,
    }


def _claim_one():
    """Atomically claim one pending/retry job."""
    conn = _conn()
    conn.autocommit = False
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            select *
            from public.catalog_enrichment_queue
            where status in ('PENDING','RETRY')
              and (next_run_at is null or next_run_at <= now())
            order by priority asc, created_at asc
            for update skip locked
            limit 1
            """
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return None

        cur.execute(
            """
            update public.catalog_enrichment_queue
            set status='RUNNING',
                attempts=attempts+1,
                started_at=now(),
                updated_at=now(),
                last_error=null
            where id=%s
            """,
            (row["id"],),
        )
        conn.commit()
        conn.close()
        return dict(row)
    except Exception:
        conn.rollback()
        conn.close()
        raise


def _merge_field_evidence(spec):
    """Merge per-field MA-Shops evidence into one observation per dealer/url."""
    by = {}
    evidence = (spec or {}).get("spec_evidence_by_field") or {}
    mapping = {
        "primary_metal": "primary_metal",
        "fineness_per_mille": "fineness_per_mille",
        "weight_g": "weight_g",
        "diameter_mm": "diameter_mm",
    }
    for src_field, dst_field in mapping.items():
        for rec in evidence.get(src_field) or []:
            url = str(rec.get("url") or "").strip()
            dealer = str(rec.get("dealer") or "").strip()
            if not url and not dealer:
                continue
            key = (dealer.lower(), url.lower())
            row = by.setdefault(key, {
                "dealer": dealer or None,
                "source_url": url or None,
                "title": rec.get("title"),
                "raw_description": rec.get("evidence"),
            })
            row[dst_field] = rec.get("value")
            if rec.get("evidence") and not row.get("raw_description"):
                row["raw_description"] = rec.get("evidence")
    for row in by.values():
        if row.get("weight_g") is not None and row.get("fineness_per_mille") is not None:
            try:
                row["fine_metal_g"] = float(row["weight_g"]) * float(row["fineness_per_mille"]) / 1000.0
            except Exception:
                pass
    return list(by.values())


def _ma_item_id(url):
    if not url:
        return None
    import re
    m = re.search(r"[?&]id=([^&#]+)", url)
    return m.group(1) if m else url


def store_observations(coin_id, coin, spec):
    observations = _merge_field_evidence(spec)
    if not observations:
        return 0

    country = str(coin.get("countryEN") or coin.get("country") or "").strip()
    year = int(str(coin.get("year")).strip())
    denomination, currency = _parse_denom(coin.get("denom") or "")
    variant = str(coin.get("variant") or "").strip() or None

    inserted = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for o in observations:
                source_item_id = _ma_item_id(o.get("source_url"))
                cur.execute(
                    """
                    insert into public.coin_observations
                        (coin_id, source, source_item_id, source_url, dealer, title,
                         raw_description, observed_country, observed_year,
                         observed_denomination, observed_currency, observed_variant,
                         primary_metal, composition, fineness_per_mille, weight_g,
                         diameter_mm, fine_metal_g, identity_confidence,
                         identity_verified, specs_usable, raw_data)
                    values
                        (%s,'MA-Shops',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                         %s,%s,%s,%s,%s,%s,%s,true,true,%s::jsonb)
                    on conflict (source, source_item_id) where source_item_id is not null
                    do update set
                        coin_id=excluded.coin_id,
                        dealer=excluded.dealer,
                        title=excluded.title,
                        raw_description=excluded.raw_description,
                        primary_metal=excluded.primary_metal,
                        composition=excluded.composition,
                        fineness_per_mille=excluded.fineness_per_mille,
                        weight_g=excluded.weight_g,
                        diameter_mm=excluded.diameter_mm,
                        fine_metal_g=excluded.fine_metal_g,
                        identity_verified=true,
                        specs_usable=true,
                        raw_data=excluded.raw_data,
                        observed_at=now()
                    """,
                    (
                        coin_id, source_item_id, o.get("source_url"), o.get("dealer"),
                        o.get("title"), o.get("raw_description"), country, year,
                        denomination, currency, variant,
                        o.get("primary_metal"),
                        (f"{o.get('primary_metal')} (.{int(o.get('fineness_per_mille')):03d})"
                         if o.get("primary_metal") and o.get("fineness_per_mille") is not None
                         else o.get("primary_metal")),
                        o.get("fineness_per_mille"), o.get("weight_g"),
                        o.get("diameter_mm"), o.get("fine_metal_g"),
                        float(spec.get("confidence") or 0.92),
                        json.dumps({"spec_source": spec.get("spec_source")}),
                    ),
                )
                inserted += 1
    return inserted


def _consensus_exact(rows, field, decimals=None, min_dealers=2):
    groups = {}
    for r in rows:
        val = r.get(field)
        if val is None:
            continue
        dealer = (r.get("dealer") or r.get("source_url") or "").lower()
        if not dealer:
            continue
        key = str(val).lower()
        if decimals is not None:
            try:
                key = round(float(val), decimals)
            except Exception:
                continue
        groups.setdefault(key, {})[dealer] = r
    if not groups:
        return None, 0
    key, dealers = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(dealers) < min_dealers:
        return None, len(dealers)
    return key, len(dealers)


def recompute_canonical(coin_id):
    """Recompute current-year canonical fields from STORED observations only."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select dealer, source_url, primary_metal, fineness_per_mille,
                       weight_g, diameter_mm
                from public.coin_observations
                where coin_id=%s
                  and identity_verified=true
                  and specs_usable=true
                """,
                (coin_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]

            metal, metal_n = _consensus_exact(rows, "primary_metal", None, 2)
            fine, fine_n = _consensus_exact(rows, "fineness_per_mille", 0, 2)
            weight, weight_n = _consensus_exact(rows, "weight_g", 2, 2)
            diam, diam_n = _consensus_exact(rows, "diameter_mm", 1, 2)

            canonical = {}
            if metal is not None:
                # preserve original case from a matching observation
                canonical["primary_metal"] = next(
                    r["primary_metal"] for r in rows
                    if r.get("primary_metal") is not None
                    and str(r["primary_metal"]).lower() == str(metal).lower()
                )
            if fine is not None:
                canonical["fineness_per_mille"] = int(round(float(fine)))
            if weight is not None:
                canonical["weight_g"] = float(weight)
            if diam is not None:
                canonical["diameter_mm"] = float(diam)

            if canonical.get("primary_metal") and canonical.get("fineness_per_mille") is not None:
                canonical["composition"] = f"{canonical['primary_metal']} (.{canonical['fineness_per_mille']:03d})"
            if canonical.get("weight_g") is not None and canonical.get("fineness_per_mille") is not None:
                canonical["fine_metal_g"] = canonical["weight_g"] * canonical["fineness_per_mille"] / 1000.0

            confidence = "HIGH" if all(
                k in canonical for k in ("primary_metal","fineness_per_mille","weight_g")
            ) else ("MEDIUM" if canonical else "UNVERIFIED")

            cur.execute(
                """
                update public.coin_catalog
                set primary_metal=%s,
                    composition=%s,
                    fineness_per_mille=%s,
                    weight_g=%s,
                    diameter_mm=%s,
                    fine_metal_g=%s,
                    specs_confidence=%s,
                    observation_count=%s,
                    verified_observation_count=%s,
                    specs_verified_at=case when %s in ('HIGH','MEDIUM') then now() else specs_verified_at end,
                    metadata=coalesce(metadata,'{}'::jsonb) || %s::jsonb,
                    updated_at=now()
                where id=%s
                """,
                (
                    canonical.get("primary_metal"),
                    canonical.get("composition"),
                    canonical.get("fineness_per_mille"),
                    canonical.get("weight_g"),
                    canonical.get("diameter_mm"),
                    canonical.get("fine_metal_g"),
                    confidence,
                    len(rows), len(rows), confidence,
                    json.dumps({
                        "enrichment_status":"DONE_CURRENT_YEAR",
                        "consensus_counts":{
                            "metal":metal_n, "fineness":fine_n,
                            "weight":weight_n, "diameter":diam_n,
                        }
                    }),
                    coin_id,
                ),
            )
            return {"canonical": canonical, "confidence": confidence, "observation_count": len(rows)}


def _finish_job(job_id, status, result=None, error=None, retry_delay_minutes=60):
    with _conn() as conn:
        with conn.cursor() as cur:
            if status == "RETRY":
                cur.execute(
                    """
                    update public.catalog_enrichment_queue
                    set status='RETRY',
                        last_error=%s,
                        result=%s::jsonb,
                        next_run_at=now()+(%s || ' minutes')::interval,
                        finished_at=now(),
                        updated_at=now()
                    where id=%s
                    """,
                    (error, json.dumps(result or {}), int(retry_delay_minutes), job_id),
                )
            else:
                cur.execute(
                    """
                    update public.catalog_enrichment_queue
                    set status=%s,
                        last_error=%s,
                        result=%s::jsonb,
                        finished_at=now(),
                        updated_at=now()
                    where id=%s
                    """,
                    (status, error, json.dumps(result or {}), job_id),
                )



def _query_variants(coin):
    """Conservative MA-Shops query variants; identity filtering remains unchanged."""
    country = str(coin.get("countryEN") or coin.get("country") or "").strip()
    denom = str(coin.get("denom") or "").strip()
    year = str(coin.get("year") or "").strip()
    variant = str(coin.get("variant") or "").strip()

    countries = [country]
    if country.lower() in ("united states", "united states of america"):
        countries += ["USA", "U.S.A."]

    denoms = [denom]
    dlow = denom.lower()
    if dlow in ("1/2 dollar", "0.5 dollar", "½ dollar"):
        denoms += ["Half Dollar", "50 Cents", "50 Cent"]
    elif dlow == "1 dollar":
        denoms += ["Dollar", "1 $"]

    queries = []
    seen = set()
    for c in countries:
        for d in denoms:
            parts = [c, d, year, variant]
            q = " ".join(p for p in parts if p).strip()
            key = q.lower()
            if q and key not in seen:
                seen.add(key)
                queries.append(q)

    # Keep bounded: primary + at most 5 alternates.
    return queries[:6]


def _best_spec_from_queries(coin, queries, deadline):
    """Try bounded query variants and keep the strongest accepted evidence."""
    attempts = []
    best = None
    best_score = (-1, -1)

    for q in queries:
        if time.time() >= deadline:
            break
        try:
            spec = nv.mashops_spec_fallback(coin, q)
        except Exception as e:
            attempts.append({"query": q, "status": "ERROR", "error": type(e).__name__})
            continue

        if not spec:
            attempts.append({"query": q, "status": "NO_CONSENSUS"})
            continue

        ev = spec.get("spec_evidence_by_field") or {}
        unique_urls = set()
        field_hits = 0
        for field in ("primary_metal","fineness_per_mille","weight_g","diameter_mm"):
            rows = ev.get(field) or []
            if rows:
                field_hits += 1
            for r in rows:
                if r.get("url"):
                    unique_urls.add(r["url"])

        score = (len(unique_urls), field_hits)
        attempts.append({
            "query": q,
            "status": "EVIDENCE",
            "unique_listings": len(unique_urls),
            "fields_with_evidence": field_hits,
        })
        if score > best_score:
            best = spec
            best_score = score

        # Early stop on strong evidence.
        if len(unique_urls) >= 3 and field_hits >= 3:
            break

    return best, attempts


def run_one(max_seconds=22.0):
    """Run exactly one queued coin. Intended for one HTTP/cron invocation."""
    started = time.time()
    job = _claim_one()
    if not job:
        return {"ok": True, "status": "IDLE", "message": "No pending catalogue jobs."}

    coin = {
        "country": job["country"],
        "countryEN": job["country"],
        "denom": job["denomination_text"],
        "year": str(job["year"]),
        "variant": job.get("variant") or "",
    }
    query = " ".join(
        str(coin.get(k) or "").strip()
        for k in ("countryEN","denom","year","variant")
        if coin.get(k)
    )

    try:
        deadline = started + max_seconds
        queries = _query_variants(coin)
        spec, query_attempts = _best_spec_from_queries(coin, queries, deadline)
        elapsed = time.time() - started

        if elapsed > max_seconds:
            result = {
                "coin": coin,
                "elapsed_seconds": round(elapsed,3),
                "reason":"TIME_BUDGET_EXCEEDED",
                "query_attempts": query_attempts,
            }
            _finish_job(job["id"], "RETRY", result=result, error="TIME_BUDGET_EXCEEDED", retry_delay_minutes=120)
            return {"ok": True, "status": "RETRY", **result}

        if not spec:
            result = {
                "coin": coin,
                "elapsed_seconds": round(elapsed,3),
                "reason":"NO_CONSENSUS",
                "query_attempts": query_attempts,
            }
            _finish_job(job["id"], "RETRY", result=result, error="NO_CONSENSUS", retry_delay_minutes=360)
            return {"ok": True, "status": "RETRY", **result}

        stored = store_observations(job["coin_id"], coin, spec)
        canonical = recompute_canonical(job["coin_id"])
        result = {
            "coin": coin,
            "stored_observations": stored,
            "canonical": canonical,
            "query_attempts": query_attempts,
            "elapsed_seconds": round(time.time()-started,3),
        }
        _finish_job(job["id"], "DONE", result=result)
        return {"ok": True, "status": "DONE", **result}

    except Exception as e:
        result = {
            "coin": coin,
            "elapsed_seconds": round(time.time()-started,3),
            "error_type": type(e).__name__,
        }
        _finish_job(job["id"], "RETRY", result=result, error=str(e)[:1000], retry_delay_minutes=120)
        return {"ok": False, "status": "RETRY", **result, "error": str(e)[:500]}


def queue_status():
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select status, count(*) as count
                from public.catalog_enrichment_queue
                group by status
                order by status
                """
            )
            return [dict(r) for r in cur.fetchall()]


if __name__ == "__main__":
    print(json.dumps(run_one(), ensure_ascii=False, indent=2, default=str))
