# CoinBids vNext — Changelog, Regression Report & Known Limitations
Date: 2026-08-14

This document is the deliverable requested by `COINBIDS_MASTER_VNEXT_SPEC_FOR_CLAUDE.txt`
§25 (implementation plan, changelog, regression report, known limitations, deployment
notes), covering the gaps identified after auditing the vNext build against all seven
spec documents plus `CoinBids_MASTER_HANDOFF_FOR_CLAUDE.txt`.

**Scope of this pass:** the vNext build already correctly implemented the large majority
of the audit/spec requirements (verified directly in code — see prior conversation turn
for the full item-by-item comparison). This pass fixes the items that were still open,
plus adds the evidence-transparency UI the Auction Intelligence spec requires. It does
**not** rewrite the app and does not touch anything that was already working.

---

## 1. Changes made, mapped to requirement

| Spec ref | Requirement | Change |
|---|---|---|
| BUG_AUDIT H12 / TOP2 §5E | Grade must be a real comparison dimension, not ignored | Backend: added `grade_tier()`/`grade_conflicts()` — MINT vs CIRCULATED tier classification. Wired into `passes_hard_filter()` (hard reject on conflict) and `score_title()` (soft boost on match). Unstated listing grade is never treated as a mismatch. |
| BUG_AUDIT H06 | Dashboard summed mixed currencies and labelled the total "EUR" | Frontend: `financeSummary` now groups by currency and displays each total separately instead of adding e.g. USD + EUR and calling it €. |
| BUG_AUDIT H11 | "1.234 EUR" thousands-vs-decimal ambiguity | `num()`: unambiguous multi-group case (`1.234.567`) now parsed as thousands. Single-group case remains a documented limitation (see §3) — genuinely ambiguous without site-locale context. |
| MASHOPS_PRICE_SHIPPING_BUG | Only "shipping: amount" ordering was parsed | `extract_prices_with_shipping()`: now also matches MA-Shops' actual "amount + shipping (to Greece)" ordering, gated by a leading `+` or a destination token so it can't misread an item price as shipping. |
| BUG_AUDIT M26 | Shipping parsed in a different currency than item price could be added directly | Shipping is now bridged through EUR into the item's currency before being attached to an offer; a failed conversion leaves shipping unknown rather than silently wrong. |
| BUG_AUDIT M29 | Currency selector only actually converted to EUR | `coin_search()` now also converts to USD/GBP/CHF targets when FX data is available. |
| BUG_AUDIT M16 | Frontend-computed `variants[]` was generated but never read by the backend | `make_queries()` now merges validated frontend variant queries in. |
| BUG_AUDIT M25 | No explicit rejection of sold/out-of-stock listings | Added `looks_unavailable()` + JSON-LD `availability` check; both extraction paths now drop confirmed-unavailable listings before they can reach ranking. |
| BUG_AUDIT M24 | No caching — every identical search re-scraped MA-Shops | Added a 15-minute in-memory TTL cache keyed on normalized coin identity + shipping/currency mode. Only successful (non-empty) results are cached, so a transient block/timeout is never "frozen in" as the answer. |
| BUG_AUDIT L02 | No rate limiting | Added a per-IP, per-minute limiter (`COINBIDS_RATE_LIMIT_PER_MIN`, default 30) on all `/api/*` routes, returning HTTP 429 past the limit. |
| BUG_AUDIT M06/M07 | Selling 1 of 5 owned coins marked the whole record SOLD | `saleForm` now has a "quantity to sell" field. Selling fewer than the owned quantity splits a new SOLD record off (own ID, own timestamps) and reduces the original OWNED record's quantity — same-record-ID lifecycle preserved, physical-count invariant preserved. |
| BUG_AUDIT M10 | Legacy sheet import silently forced `currency='EUR'` | Currency is still defaulted to EUR (the legacy sheet has no currency column, and EUR is overwhelmingly the correct guess for this collection) but every such row is now explicitly flagged in its notes as an *assumption*, not verified data. |
| BUG_AUDIT M11/M12 | Legacy importer trusted fixed column positions with no verification | Added a header-row sanity check (looks for "country"/"year"-like headers in the expected columns) that asks for explicit confirmation before importing a workbook whose layout doesn't match, instead of silently mis-mapping. |
| BUG_AUDIT M14/M15 | "Save / Update XLSX" implied real save-back; dead `currentFileHandle` | Button relabelled "Download updated XLSX" with accurate tooltip/alert text. Removed the unused `currentFileHandle` variable. |
| — (new, found during this pass) | `parseCoinText()`'s `grade` field always read the **Research page's** `rGrade` DOM input, regardless of which page called it | This silently leaked whatever grade was last typed on the Research page into Auction Intelligence searches (which has no grade field of its own). Grade is now extracted directly from the input text (UNC/BU/Proof/XF/VF/MS65/PCGS/NGC/etc.) in `parseCoinText()` itself; the Research page's manual `rGrade` field, when filled in, still takes priority for that page only. |
| AUCTION_INTELLIGENCE §49 | Technical evidence should be under "Why? / Evidence", not cluttering the main result | Added a collapsible "Why? / Evidence used" panel under both Live Bid Advisor and Sell-at-Auction results, listing the exact MA-Shops offers and realized comparables that fed the estimate, with links. |
| MASTER_HANDOFF §25 deployment | Expected filenames (`Procfile`) missing | Added `Procfile` (`gunicorn numisvault_backend:app`). Corrected README filename references (`CoinBids_App.html`/`coinbids_backend.py` → actual `index.html`/`numisvault_backend.py`). |

