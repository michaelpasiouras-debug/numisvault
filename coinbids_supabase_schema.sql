-- ============================================================================
-- COINBIDS — SUPABASE SCHEMA + ROW LEVEL SECURITY MIGRATION
-- ============================================================================
-- Run this ENTIRE script once in the Supabase SQL Editor
-- (Dashboard -> your project -> SQL Editor -> New query -> paste -> Run)
-- BEFORE deploying the new index.html. The new frontend expects this table
-- and these policies to already exist.
--
-- Safe to re-run: every statement uses IF NOT EXISTS / OR REPLACE / DROP
-- POLICY IF EXISTS guards, so running this script twice does not error or
-- duplicate anything.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. TABLE
-- ----------------------------------------------------------------------------
-- Defensive: gen_random_uuid() is built into modern Postgres, but this
-- extension guarantees it's available regardless of the exact Postgres
-- version underlying this Supabase project. Safe/idempotent to run even if
-- already enabled.
create extension if not exists pgcrypto;

-- One row = one CoinBids record (an OWNED, PENDING, WISHLIST, or SOLD coin).
-- `client_id` is the app-generated id already used throughout the existing
-- frontend (the uid() JS function, e.g. "C8J2K3L9ABC") — kept as its own
-- column (not repurposed as the Postgres primary key) so the existing
-- frontend code that reads/writes c.id does not need to change at all.
-- Most fields are stored as `text` rather than a stricter numeric/date type:
-- the existing frontend already treats these loosely (a price field can
-- legitimately be an empty string, a quantity field can arrive as a raw
-- string from an HTML input, etc.) — matching that exactly avoids any
-- risk of an upsert failing or silently coercing/losing data because of a
-- stricter column type than the app actually produces.
create table if not exists public.coins (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  client_id text not null,
  status text not null check (status in ('OWNED','PENDING','WISHLIST','SOLD')),
  country text default '',
  denomination text default '',
  year text default '',
  mint text default '',
  variant text default '',
  metal text default '',
  grade text default '',
  catalog_ref text default '',
  quantity text default '',
  buy_price text default '',
  currency text default '',
  vendor text default '',
  order_date text default '',
  received_date text default '',
  certificate text default '',
  notes text default '',
  sale_price text default '',
  sale_currency text default '',
  sale_date text default '',
  buyer text default '',
  sale_notes text default '',
  created_at text default '',
  updated_at text default '',
  synced_at timestamptz not null default now(),  -- server-side bookkeeping only; not read/written by the app logic
  unique (user_id, client_id)
);

comment on table public.coins is 'CoinBids per-user Collection/Pending/Wishlist/Sold records. One row per coin record, owned by exactly one auth.users row via user_id.';
comment on column public.coins.client_id is 'The app-generated id (uid() in the frontend) — used as the upsert conflict key together with user_id, so re-saving/re-importing the same record updates it instead of duplicating it.';

-- Helpful indexes for the query patterns the app actually uses (load-on-
-- login by user_id; dashboard counts/filters by user_id+status).
create index if not exists coins_user_id_idx on public.coins (user_id);
create index if not exists coins_user_status_idx on public.coins (user_id, status);

-- Keep `updated_at`'s sibling bookkeeping column current automatically on
-- every UPDATE (separate from the app's own `updated_at` text field, which
-- the frontend sets itself — this is purely for server-side visibility).
create or replace function public.coins_set_synced_at()
returns trigger
language plpgsql
as $$
begin
  new.synced_at = now();
  return new;
end;
$$;

drop trigger if exists coins_set_synced_at_trigger on public.coins;
create trigger coins_set_synced_at_trigger
  before update on public.coins
  for each row
  execute function public.coins_set_synced_at();

-- ----------------------------------------------------------------------------
-- 2. ROW LEVEL SECURITY — MANDATORY
-- ----------------------------------------------------------------------------
-- This is the actual security boundary: even though the frontend also
-- filters by the logged-in user, that is a UX convenience only. Without RLS
-- enabled and these policies, ANY authenticated (or even anonymous, if the
-- table were exposed without RLS) caller could read or write ANY row via
-- the Supabase REST API directly, regardless of what the frontend code
-- does. The database itself must be the thing that refuses cross-account
-- access — these policies are that enforcement.
alter table public.coins enable row level security;

-- (Re-creatable: drop-if-exists then create, so this script is safe to
-- re-run without erroring on "policy already exists".)
drop policy if exists coins_select_own on public.coins;
create policy coins_select_own
  on public.coins
  for select
  using (auth.uid() = user_id);

drop policy if exists coins_insert_own on public.coins;
create policy coins_insert_own
  on public.coins
  for insert
  with check (auth.uid() = user_id);

drop policy if exists coins_update_own on public.coins;
create policy coins_update_own
  on public.coins
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists coins_delete_own on public.coins;
create policy coins_delete_own
  on public.coins
  for delete
  using (auth.uid() = user_id);

-- ----------------------------------------------------------------------------
-- 3. WHAT THIS DOES *NOT* DO (read before assuming more was set up)
-- ----------------------------------------------------------------------------
-- - Does NOT touch Supabase Auth configuration (email/password, Google
--   OAuth) — that was already configured in an earlier phase of this
--   project and is unaffected by this script.
-- - Does NOT create any table for `offers` (Price Research results cache).
--   The frontend change in this pass deliberately keeps `offers` as a
--   localStorage-only cache, not synced to Supabase — that was out of the
--   scope of "Collection / Pending / Wishlist / Sold data must persist",
--   which is specifically about the coins table above. If you want Price
--   Research results to also persist per-account later, that is a separate,
--   additional table + policies, not included here.
-- - Does NOT grant the frontend the service_role key. The frontend
--   continues to use ONLY the existing publishable/anon key
--   (SUPABASE_PUBLISHABLE_KEY already in index.html) plus the user's own
--   authenticated session — RLS is what makes that safe.
--
-- ============================================================================
-- END OF SCRIPT
-- ============================================================================
