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
from psycopg2.extras import RealDictCursor
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
    """Robust catalogue denomination parser.

    Keeps the legacy NumisVault parser first, then uses a strict generic
    leading-number fallback so valid labels do not depend on a hard-coded
    currency vocabulary.
    """
    raw = " ".join(str(denom_text or "").strip().split())
    if not raw:
        raise ValueError("Empty denomination")

    for symbol, fraction in {
        "½":"1/2","¼":"1/4","¾":"3/4","⅒":"1/10",
        "⅕":"1/5","⅖":"2/5","⅛":"1/8"
    }.items():
        raw = raw.replace(symbol, fraction)

    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)(?:\s+(.+?))?\s*", raw)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b == 0:
            raise ValueError(f"Invalid denomination fraction: {denom_text!r}")
        return a / b, (m.group(3) or "").strip()

    try:
        parsed = nv.parse_denomination(raw)
    except Exception:
        parsed = None
    if parsed:
        try:
            value = float(parsed[0])
            unit = str(parsed[1] or "").strip()
            if value > 0:
                return value, unit
        except (TypeError, ValueError, IndexError):
            pass

    m = re.fullmatch(
        r"\s*([+]?(?:\d+(?:[\.,]\d+)?|[\.,]\d+))(?:\s+(.+?))?\s*",
        raw,
        flags=re.UNICODE,
    )
    if not m:
        raise ValueError(f"Could not parse denomination: {denom_text!r}")

    value = float(m.group(1).replace(",", "."))
    if value <= 0:
        raise ValueError(f"Invalid denomination value: {denom_text!r}")
    return value, (m.group(2) or "").strip()


def ensure_catalog_coin(cur, coin):
    country = str(coin.get("countryEN") or coin.get("country") or "").strip()
    year = int(str(coin.get("year")).strip())
    denomination, currency = _parse_denom(coin.get("denom") or coin.get("denomination") or "")
    variant = str(coin.get("variant") or "").strip()
    series = str(coin.get("series") or variant or "").strip() or None
    identity_variant = str(coin.get("_identity_variant") or variant).strip()
    key = _identity_key(country, year, denomination, currency, identity_variant)

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



