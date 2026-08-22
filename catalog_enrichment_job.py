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
    parsed = nv.parse_denomination(denom_text)
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
        spec = nv.mashops_spec_fallback(coin, query)
        elapsed = time.time() - started

        if elapsed > max_seconds:
            result = {"coin": coin, "elapsed_seconds": round(elapsed,3), "reason":"TIME_BUDGET_EXCEEDED"}
            _finish_job(job["id"], "RETRY", result=result, error="TIME_BUDGET_EXCEEDED", retry_delay_minutes=120)
            return {"ok": True, "status": "RETRY", **result}

        if not spec:
            result = {"coin": coin, "elapsed_seconds": round(elapsed,3), "reason":"NO_CONSENSUS"}
            _finish_job(job["id"], "RETRY", result=result, error="NO_CONSENSUS", retry_delay_minutes=360)
            return {"ok": True, "status": "RETRY", **result}

        stored = store_observations(job["coin_id"], coin, spec)
        canonical = recompute_canonical(job["coin_id"])
        result = {
            "coin": coin,
            "stored_observations": stored,
            "canonical": canonical,
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