## 2. Regression tests run

All backend changes were unit-tested directly against the module (pure-function level;
no live network calls, consistent with this sandbox's restrictions — see §3):

```
PASS  num 1.234,56 EUR -> 1234.56
PASS  num 1,234.56 USD -> 1234.56
PASS  num 12,34 EUR -> 12.34
PASS  num 12.34 USD -> 12.34
PASS  num 1.234.567 -> 1234567 (unambiguous multi-dot)
PASS  2 euro rejects 1 euro 2025
PASS  2 euro accepts 2 euro 2025
PASS  10 cent rejects 5 cent 2010
PASS  10 cent accepts 10 cent 2010
PASS  grade_tier UNC -> MINT
PASS  grade_tier VF -> CIRCULATED
PASS  grade_conflicts UNC vs VF listing -> True
PASS  grade_conflicts UNC vs unknown-grade listing -> False
PASS  grade_conflicts UNC vs UNC listing -> False
PASS  classify banknote
PASS  classify coin
PASS  Kursmünzensatz -> SET
PASS  single coin -> SINGLE_COIN
PASS  looks_unavailable sold
PASS  looks_unavailable normal
PASS  MA-Shops repro: title "2 €" face value never becomes price (price=3.95)
PASS  MA-Shops repro: shipping = 7.50 (amount-before-"shipping" ordering)
PASS  MA-Shops: "label: amount" ordering still works (no regression)
PASS  hard_filter rejects wrong denomination (1 euro)
PASS  hard_filter accepts correct 2 euro 2025
PASS  hard_filter rejects banknote
PASS  hard_filter rejects Kursmünzensatz set
PASS  hard_filter rejects wrong variant (Pula Amphitheater)
PASS  hard_filter accepts correct variant (King Tomislav)
PASS  hard_filter rejects grade mismatch (VF listing for UNC request)
PASS  hard_filter accepts matching grade (UNC listing for UNC request)
PASS  hard_filter allows unknown-grade listing (no false reject)
```
30/30 passed. Python `py_compile` and Node `--check` both pass clean on the full files.

**Not tested here (requires live network / real credentials, unavailable in this
sandbox):** real Numista API responses, live MA-Shops HTML fetch/parsing against
today's actual markup, live FX rate endpoint, production DNS/HTTPS on coinbids.eu.
These must be verified on your machine or on Render before calling this "done" —
consistent with `COINBIDS_VALIDATION_REPORT.txt`'s own caveat.

## 3. Known limitations (honest, not fixed in this pass)

- **H11 price parsing, single-dot case:** `"1.234 EUR"` with no other separator is
  genuinely ambiguous (could be €1.234 or €1,234) without knowing the source site's
  locale. Left unresolved rather than guessed silently — multi-dot and comma+dot cases
  are unambiguous and are now handled correctly.
- **H03 (dead code, harmless):** `COUNTRY_SYNONYMS` in the backend still has Greek keys
  while the frontend now always sends English country names, so that branch never
  fires. It's dead code, not a live bug — the actual country hard-filter uses the
  separate, working `COUNTRY_CANON`/`canonical_country()` system. Left as-is to avoid
  touching working matching logic; flagged for cleanup.
- **M18:** Year parser still only accepts 1000–2099 (product-scope limitation for
  modern-era coins, not addressed).
- **M19:** "Identification readiness" score is still a completeness heuristic, not a
  real catalogue-matched confidence score.
- **Automated realized-auction retrieval (the Auction Intelligence spec's core ask,
  §3 and the handoff's §14) is still NOT implemented.** This requires verified,
  permitted automated access to Heritage/Stack's Bowers/Sixbid/NumisBids/PCGS —
  none of which this sandbox can reach or test (network here is restricted to package
  registries only, not arbitrary web/auction-house domains). Per both specs'
  explicit instruction ("do not invent provider capabilities" / "prove with a small
  test that the required information can actually be retrieved reliably"), this
  must be built and verified against each real provider's actual API/ToS on a
  machine with real internet access — it is not something to fabricate here.
  The current Live Bid Advisor / Sell-at-Auction modes remain honest about this:
  they use validated MA-Shops offers plus user-supplied realized comparables only,
  and explicitly report low confidence when no realized sales are supplied.
- **Backtesting (spec §20 / handoff Phase G):** not implemented — there is no
  automated historical-sales pipeline yet to backtest against.
- Rate limiting and the search cache are in-memory (per-process). On Render's free
  tier with a single worker this is fine; if you scale to multiple workers/dynos
  later, both need to move to a shared store (e.g. Redis) or they'll be inconsistent
  across instances.

## 4. Deployment notes (coinbids.eu)

Unchanged from `README_COINBIDS.txt`, restated for completeness:

- Render web service serves both `index.html` (`GET /`) and `/api/*` from
  `numisvault_backend.py` via the new `Procfile`.
- Required env vars: `NUMISTA_API_KEY`, `COINBIDS_CORS_ORIGINS=https://coinbids.eu,https://www.coinbids.eu`,
  optionally `COINBIDS_RATE_LIMIT_PER_MIN` (default 30).
- `PORT` is supplied by Render automatically; the `Procfile` binds to it.
- Confirm `COINBIDS_CORS_ORIGINS` is actually set in production — the backend
  defaults to `"*"` when it is not, which is fine for local dev but must not ship
  unset to production.
- After deploying: hard-refresh in a normal browser and re-test in a fresh
  incognito window (empty collection, no private data, no stale cached HTML).

## 5. Suggested next steps (not done here)

1. Verify live: real Numista responses, real MA-Shops fetch/parse against current
   markup, real FX rates, real coinbids.eu HTTPS/DNS — all flagged untestable in
   this sandbox.
2. If/when an auction-archive source is confirmed technically + legally accessible,
   build it as one modular provider adapter at a time (per the Auction Intelligence
   spec's Phase A), starting with a small proof-of-query before wiring it into
   valuation.
3. Consider moving the in-memory cache/rate-limit state to Redis before scaling
   past a single backend process.