def enqueue_coin_if_new(coin, priority=100):
    """Queue a coin only if no queue row exists for its canonical identity.

    Unlike enqueue_coin(), this never resets RETRY/FAILED/PENDING jobs and
    therefore respects next_run_at backoff during unattended automation.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            coin_id,key=ensure_catalog_coin(cur,coin)
            cur.execute(
                """
                insert into public.catalog_enrichment_queue
                    (coin_id,identity_key,country,year,denomination_text,
                     variant,priority,status,attempts,next_run_at)
                values
                    (%s,%s,%s,%s,%s,%s,%s,'PENDING',0,now())
                on conflict (identity_key) do nothing
                returning id
                """,
                (
                    coin_id,key,
                    str(coin.get("countryEN") or coin.get("country") or "").strip(),
                    int(str(coin.get("year")).strip()),
                    str(coin.get("denom") or coin.get("denomination") or "").strip(),
                    str(coin.get("variant") or "").strip() or None,
                    int(priority),
                ),
            )
            row=cur.fetchone()
            return (str(row[0]) if row else None),str(coin_id),key


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



_EURO_STANDARD_PROFILES = {
    0.01: (2.30, 16.25, "copper-plated steel"),
    0.02: (3.06, 18.75, "copper-plated steel"),
    0.05: (3.92, 21.25, "copper-plated steel"),
    0.10: (4.10, 19.75, "Nordic gold"),
    0.20: (5.74, 22.25, "Nordic gold"),
    0.50: (7.80, 24.25, "Nordic gold"),
    1.00: (7.50, 23.25, "bimetallic"),
    2.00: (8.50, 25.75, "bimetallic"),
}

_EURO_COUNTRIES = {
    "andorra","austria","belgium","croatia","cyprus","estonia","finland","france",
    "germany","greece","ireland","italy","latvia","lithuania","luxembourg","malta",
    "monaco","netherlands","portugal","san marino","slovakia","slovenia","spain",
    "vatican city","vatican"
}

_EURO_COIN_START_YEAR = {
    "andorra":2014, "austria":2002, "belgium":1999, "bulgaria":2026,
    "croatia":2023, "cyprus":2008, "estonia":2011, "finland":1999,
    "france":1999, "germany":2002, "greece":2002, "ireland":2002,
    "italy":2002, "latvia":2014, "lithuania":2015, "luxembourg":2002,
    "malta":2008, "monaco":2002, "netherlands":1999, "portugal":2002,
    "san marino":2002, "slovakia":2009, "slovenia":2007, "spain":1999,
    "vatican city":2002,
}

def _norm_country(v):
    try:
        return nv.canonical_country(str(v or "").strip())
    except Exception:
        return " ".join(str(v or "").strip().lower().split())

def _is_exact_euro_profile_row(row):
    """Conservative detector for legacy standard euro circulation rows.
    Requires country + standard denomination + matching standard weight/diameter.
    This avoids touching unrelated historical numeric denominations."""
    country=_norm_country(row.get("country"))
    denom=_numeric_denomination(row.get("denomination_label") or row.get("denom"))
    if country not in _EURO_COIN_START_YEAR or denom not in _EURO_STANDARD_PROFILES:
        return False
    expected_w,expected_d,_ = _EURO_STANDARD_PROFILES[denom]
    try:
        w=float(row.get("weight_g"))
        d=float(row.get("diameter_mm"))
    except (TypeError,ValueError):
        return False
    return abs(w-expected_w)<=0.02 and abs(d-expected_d)<=0.03

def _invalid_euro_year_reason(row):
    if not _is_exact_euro_profile_row(row):
        return None
    country=_norm_country(row.get("country"))
    start=_EURO_COIN_START_YEAR.get(country)
    try:
        issue=row.get("issue_year")
        yf=row.get("year_from")
        anchor=int(issue if issue is not None else yf)
    except (TypeError,ValueError):
        return None
    if start and anchor < start:
        return {
            "code":"EURO_YEAR_BEFORE_ISSUER_START",
            "country":country,
            "anchor_year":anchor,
            "minimum_year":start,
            "denomination":_numeric_denomination(row.get("denomination_label") or row.get("denom")),
        }
    return None


def _numeric_denomination(label):
    s=str(label or "").strip().lower().replace(",",".")
    # Deliberately accept only a bare numeric denomination. This avoids treating
    # historical "10 francs", "1 schilling", etc. as euro-profile records.
    try:
        if not s or any(ch.isalpha() for ch in s):
            return None
        return round(float(s),2)
    except Exception:
        return None

def _euro_profile_conflict(row):
    """Return a reason when a row exactly matches euro physical specs but its
    stored metal conflicts with the known circulation profile.

    This is a quarantine guard, not an automatic correction.
    """
    country=" ".join(str(row.get("country") or "").strip().lower().split())
    if country not in _EURO_COUNTRIES:
        return None

    denom=_numeric_denomination(row.get("denomination_label") or row.get("denom"))
    if denom not in _EURO_STANDARD_PROFILES:
        return None

    expected_w,expected_d,expected_metal=_EURO_STANDARD_PROFILES[denom]
    try:
        w=float(row.get("weight_g"))
        d=float(row.get("diameter_mm"))
    except (TypeError,ValueError):
        return None

    if abs(w-expected_w)>0.02 or abs(d-expected_d)>0.03:
        return None

    metal=" ".join(str(row.get("primary_metal") or "").strip().lower().split())
    if not metal:
        return None

    # "Nordic gold" is a copper alloy and contains no gold. A bare precious-metal
    # label on an exact euro circulation profile is therefore contradictory.
    precious_tokens=("gold","silver","platinum","palladium","electrum")
    if any(t in metal for t in precious_tokens) and "nordic gold" not in metal:
        return {
            "code":"EURO_PROFILE_METAL_CONFLICT",
            "expected_profile":expected_metal,
            "denomination":denom,
            "weight_g":w,
            "diameter_mm":d,
            "stored_primary_metal":row.get("primary_metal"),
        }
    return None


def _coin_specs_candidates(limit=20):
    """Return genuinely incomplete production spec SERIES, including year ranges.

    Completeness is metal-aware:
      * weight + diameter + primary_metal are always required
      * fineness is required only when the known primary metal is precious
      * ordinary base-metal alloys do not require fineness

    A year range is NOT itself an error. It is a catalogue series. We enqueue it
    only when a real physical-spec field is missing. For safe enrichment, the
    queue uses one representative year, while source_spec_id/record_id remain
    attached so write-back can target the original series row.
    """
    limit=max(1,min(int(limit),500))
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                select
                    id,
                    country,
                    denomination_label,
                    variant,
                    issue_year,
                    year_from,
                    year_to,
                    weight_g,
                    diameter_mm,
                    primary_metal,
                    fineness_per_mille,
                    verified,
                    confidence
                from public.coin_specs
                order by country,
                         coalesce(issue_year,year_from),
                         denomination_label,
                         id
                """
            )
            rows=[dict(r) for r in cur.fetchall()]

    def norm(v):
        return " ".join(str(v or "").strip().lower().split())

    def precious(metal):
        m=norm(metal)
        return any(t in m for t in ("silver","gold","platinum","palladium","electrum"))

    candidates=[]
    for r in rows:
        # Never enrich a row whose existing data is internally contradictory
        # or whose legacy euro year predates that issuer's euro coinage.
        if _euro_profile_conflict(r):
            continue
        if _invalid_euro_year_reason(r):
            continue

        missing=[]
        if r.get("weight_g") is None:
            missing.append("weight_g")
        if r.get("diameter_mm") is None:
            missing.append("diameter_mm")

        metal=r.get("primary_metal")
        if metal is None or not norm(metal):
            missing.append("primary_metal")
        elif precious(metal) and r.get("fineness_per_mille") is None:
            missing.append("fineness_per_mille")

        if not missing:
            continue

        issue_year=r.get("issue_year")
        year_from=r.get("year_from")
        year_to=r.get("year_to")

        # Representative year is only for evidence search. The original series
        # identity is preserved separately for safe write-back.
        representative_year = issue_year if issue_year is not None else year_from
        if representative_year is None:
            # No defensible year anchor: audit it, but do not automate a fuzzy search.
            continue

        country=str(r.get("country") or "").strip()
        denom=str(r.get("denomination_label") or "").strip()
        variant=str(r.get("variant") or "").strip()

        candidates.append({
            "source_spec_id":str(r["id"]),
            "record_id":str(r["id"]),
            "country":country,
            "countryEN":country,
            "denom":denom,
            "year":str(representative_year),
            "year_from":year_from,
            "year_to":year_to,
            "issue_year":issue_year,
            "variant":variant,
            "missing_fields":missing,
            "series_mode": bool(issue_year is None and (
                year_to is None or str(year_from)!=str(year_to)
            )),
            "existing":{
                "weight_g":float(r["weight_g"]) if r.get("weight_g") is not None else None,
                "diameter_mm":float(r["diameter_mm"]) if r.get("diameter_mm") is not None else None,
                "primary_metal":r.get("primary_metal"),
                "fineness_per_mille":float(r["fineness_per_mille"]) if r.get("fineness_per_mille") is not None else None,
                "verified":r.get("verified"),
                "confidence":float(r["confidence"]) if r.get("confidence") is not None else None,
            },
        })
        if len(candidates)>=limit:
            break

    return candidates

