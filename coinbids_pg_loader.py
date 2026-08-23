#!/usr/bin/env python3
"""
CoinBids — loads CoinSpec records (from coinbids_database_builder_v2.py's
migrate_legacy()/parse_*() functions) into PostgreSQL via upsert, with
import-run logging. This is the "write to Postgres" half the builder itself
does not do — the builder produces JSON/CSV; this script is what actually
gets those records into the database the backend reads from.

Usage:
    python3 coinbids_pg_loader.py --database-url "$DATABASE_URL" \
        --legacy coin_specs_database.json \
        --legacy coin_specs_database_MASTER_EUROPE_v17.json

Never rebuilds the whole table from scratch — every record is an UPSERT
keyed on record_id (see coinbids_coin_specs_schema.sql), so a source that
fails to fetch this run does not wipe out data from a previous successful
run, and running this script twice with the same input never duplicates
rows.
"""
from __future__ import annotations
import argparse, sys, time
from pathlib import Path

import psycopg2
import psycopg2.extras

from coinbids_database_builder_v2 import (
    CoinSpec, migrate_legacy, dedupe, parse_ec_common, parse_ecb_greece,
    import_mint_products, Fetcher, audit,
)


def upsert_records(conn, records: list[CoinSpec], source_adapter: str) -> dict:
    """Upserts a batch of CoinSpec records (all from ONE source_adapter run)
    into coin_specs + coin_spec_sources + coin_aliases +
    coin_spec_external_ids. Wrapped in a single transaction per batch — a
    genuine mid-batch failure rolls the whole batch back rather than leaving
    the database half-updated, and logs an import_run row either way."""
    inserted = 0
    updated = 0
    failed = 0
    started_at = time.time()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into coin_spec_import_runs (source_adapter, status, discovered) "
                "values (%s, 'running', %s) returning id",
                (source_adapter, len(records)),
            )
            run_id = cur.fetchone()[0]

        for r in records:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into coin_specs (
                            record_id, country_code, country, currency_code,
                            denomination_value, denomination_label,
                            year_from, year_to, issue_year, coin_type, variant, title,
                            composition_text, primary_metal, fineness_per_mille,
                            weight_g, diameter_mm, thickness_mm, fine_metal_g,
                            edge, mint, mintmark, mintage,
                            source_priority, confidence, verified, metal_value_ready
                        ) values (
                            %(record_id)s, %(country_code)s, %(country)s, %(currency_code)s,
                            %(denomination_value)s, %(denomination_label)s,
                            %(year_from)s, %(year_to)s, %(issue_year)s, %(coin_type)s, %(variant)s, %(title)s,
                            %(composition_text)s, %(primary_metal)s, %(fineness_per_mille)s,
                            %(weight_g)s, %(diameter_mm)s, %(thickness_mm)s, %(fine_metal_g)s,
                            %(edge)s, %(mint)s, %(mintmark)s, %(mintage)s,
                            %(source_priority)s, %(confidence)s, %(verified)s, %(metal_value_ready)s
                        )
                        on conflict (record_id) do update set
                            country_code=excluded.country_code, country=excluded.country,
                            currency_code=excluded.currency_code,
                            denomination_value=excluded.denomination_value,
                            denomination_label=excluded.denomination_label,
                            year_from=excluded.year_from, year_to=excluded.year_to,
                            issue_year=excluded.issue_year, coin_type=excluded.coin_type,
                            variant=excluded.variant, title=excluded.title,
                            composition_text=excluded.composition_text,
                            primary_metal=excluded.primary_metal,
                            fineness_per_mille=excluded.fineness_per_mille,
                            weight_g=excluded.weight_g, diameter_mm=excluded.diameter_mm,
                            thickness_mm=excluded.thickness_mm, fine_metal_g=excluded.fine_metal_g,
                            edge=excluded.edge, mint=excluded.mint, mintmark=excluded.mintmark,
                            mintage=excluded.mintage, source_priority=excluded.source_priority,
                            confidence=excluded.confidence, verified=excluded.verified,
                            metal_value_ready=excluded.metal_value_ready
                        returning id, (xmax = 0) as was_insert
                        """,
                        {
                            "record_id": r.record_id, "country_code": r.country_code, "country": r.country,
                            "currency_code": r.currency_code, "denomination_value": r.denomination_value,
                            "denomination_label": r.denomination_label, "year_from": r.year_from,
                            "year_to": r.year_to, "issue_year": r.issue_year, "coin_type": r.coin_type,
                            "variant": r.variant, "title": r.title, "composition_text": r.composition_text,
                            "primary_metal": r.primary_metal, "fineness_per_mille": r.fineness_per_mille,
                            "weight_g": r.weight_g, "diameter_mm": r.diameter_mm, "thickness_mm": r.thickness_mm,
                            "fine_metal_g": r.fine_metal_g, "edge": r.edge, "mint": r.mint,
                            "mintmark": r.mintmark, "mintage": r.mintage,
                            "source_priority": r.source_priority, "confidence": r.confidence,
                            "verified": r.verified, "metal_value_ready": r.metal_value_ready,
                        },
                    )
                    spec_id, was_insert = cur.fetchone()
                    if was_insert:
                        inserted += 1
                    else:
                        updated += 1
                        # Re-inserting sources/aliases/external_ids on every
                        # upsert would duplicate them — clear and
                        # re-populate for this spec_id instead.
                        cur.execute("delete from coin_spec_sources where coin_spec_id=%s", (spec_id,))
                        cur.execute("delete from coin_aliases where coin_spec_id=%s", (spec_id,))
                        cur.execute("delete from coin_spec_external_ids where coin_spec_id=%s", (spec_id,))

                    for p in r.provenance:
                        cur.execute(
                            "insert into coin_spec_sources (coin_spec_id, source_name, source_url, source_type, source_license, retrieved_at, note) "
                            "values (%s,%s,%s,%s,%s,%s,%s)",
                            (spec_id, p.source_name, p.source_url, p.source_type, p.license, p.retrieved_at, p.note),
                        )
                    for a in r.aliases:
                        if a:
                            cur.execute(
                                "insert into coin_aliases (coin_spec_id, alias) values (%s,%s)",
                                (spec_id, a),
                            )
                    for ns, ext_id in r.external_ids.items():
                        cur.execute(
                            "insert into coin_spec_external_ids (coin_spec_id, namespace, external_id) values (%s,%s,%s)",
                            (spec_id, ns, ext_id),
                        )
            except Exception as e:
                failed += 1
                print(f"[loader] FAILED to upsert {r.record_id} ({r.country} {r.currency_code} {r.denomination_value}): {e}", file=sys.stderr)

        with conn.cursor() as cur:
            cur.execute(
                "update coin_spec_import_runs set finished_at=now(), status=%s, inserted=%s, updated=%s, failed=%s where id=%s",
                ("success" if failed == 0 else "partial_failure", inserted, updated, failed, run_id),
            )

    elapsed = time.time() - started_at
    return {"source_adapter": source_adapter, "inserted": inserted, "updated": updated, "failed": failed, "elapsed_seconds": round(elapsed, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--legacy", action="append", default=[], help="Legacy JSON file(s) to migrate (can repeat)")
    ap.add_argument("--live-ec", action="store_true", help="Also fetch live European Commission common euro specs")
    ap.add_argument("--live-ecb-greece", action="store_true", help="Also fetch live ECB Greece national-side data")
    ap.add_argument("--live-mint", action="store_true", help="Also fetch live Bank of Greece Mint product pages")
    ap.add_argument("--max-mint-pages", type=int, default=250)
    args = ap.parse_args()

    conn = psycopg2.connect(args.database_url)
    results = []

    all_legacy = []
    for path_str in args.legacy:
        recs = migrate_legacy(Path(path_str))
        all_legacy.extend(recs)
        print(f"[loader] {path_str}: {len(recs)} records after country expansion")
    if all_legacy:
        deduped = dedupe(all_legacy)
        print(f"[loader] {len(all_legacy)} total -> {len(deduped)} after dedupe across all legacy files")
        results.append(upsert_records(conn, deduped, "legacy_migration"))

    if args.live_ec or args.live_ecb_greece or args.live_mint:
        fetch = Fetcher()
        common = None
        if args.live_ec:
            common = parse_ec_common(fetch)
            results.append(upsert_records(conn, common, "european_commission_live"))
        if args.live_ecb_greece:
            if common is None:
                common = parse_ec_common(fetch)
            ecb_recs = parse_ecb_greece(fetch, common)
            results.append(upsert_records(conn, ecb_recs, "ecb_greece_live"))
        if args.live_mint:
            mint_recs, failures = import_mint_products(fetch, args.max_mint_pages)
            results.append(upsert_records(conn, mint_recs, "bank_of_greece_mint_live"))
            if failures:
                print(f"[loader] {len(failures)} Mint page(s) failed to parse (logged, not fatal)")

    conn.close()
    print()
    print("=== Summary ===")
    for r in results:
        print(f"  {r['source_adapter']}: +{r['inserted']} inserted, {r['updated']} updated, {r['failed']} failed ({r['elapsed_seconds']}s)")


if __name__ == "__main__":
    main()
