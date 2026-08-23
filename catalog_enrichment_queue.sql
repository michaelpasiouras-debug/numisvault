-- CoinBids catalogue enrichment queue
-- Safe additive migration: creates ONE new table; existing catalogue tables remain unchanged.

create table if not exists public.catalog_enrichment_queue (
    id uuid primary key default gen_random_uuid(),
    coin_id uuid not null references public.coin_catalog(id) on delete cascade,
    identity_key text not null unique,
    country text not null,
    year integer not null,
    denomination_text text not null,
    variant text,
    priority integer not null default 100,
    status text not null default 'PENDING'
        check (status in ('PENDING','RUNNING','RETRY','DONE','FAILED')),
    attempts integer not null default 0,
    last_error text,
    result jsonb default '{}'::jsonb,
    next_run_at timestamptz default now(),
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists catalog_enrichment_queue_status_idx
on public.catalog_enrichment_queue(status, next_run_at, priority, created_at);

select status, count(*) from public.catalog_enrichment_queue group by status;