def enqueue_production_batch(limit=20, priority_start=1000):
    """Queue NEW real production identities without disturbing existing jobs."""
    limit=max(1,min(int(limit),100))
    # Pull a wider pool so old RETRY/DONE identities do not starve new rows.
    candidates=_coin_specs_candidates(max(limit*5,50))
    jobs=[]
    skipped=[]
    priority=int(priority_start)

    for c in candidates:
        if len(jobs)>=limit:
            break
        coin={
            "country":c["country"],
            "countryEN":c["countryEN"],
            "denom":c["denom"],
            "year":c["year"],
            "variant":c.get("variant") or "",
            "_identity_variant":(
                (c.get("variant") or "") + " [coin_specs:" + str(c["source_spec_id"]) + "]"
            ).strip(),
        }
        try:
            # Persist the exact production series row on the catalogue record so
            # run_one can safely write back to that row after evidence is found.
            with _conn() as conn:
                with conn.cursor() as cur:
                    coin_id0,key0=ensure_catalog_coin(cur,coin)
                    cur.execute(
                        """
                        update public.coin_catalog
                        set metadata=coalesce(metadata,'{}'::jsonb) || %s::jsonb,
                            updated_at=now()
                        where id=%s
                        """,
                        (json.dumps({
                            "source_spec_id":c.get("source_spec_id"),
                            "record_id":c.get("record_id"),
                            "series_mode":c.get("series_mode",False),
                            "year_from":c.get("year_from"),
                            "year_to":c.get("year_to"),
                            "missing_fields":c.get("missing_fields",[]),
                        }),coin_id0),
                    )
            job_id,coin_id,key=enqueue_coin_if_new(coin,priority=priority)
            if not job_id:
                skipped.append({
                    "record_id":c.get("record_id"),
                    "identity_key":key,
                    "reason":"ALREADY_QUEUED",
                })
                continue
            jobs.append({
                "job_id":job_id,
                "coin_id":coin_id,
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
        "requested":limit,
        "eligible_scanned":len(candidates),
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
    """Identity-safe MA-Shops queries: variant-specific first, then variant-free.

    Country + denomination + year are retained in every query. Variant is not
    mandatory because internal catalogue labels may not appear in dealer titles.
    """
    country=str(coin.get("countryEN") or coin.get("country") or "").strip()
    denom=str(coin.get("denom") or "").strip()
    year=str(coin.get("year") or "").strip()
    variant=str(coin.get("variant") or "").strip()

    countries=[country]
    if country.lower() in ("united states","united states of america"):
        countries += ["USA","U.S.A."]

    denoms=[denom]
    dlow=denom.lower()

    # Legacy euro-profile rows store denomination as bare decimals (0.5, 0.2,
    # 0.1...). MA-Shops dealer text normally uses 50 Cent / 20 Cent / 10 Cent.
    # Add identity-equivalent query forms without changing the stored catalogue.
    try:
        euro_countries=getattr(nv,"_EURO_SPEC_COUNTRIES",set())
        if nv.canonical_country(country) in euro_countries and not nv.parse_denomination(denom):
            v=float(denom.replace(",","."))
            if 0 < v < 1 and abs(v*100-round(v*100))<1e-9:
                cents=int(round(v*100))
                denoms += [f"{cents} Cent",f"{cents} Cents",f"{v:g} Euro"]
            elif v>=1 and abs(v-round(v))<1e-9:
                denoms += [f"{int(round(v))} Euro"]
    except Exception:
        pass

    if dlow in ("1/2 dollar","0.5 dollar","½ dollar"):
        denoms += ["Half Dollar","50 Cents","50 Cent"]
    elif dlow=="1 dollar":
        denoms += ["Dollar","1 $"]

    denoms=list(dict.fromkeys(x for x in denoms if str(x).strip()))
    queries=[]
    seen=set()

    def add(parts):
        q=" ".join(str(p).strip() for p in parts if str(p or "").strip()).strip()
        key=q.lower()
        if q and key not in seen:
            seen.add(key)
            queries.append(q)

    # First pass: exact catalogue variant.
    if variant:
        for c in countries:
            for d in denoms:
                add([c,d,year,variant])

    # Second pass: variant-free fallback. Identity remains country+denom+year.
    for c in countries:
        for d in denoms:
            add([c,d,year])

    return queries[:8]

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

        # Defence in depth: backend extractor already guards this, but the cron
        # refuses contradictory precious-metal labels for standard 10/20/50 euro cents.
        guarded,guard_reason=nv._guard_euro_cent_spec(coin,dict(spec))
        if guard_reason:
            spec=guarded
            attempts.append({"query":q,"status":"SAFETY_GUARD","reason":guard_reason})
            if not any(spec.get(k) is not None for k in ("primary_metal","fineness_per_mille","weight_g","diameter_mm")):
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



# ---------------------------------------------------------------------------
# Tier-1 official issuer fallback
# ---------------------------------------------------------------------------
# This registry contains only specs verified against official central-bank /
# issuer publications. It is deliberately explicit: no fuzzy web scraping and
# no inference from a similar coin. Add new entries only after source review.
OFFICIAL_ISSUER_SPECS = {
    ("estonia", 1993, "5 krooni"): {
        "variant_tokens": ("republic of estonia 75",),
        "primary_metal": "Copper alloy",
        "composition": "Cu89Al5Zn5Sn1",
        "weight_g": 7.1,
        "diameter_mm": 26.2,
        "source": "Eesti Pank",
        "source_url": "https://www.eestipank.ee/sularaha/kroonimyndid",
        "source_item_id": "eestipank-5-krooni-1993",
        "source_note": "Official Eesti Pank kroon coin specifications.",
    },
    ("estonia", 1994, "5 krooni"): {
        "variant_tokens": ("bank of estonia 75", "eesti pank 75"),
        "primary_metal": "Copper alloy",
        "composition": "Cu89Al5Zn5Sn1",
        "weight_g": 7.1,
        "diameter_mm": 26.1,
        "source": "Eesti Pank",
        "source_url": "https://www.eestipank.ee/sularaha/kroonimyndid",
        "source_item_id": "eestipank-5-krooni-1994",
        "source_note": "Official Eesti Pank kroon coin specifications.",
    },
    ("serbia", 2003, "2 dinara"): {
        "variant_tokens": ("2003 alloy",),
        "primary_metal": "Copper-Nickel-Zinc alloy",
        "composition": "Cu70Ni12Zn18",
        "weight_g": 5.24,
        "diameter_mm": 22.0,
        "source": "National Bank of Serbia",
        "source_url": "https://www.nbs.rs/export/sites/NBS_site/documents-eng/propisi/propisi-trz/issue_1_2_5_10_20_200365.pdf",
        "source_item_id": "nbs-rs-65-2003-2-dinara",
        "source_note": "Official Gazette of RS No. 65/2003, NBS decision on 1/2/5/10/20 dinar coins.",
    },
    ("serbia", 2003, "5 dinara"): {
        "variant_tokens": ("2003 alloy",),
        "primary_metal": "Copper-Nickel-Zinc alloy",
        "composition": "Cu70Ni12Zn18",
        "weight_g": 6.23,
        "diameter_mm": 24.0,
        "source": "National Bank of Serbia",
        "source_url": "https://www.nbs.rs/export/sites/NBS_site/documents-eng/propisi/propisi-trz/issue_1_2_5_10_20_200365.pdf",
        "source_item_id": "nbs-rs-65-2003-5-dinara",
        "source_note": "Official Gazette of RS No. 65/2003, NBS decision on 1/2/5/10/20 dinar coins.",
    },
}


def _official_spec_for_coin(coin):
    country=" ".join(str(coin.get("countryEN") or coin.get("country") or "").lower().split())
    year=int(str(coin.get("year")).strip())
    denom=" ".join(str(coin.get("denom") or "").lower().split())
    variant=" ".join(str(coin.get("variant") or "").lower().split())
    spec=OFFICIAL_ISSUER_SPECS.get((country,year,denom))
    if not spec:
        return None
    tokens=tuple(spec.get("variant_tokens") or ())
    if tokens and variant and not any(t in variant for t in tokens):
        return None
    return dict(spec)


def store_official_observation(coin_id, coin, official):
    """Store one Tier-1 issuer observation without pretending it is dealer consensus."""
    country=str(coin.get("countryEN") or coin.get("country") or "").strip()
    year=int(str(coin.get("year")).strip())
    denomination,currency=_parse_denom(coin.get("denom") or "")
    variant=str(coin.get("variant") or "").strip() or None
    raw={
        "authority":"OFFICIAL_ISSUER",
        "source_note":official.get("source_note"),
        "composition":official.get("composition"),
    }
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into public.coin_observations
                    (coin_id,source,source_item_id,source_url,dealer,title,
                     raw_description,observed_country,observed_year,
                     observed_denomination,observed_currency,observed_variant,
                     primary_metal,composition,fineness_per_mille,weight_g,
                     diameter_mm,fine_metal_g,identity_confidence,
                     identity_verified,specs_usable,raw_data)
                values
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                     %s,%s,%s,%s,%s,%s,%s,true,true,%s::jsonb)
                on conflict (source,source_item_id) where source_item_id is not null
                do update set
                    coin_id=excluded.coin_id,
                    source_url=excluded.source_url,
                    title=excluded.title,
                    raw_description=excluded.raw_description,
                    observed_country=excluded.observed_country,
                    observed_year=excluded.observed_year,
                    observed_denomination=excluded.observed_denomination,
                    observed_currency=excluded.observed_currency,
                    observed_variant=excluded.observed_variant,
                    primary_metal=excluded.primary_metal,
                    composition=excluded.composition,
                    fineness_per_mille=excluded.fineness_per_mille,
                    weight_g=excluded.weight_g,
                    diameter_mm=excluded.diameter_mm,
                    identity_confidence=excluded.identity_confidence,
                    identity_verified=true,
                    specs_usable=true,
                    raw_data=excluded.raw_data,
                    observed_at=now()
                """,
                (
                    coin_id,official["source"],official["source_item_id"],
                    official["source_url"],official["source"],official.get("source_note"),
                    official.get("source_note"),country,year,denomination,currency,variant,
                    official.get("primary_metal"),official.get("composition"),
                    official.get("fineness_per_mille"),official.get("weight_g"),
                    official.get("diameter_mm"),None,1.0,json.dumps(raw),
                ),
            )
    return 1


def apply_official_to_catalog(coin_id, official):
    """Apply Tier-1 issuer fields to the enrichment catalogue.

    Official issuer data is authoritative enough to fill canonical fields by
    itself, but provenance is retained and confidence is explicitly OFFICIAL.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update public.coin_catalog
                set primary_metal=coalesce(%s,primary_metal),
                    composition=coalesce(%s,composition),
                    fineness_per_mille=coalesce(%s,fineness_per_mille),
                    weight_g=coalesce(%s,weight_g),
                    diameter_mm=coalesce(%s,diameter_mm),
                    fine_metal_g=case
                        when coalesce(%s,fineness_per_mille) is not null
                         and coalesce(%s,weight_g) is not null
                        then coalesce(%s,weight_g)*coalesce(%s,fineness_per_mille)/1000.0
                        else fine_metal_g
                    end,
                    specs_confidence='HIGH',
                    specs_verified_at=now(),
                    metadata=coalesce(metadata,'{}'::jsonb) || %s::jsonb,
                    updated_at=now()
                where id=%s
                """,
                (
                    official.get("primary_metal"),official.get("composition"),
                    official.get("fineness_per_mille"),official.get("weight_g"),
                    official.get("diameter_mm"),
                    official.get("fineness_per_mille"),official.get("weight_g"),
                    official.get("weight_g"),official.get("fineness_per_mille"),
                    json.dumps({
                        "enrichment_status":"DONE_OFFICIAL_ISSUER",
                        "spec_authority":"OFFICIAL_ISSUER",
                        "official_source":official.get("source"),
                        "official_source_url":official.get("source_url"),
                    }),
                    coin_id,
                ),
            )


def update_production_coin_specs(coin, official=None, source_spec_id=None, canonical=None):
    """Fill missing fields in the exact production row whenever its ID is known.

    Never overwrites an existing non-null physical spec. For legacy/pilot calls
    without source_spec_id, falls back to the previous exact issue matcher.
    """
    values=official or canonical or {}
    metal=values.get("primary_metal")
    fineness=values.get("fineness_per_mille")
    weight=values.get("weight_g")
    diameter=values.get("diameter_mm")

    with _conn() as conn:
        with conn.cursor() as cur:
            if source_spec_id is not None:
                cur.execute(
                    """
                    update public.coin_specs
                    set primary_metal=coalesce(primary_metal,%s),
                        fineness_per_mille=coalesce(fineness_per_mille,%s),
                        weight_g=coalesce(weight_g,%s),
                        diameter_mm=coalesce(diameter_mm,%s),
                        updated_at=now()
                    where id=%s
                    returning id
                    """,
                    (metal,fineness,weight,diameter,source_spec_id),
                )
                return [str(r[0]) for r in cur.fetchall()]

            country=str(coin.get("countryEN") or coin.get("country") or "").strip()
            year=int(str(coin.get("year")).strip())
            denom=" ".join(str(coin.get("denom") or "").strip().split())
            variant=str(coin.get("variant") or "").strip()
            cur.execute(
                """
                update public.coin_specs
                set primary_metal=coalesce(primary_metal,%s),
                    fineness_per_mille=coalesce(fineness_per_mille,%s),
                    weight_g=coalesce(weight_g,%s),
                    diameter_mm=coalesce(diameter_mm,%s),
                    updated_at=now()
                where lower(country)=lower(%s)
                  and coalesce(issue_year,
                      case when year_from=year_to then year_from else null end)=%s
                  and lower(trim(denomination_label))=lower(trim(%s))
                  and (%s='' or lower(trim(coalesce(variant,'')))=lower(trim(%s)))
                returning id
                """,
                (metal,fineness,weight,diameter,country,year,denom,variant,variant),
            )
            return [str(r[0]) for r in cur.fetchall()]


MAX_AUTOMATIC_ATTEMPTS = 3

def _retry_or_fail(job, result, error, retry_delay_minutes):
    """Bound unattended retries. Repeated no-consensus/network failures are
    preserved for manual review instead of cycling forever."""
    current_attempt=int(job.get("attempts") or 0) + 1  # _claim_one increments DB after returning row
    result=dict(result or {})
    result["attempt"]=current_attempt
    result["max_automatic_attempts"]=MAX_AUTOMATIC_ATTEMPTS
    if current_attempt >= MAX_AUTOMATIC_ATTEMPTS:
        result["terminal_reason"]="AUTOMATIC_RETRY_LIMIT_REACHED"
        _finish_job(job["id"],"FAILED",result=result,error=error)
        return "FAILED",result
    _finish_job(job["id"],"RETRY",result=result,error=error,retry_delay_minutes=retry_delay_minutes)
    return "RETRY",result

def run_one(max_seconds=22.0):
    """Run exactly one queued coin: official issuer first, MA-Shops second."""
    started=time.time()
    job=_claim_one()
    if not job:
        return {"ok":True,"status":"IDLE","message":"No pending catalogue jobs."}

    coin={
        "country":job["country"],
        "countryEN":job["country"],
        "denom":job["denomination_text"],
        "year":str(job["year"]),
        "variant":job.get("variant") or "",
    }

    source_spec_id=None
    metadata_error=None
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("select metadata from public.coin_catalog where id=%s",(job["coin_id"],))
                rr=cur.fetchone()
                md=dict((rr or {}).get("metadata") or {})
                source_spec_id=md.get("source_spec_id")
    except Exception as e:
        metadata_error=f"{type(e).__name__}: {e}"

    if "[coin_specs:" in str(job.get("identity_key") or "") and not source_spec_id:
        result={
            "coin":coin,
            "elapsed_seconds":round(time.time()-started,3),
            "reason":"SOURCE_SPEC_ID_MISSING",
            "metadata_error":metadata_error,
        }
        status,result=_retry_or_fail(
            job,result,"SOURCE_SPEC_ID_MISSING",retry_delay_minutes=360
        )
        return {"ok":False,"status":status,**result}

    try:
        # Tier 1: curated official issuer evidence.
        official=_official_spec_for_coin(coin)
        if official:
            stored=store_official_observation(job["coin_id"],coin,official)
            apply_official_to_catalog(job["coin_id"],official)
            production_rows=update_production_coin_specs(coin,official=official,source_spec_id=source_spec_id)
            result={
                "coin":coin,
                "source_tier":"OFFICIAL_ISSUER",
                "official_source":official.get("source"),
                "official_source_url":official.get("source_url"),
                "canonical":{
                    "primary_metal":official.get("primary_metal"),
                    "composition":official.get("composition"),
                    "fineness_per_mille":official.get("fineness_per_mille"),
                    "weight_g":official.get("weight_g"),
                    "diameter_mm":official.get("diameter_mm"),
                },
                "stored_observations":stored,
                "production_coin_specs_updated":production_rows,
                "elapsed_seconds":round(time.time()-started,3),
            }
            _finish_job(job["id"],"DONE",result=result)
            return {"ok":True,"status":"DONE",**result}

        # Tier 2: existing MA-Shops multi-listing consensus.
        deadline=started+max_seconds
        queries=_query_variants(coin)
        spec,query_attempts=_best_spec_from_queries(coin,queries,deadline)
        elapsed=time.time()-started

        if elapsed>max_seconds:
            result={
                "coin":coin,"elapsed_seconds":round(elapsed,3),
                "reason":"TIME_BUDGET_EXCEEDED","query_attempts":query_attempts,
            }
            status,result=_retry_or_fail(job,result,"TIME_BUDGET_EXCEEDED",retry_delay_minutes=120)
            return {"ok":True,"status":status,**result}

        if not spec:
            result={
                "coin":coin,"elapsed_seconds":round(elapsed,3),
                "reason":"NO_CONSENSUS","query_attempts":query_attempts,
                "source_tier":"MA_SHOPS",
            }
            status,result=_retry_or_fail(job,result,"NO_CONSENSUS",retry_delay_minutes=360)
            return {"ok":True,"status":status,**result}

        stored=store_observations(job["coin_id"],coin,spec)
        canonical=recompute_canonical(job["coin_id"])
        production_rows=[]
        canonical_values=(canonical or {}).get("canonical") or {}
        if source_spec_id and canonical_values:
            production_rows=update_production_coin_specs(
                coin,source_spec_id=source_spec_id,canonical=canonical_values
            )
        result={
            "coin":coin,"stored_observations":stored,"canonical":canonical,
            "query_attempts":query_attempts,
            "source_tier":"MA_SHOPS",
            "production_coin_specs_updated":production_rows,
            "source_spec_id":source_spec_id,
            "elapsed_seconds":round(time.time()-started,3),
        }
        _finish_job(job["id"],"DONE",result=result)
        return {"ok":True,"status":"DONE",**result}

    except Exception as e:
        result={
            "coin":coin,
            "elapsed_seconds":round(time.time()-started,3),
            "error_type":type(e).__name__,
        }
        status,result=_retry_or_fail(job,result,str(e)[:1000],retry_delay_minutes=120)
        return {"ok":False,"status":status,**result,"error":str(e)[:500]}




def repair_official_registry():
    """Idempotently restore every curated official observation.

    Needed after removing the incorrect unique(source,source_url) constraint:
    one official page may validly document multiple different coin issues.
    """
    repaired=[]
    errors=[]

    for (country_key,year,denom_key),official in OFFICIAL_ISSUER_SPECS.items():
        variant_tokens=tuple(official.get("variant_tokens") or ())
        coin={
            "country":country_key.title(),
            "countryEN":country_key.title(),
            "denom":denom_key,
            "year":str(year),
            "variant":variant_tokens[0] if variant_tokens else "",
        }
        try:
            with _conn() as conn:
                with conn.cursor() as cur:
                    coin_id,key=ensure_catalog_coin(cur,coin)

            stored=store_official_observation(coin_id,coin,official)
            apply_official_to_catalog(coin_id,official)
            production_rows=update_production_coin_specs(coin,official)

            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        update public.catalog_enrichment_queue
                        set status='DONE',
                            last_error=null,
                            next_run_at=null,
                            result=coalesce(result,'{}'::jsonb) || %s::jsonb,
                            finished_at=now(),
                            updated_at=now()
                        where identity_key=%s
                        """,
                        (
                            json.dumps({
                                "repair":"OFFICIAL_REGISTRY_RESEEDED",
                                "official_source":official.get("source"),
                                "official_source_url":official.get("source_url"),
                            }),
                            key,
                        ),
                    )

            repaired.append({
                "identity_key":key,
                "coin_id":str(coin_id),
                "stored_observations":stored,
                "production_coin_specs_updated":production_rows,
                "official_source":official.get("source"),
            })
        except Exception as e:
            errors.append({
                "country":country_key,"year":year,"denom":denom_key,
                "error":type(e).__name__,"message":str(e)[:300],
            })

    return {
        "ok":not errors,
        "registry_entries":len(OFFICIAL_ISSUER_SPECS),
        "repaired":repaired,
        "errors":errors,
    }



