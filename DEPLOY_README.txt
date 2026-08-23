COINBIDS TARGETED REPAIR — 23 AUG 2026

DEPLOY THESE FIVE FILES:
- index.html
- public_home.html
- public.css
- coinbids-logo.png
- numisvault_backend.py

FIXED:
1. Public mobile header no longer overflows with desktop navigation / giant Sign up.
2. The exact CoinBids coin+magnifier logo embedded in the working app is restored as a real asset.
3. The fake giant CB NUMISMATICS hero coin is removed and replaced with the real logo.
4. Authenticated app header is visually compacted on mobile only; controls/IDs/listeners are preserved.
5. Price Research now distinguishes:
   - no MA-Shops candidates at all, from
   - candidates found but rejected by identity validation.
   It displays the main rejection reasons instead of the misleading generic message.
6. Backend validation remains strict. No fuzzy relaxation was introduced.
7. Existing bounded Price Research and cron-observability backend lineage is preserved.

IMPORTANT:
“Greece 10 euros 2021 mechanism” is internally inconsistent: the Greek €10
Antikythera Mechanism issue is 2022, not 2021. CoinBids must NOT fabricate a price
for the 2021+mechanism combination. It should explain that candidates were found but
failed explicit year/type validation.

CHECKS:
{
  "index_js_syntax": true,
  "backend_python_compile": true,
  "exact_app_logo_recovered": true,
  "fake_CB_NUMISMATICS_removed": true,
  "public_mobile_overflow_fix": true,
  "price_rejection_diagnostics_visible": true,
  "strict_hard_filter_preserved": true,
  "bounded_query_limit_preserved": true,
  "bounded_pages_preserved": true,
  "auction_comparables_fix_preserved": true,
  "sales_fix_preserved": true
}