def quarantine_invalid_euro_jobs():
    """Mark queued jobs whose exact source coin_specs row has an impossible
    pre-euro issuer year as FAILED. Idempotent and conservative."""
    changed=[]
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                select q.id as job_id,q.status,q.coin_id,c.metadata
                from public.catalog_enrichment_queue q
                join public.coin_catalog c on c.id=q.coin_id
                where q.status in ('PENDING','RETRY')
            """)
            jobs=[dict(r) for r in cur.fetchall()]
            for j in jobs:
                md=dict(j.get("metadata") or {})
                sid=md.get("source_spec_id")
                if not sid:
                    continue
                cur.execute("""
                    select id,country,denomination_label,issue_year,year_from,year_to,
                           weight_g,diameter_mm,primary_metal
                    from public.coin_specs where id=%s
                """,(sid,))
                row=cur.fetchone()
                if not row:
                    continue
                reason=_invalid_euro_year_reason(dict(row))
                if not reason:
                    continue
                cur.execute("""
                    update public.catalog_enrichment_queue
                    set status='FAILED',
                        last_error='INVALID_LEGACY_EURO_YEAR',
                        result=coalesce(result,'{}'::jsonb) || %s::jsonb,
                        next_run_at=null,finished_at=now(),updated_at=now()
                    where id=%s
                """,(json.dumps({"quarantine":reason}),j["job_id"]))
                changed.append({"job_id":str(j["job_id"]),"source_spec_id":str(sid),**reason})
    return {"ok":True,"quarantined":len(changed),"jobs":changed}


def repair_euro_year_applicability():
    """One-time conservative repair of legacy standard-euro SERIES rows.
    Only rows whose denomination, weight and diameter exactly match a standard
    euro circulation profile are touched. issue_year rows are never rewritten."""
    repaired=[]
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                select id,country,denomination_label,issue_year,year_from,year_to,
                       weight_g,diameter_mm,primary_metal
                from public.coin_specs
            """)
            rows=[dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get("issue_year") is not None:
                    continue
                if not _is_exact_euro_profile_row(r):
                    continue
                country=_norm_country(r.get("country"))
                start=_EURO_COIN_START_YEAR.get(country)
                if not start:
                    continue
                try:
                    yf=int(r["year_from"]) if r.get("year_from") is not None else None
                except Exception:
                    yf=None
                if yf is not None and yf >= start:
                    continue
                cur.execute("""
                    update public.coin_specs
                    set year_from=%s,updated_at=now()
                    where id=%s
                    returning id
                """,(start,r["id"]))
                if cur.fetchone():
                    repaired.append({
                        "id":str(r["id"]),"country":r["country"],
                        "old_year_from":yf,"new_year_from":start,
                        "denomination_label":r["denomination_label"],
                    })
    q=quarantine_invalid_euro_jobs()
    return {"ok":True,"repaired_rows":len(repaired),"repaired":repaired,
            "queue_quarantine":q}


def auto_tick(enqueue_limit=20, max_seconds=22.0):
    """One bounded unattended catalogue cycle.

    1) Queue only NEW production candidates.
    2) Process exactly ONE due PENDING/RETRY job.
    This is intentionally safe for an external free cron hitting one URL.
    """
    started=time.time()
    quarantine_result=quarantine_invalid_euro_jobs()
    enqueue_result=enqueue_production_batch(limit=enqueue_limit,priority_start=1000)
    run_result=run_one(max_seconds=max_seconds)
    return {
        "ok":bool(enqueue_result.get("ok")) and bool(run_result.get("ok",True)),
        "mode":"AUTO_TICK_ONE_JOB",
        "quarantine":quarantine_result,
        "enqueue":enqueue_result,
        "run":run_result,
        "queue":queue_status(),
        "elapsed_seconds":round(time.time()-started,3),
    }




def repair_false_gold_euro_cents():
    """Schema-aware conservative repair for the Nordic-gold parser bug.

    The production schemas for coin_catalog and coin_specs are not assumed to
    contain the same columns. We introspect each table and update only columns
    that actually exist. Exact 10/20/50 euro-cent physical profiles only.
    """
    changed={
        "observations_quarantined":0,
        "coin_catalog_repaired":0,
        "coin_specs_repaired":0,
    }
    profiles=((0.10,4.10,19.75),(0.20,5.74,22.25),(0.50,7.80,24.25))

    def columns(cur, table):
        cur.execute("""
            select column_name
              from information_schema.columns
             where table_schema='public' and table_name=%s
        """,(table,))
        return {r[0] for r in cur.fetchall()}

    with _conn() as conn:
        with conn.cursor() as cur:
            obs_cols=columns(cur,"coin_observations")
            catalog_cols=columns(cur,"coin_catalog")
            specs_cols=columns(cur,"coin_specs")

            # Required identity/physical columns must exist; otherwise fail
            # without making a partial repair based on guesses.
            required_catalog={"id","denomination","weight_g","diameter_mm","primary_metal"}
            required_specs={"weight_g","diameter_mm","primary_metal"}
            missing_catalog=sorted(required_catalog-catalog_cols)
            missing_specs=sorted(required_specs-specs_cols)
            if missing_catalog or missing_specs:
                return {
                    "ok":False,
                    "error":"SCHEMA_MISSING_REQUIRED_COLUMNS",
                    "missing":{
                        "coin_catalog":missing_catalog,
                        "coin_specs":missing_specs,
                    },
                    "database_touched":False,
                }

            for denom,w,d in profiles:
                # Quarantine false Gold observations if the relevant observation
                # columns exist. This is evidence quarantine, never evidence rewrite.
                if {"coin_id","source","primary_metal","specs_usable"}.issubset(obs_cols):
                    raw_patch = ""
                    params=[]
                    if "raw_data" in obs_cols:
                        raw_patch=", raw_data=coalesce(o.raw_data,'{}'::jsonb) || %s::jsonb"
                        params.append(json.dumps({"quarantine":"NORDIC_GOLD_FALSE_GOLD_PARSER_BUG"}))
                    sql=f"""
                        update public.coin_observations o
                           set specs_usable=false {raw_patch}
                          from public.coin_catalog c
                         where o.coin_id=c.id
                           and lower(coalesce(o.source,''))='ma-shops'
                           and lower(trim(coalesce(o.primary_metal,'')))='gold'
                           and abs(c.denomination-%s)<=0.000001
                           and abs(coalesce(o.weight_g,c.weight_g)-%s)<=0.08
                           and abs(coalesce(o.diameter_mm,c.diameter_mm)-%s)<=0.18
                    """
                    cur.execute(sql,tuple(params+[denom,w,d]))
                    changed["observations_quarantined"]+=cur.rowcount

                # coin_catalog: update only columns that really exist.
                sets=["primary_metal='Nordic gold'"]
                params=[]
                if "composition" in catalog_cols:
                    sets.append("composition='Nordic gold'")
                if "fineness_per_mille" in catalog_cols:
                    sets.append("fineness_per_mille=null")
                if "fine_metal_g" in catalog_cols:
                    sets.append("fine_metal_g=null")
                if "metadata" in catalog_cols:
                    sets.append("metadata=coalesce(metadata,'{}'::jsonb) || %s::jsonb")
                    params.append(json.dumps({"repair":"NORDIC_GOLD_FALSE_GOLD_PARSER_BUG"}))
                if "updated_at" in catalog_cols:
                    sets.append("updated_at=now()")
                cur.execute(f"""
                    update public.coin_catalog
                       set {", ".join(sets)}
                     where abs(denomination-%s)<=0.000001
                       and abs(weight_g-%s)<=0.02
                       and abs(diameter_mm-%s)<=0.03
                       and lower(trim(coalesce(primary_metal,'')))='gold'
                """,tuple(params+[denom,w,d]))
                changed["coin_catalog_repaired"]+=cur.rowcount

                # coin_specs: production schema currently has no composition
                # column, so never assume one. Optional metal-value columns are
                # nulled only if they exist.
                sets=["primary_metal='Nordic gold'"]
                if "composition" in specs_cols:
                    sets.append("composition='Nordic gold'")
                if "fineness_per_mille" in specs_cols:
                    sets.append("fineness_per_mille=null")
                if "fine_metal_g" in specs_cols:
                    sets.append("fine_metal_g=null")

                where=[
                    "abs(weight_g-%s)<=0.02",
                    "abs(diameter_mm-%s)<=0.03",
                    "lower(trim(coalesce(primary_metal,'')))='gold'",
                ]
                params=[w,d]
                if "country" in specs_cols:
                    where.append("lower(trim(coalesce(country,''))) in %s")
                    params.append(tuple(str(x).lower() for x in _EURO_COUNTRIES))

                cur.execute(f"""
                    update public.coin_specs
                       set {", ".join(sets)}
                     where {" and ".join(where)}
                """,tuple(params))
                changed["coin_specs_repaired"]+=cur.rowcount

            return {
                "ok":True,
                **changed,
                "scope":"exact standard 10/20/50 euro-cent physical profiles only",
                "schema_detected":{
                    "coin_catalog_optional_columns":{
                        k:(k in catalog_cols) for k in
                        ("composition","fineness_per_mille","fine_metal_g","metadata","updated_at")
                    },
                    "coin_specs_optional_columns":{
                        k:(k in specs_cols) for k in
                        ("composition","fineness_per_mille","fine_metal_g")
                    },
                },
            }

def local_preflight():
    """DB-free smoke checks for deploy-time regressions in core helpers."""
    checks=[]

    def check(name, fn):
        try:
            fn()
            checks.append({"name":name,"ok":True})
        except Exception as e:
            checks.append({"name":name,"ok":False,"error":type(e).__name__,"message":str(e)[:300]})

    def denomination_regression_matrix():
        cases = [
            ("1/2 Dollar",0.5),("1/2 franc",0.5),("½ franc",0.5),
            ("100 lekë",100.0),("10 feninga",10.0),("20 feninga",20.0),
            ("5 feninga",5.0),("50 feninga",50.0),
            ("1 KM",1.0),("2 KM",2.0),("5 KM",5.0),
            ("10 senti",10.0),("5 senti",5.0),("20 senti",20.0),("50 senti",50.0),
            ("1 króna",1.0),("5 krónur",5.0),("10 krónur",10.0),
            ("0.1",0.1),("0.2",0.2),("0.5",0.5),("0,5",0.5),
            ("200 forint",200.0),("1000 lire",1000.0),
        ]
        failures=[]
        for label, expected in cases:
            value, unit = _parse_denom(label)
            if abs(float(value)-expected) > 1e-12:
                failures.append(f"{label}: {value!r} != {expected!r}")
        if failures:
            raise AssertionError("; ".join(failures))

    check("denomination_regression_matrix", denomination_regression_matrix)
    check("identity_key_stable", lambda: (
        (_identity_key("Serbia",2003,5,"dinar","x") ==
         _identity_key(" Serbia ",2003,5.0,"DINAR"," X ")) or
        (_ for _ in ()).throw(AssertionError("identity normalization failed"))
    ))
    check("real_dict_cursor_imported", lambda: (
        RealDictCursor is not None or
        (_ for _ in ()).throw(AssertionError("RealDictCursor missing"))
    ))
    check("euro_gold_conflict_quarantined", lambda: (
        (_euro_profile_conflict({
            "country":"Austria","denomination_label":"0.1",
            "weight_g":4.1,"diameter_mm":19.75,"primary_metal":"gold"
        }) is not None) or
        (_ for _ in ()).throw(AssertionError("euro contradiction guard failed"))
    ))
    check("nordic_gold_not_quarantined", lambda: (
        (_euro_profile_conflict({
            "country":"Austria","denomination_label":"0.1",
            "weight_g":4.1,"diameter_mm":19.75,"primary_metal":"Nordic gold"
        }) is None) or
        (_ for _ in ()).throw(AssertionError("Nordic gold false positive"))
    ))

    check("andorra_1999_euro_rejected", lambda: (
        (_invalid_euro_year_reason({
            "country":"Andorra","denomination_label":"0.2","issue_year":None,
            "year_from":1999,"year_to":None,"weight_g":5.74,"diameter_mm":22.25
        }) is not None) or
        (_ for _ in ()).throw(AssertionError("Andorra 1999 must be quarantined"))
    ))
    check("andorra_2014_euro_allowed", lambda: (
        (_invalid_euro_year_reason({
            "country":"Andorra","denomination_label":"0.2","issue_year":None,
            "year_from":2014,"year_to":None,"weight_g":5.74,"diameter_mm":22.25
        }) is None) or
        (_ for _ in ()).throw(AssertionError("Andorra 2014 must be allowed"))
    ))
    check("croatia_1999_euro_rejected", lambda: (
        (_invalid_euro_year_reason({
            "country":"Croatia","denomination_label":"0.5","issue_year":None,
            "year_from":1999,"year_to":None,"weight_g":7.8,"diameter_mm":24.25
        }) is not None) or
        (_ for _ in ()).throw(AssertionError("Croatia 1999 must be quarantined"))
    ))

    return {"ok":all(c["ok"] for c in checks),"checks":checks,"database_touched":False}


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
