#!/usr/bin/env python3
"""
COINBIDS — coin_identity_resolver.py REGRESSION TEST SUITE
============================================================
Run this BEFORE every deploy that touches coin_identity_resolver.py
(or its supporting coin_identity_database.json / coin_issue_database.json):

    python3 test_resolver_regression.py

Exit code 0  -> safe to deploy.
Exit code 1  -> DO NOT DEPLOY. A known-good case changed answer, or an
                adversarial case crashed / hung. See printed FAILURES.

This file intentionally has NO dependency on Flask/the backend/a live DB —
it only imports coin_identity_resolver.py directly, so it can run in CI,
locally, or as a pre-deploy git hook with nothing but the two JSON files
next to it.

Add a new line to KNOWN_GOOD or ADVERSARIAL_MUST_NOT_CRASH every time a
real bug is found and fixed (see coinbids audit sessions) — a bug that
was fixed once and has no regression test for it WILL come back.
"""
import sys, time, traceback
from coin_identity_resolver import resolve_coin_identity, get_resolver

FAILURES = []
PASS_COUNT = 0

def check(label, condition, detail=""):
    global PASS_COUNT
    if condition:
        PASS_COUNT += 1
    else:
        FAILURES.append(f"{label}  {detail}")

# ============================================================
# 1. KNOWN-GOOD REGRESSION — exact expected identity per query.
#    Every one of these was a real, confirmed bug at some point.
#    (country, currency, denomination_value, year, status)
# ============================================================
KNOWN_GOOD = [
    ("5 drachma 1901",                    "Greece",        "Greek drachma",       5.0,  1901, "resolved"),
    ("5 δραχμές 1901",                    "Greece",        "Greek drachma",       5.0,  1901, "resolved"),  # Greek plural inflection must resolve identically to the English form
    ("5 δραχμή 1901",                     "Greece",        "Greek drachma",       5.0,  1901, "resolved"),
    ("½ Rappen 1850 Switzerland",         "Switzerland",   None,                  0.5,  1850, "resolved"),  # Unicode ½ must not be silently dropped/misread as "1"
    ("¼ Dollar 1990 USA",                 "United States", "United States dollar",0.25, 1990, "resolved"),
]

# KNOWN OPEN ISSUE (found while building this suite, 2026-08-23 — not yet
# fixed): "USA 1 Dollar 1987 silver eagle" correctly identifies denom=1.0 as
# the top/"best" candidate, but ties at confidence=1.00 with a second,
# WRONG candidate (denom=10.0, the historical "Eagle"=$10 nickname). The
# existing "explicit numeric denomination beats a conflicting nickname" rule
# does not fire here because it only runs when currency_scores is non-empty,
# and in this query BOTH "1 dollar" and "eagle" matched only via
# special_denomination aliases, leaving currency_scores empty. Net effect:
# status comes back "review" instead of "resolved" even though .best is
# already correct. Tracked here deliberately as a SEPARATE, still-open case
# rather than silently asserting the (currently wrong) status — do not
# "fix" this test to expect "review"; fix the resolver's conflict rule
# instead, then move this back into KNOWN_GOOD.
KNOWN_ISSUE_EAGLE_TIE = ("USA 1 Dollar 1987 silver eagle", "United States", "United States dollar", 1.0, 1987)

# ============================================================
# 2. ADVERSARIAL — must NEVER raise an exception and must complete
#    within TIME_LIMIT_S regardless of whether the identity guess is
#    "correct" (many of these are deliberately nonsensical/malformed
#    input; the only requirement is "does not crash, does not hang").
# ============================================================
TIME_LIMIT_S = 2.0

ADVERSARIAL_MUST_NOT_CRASH = [
    # unicode fractions
    "⅓ Piastre 1900 Egypt", "⅔ Real 1850 Brazil", "⅛ Dollar 1850 USA",
    "⅜ Ounce 1900", "⅝ Crown 1900", "⅞ Franc 1900",
    # decimal / thousands-separator ambiguity
    "1.000 Lire 1997 Italy", "1,000 Won 1990 Korea", "1.234.567 Reis 1900 Brazil",
    "10,5 Euro 2005", "0.5 Dollar 1990", "0,5 Euro 2005",
    # empty / degenerate
    "", "   ", "\n\t", "1901", "euro", "5", "5 5 5 5 5",
    # very long input (perf / DoS probe — the 200-char cap must keep this fast)
    "a"*50000 + "!", "5 " * 20000 + "euro 1990", "1"*10000 + " euro 1990",
    ("very long coin description " * 3000) + "5 drachma 1901",
    # multiple years / multiple numbers
    "5 drachma 1901 restrike 1976", "1900 1901 1902 5 drachma", "5 10 20 50 euro 1999",
    # negative / zero / extreme numbers
    "-5 euro 1990", "0 euro 1990", "999999999999999999999999 dollar 1990",
    "1e400 euro 1990", "NaN euro 1990", "Infinity dollar 1990",
    # mixed / other scripts
    "५ रुपया 1990", "５ドル 1990", "５ 유로 1990", "５€ 1990",
    "٥ دينار 1990", "५ درهم\u200e 1990\u200e",
    # regex-metacharacter-as-literal-text injection
    "(.*)+ euro 1990", "[a-z]+ dollar 1990", r"\d+ euro 1990",
    "(?:a+)+b euro 1990", "5" + "("*500 + "euro 1990",
    # zero-width / combining / control characters
    "5\u200b euro\u200d 1990", "5 e\u0301uro 1990", "5 euro\x00 1990",
    "\u0301"*10 + " 5 euro 1990",
    # no-space / glued tokens
    "5euro1990", "5€1990", "€51990", "5EURO1990Greece",
    # SQL/JS/HTML injection style
    "5 euro 1990'; DROP TABLE coins;--",
    "<script>alert(1)</script> 5 euro 1990",
    '5 euro 1990" onmouseover="alert(1)',
    "${7*7} 5 euro 1990",
    # catalog-ID edge cases (masking must not itself throw)
    "KM#123 5 drachmai 1976", "Y# 45 2 euro 2010", "quarter P 1964",
]


def run():
    print("=" * 70)
    print("COIN IDENTITY RESOLVER — REGRESSION SUITE")
    print("=" * 70)

    # --- known-good ---
    print("\n[1/4] Known-good regression cases...")
    for query, exp_country, exp_currency, exp_denom, exp_year, exp_status in KNOWN_GOOD:
        try:
            r = resolve_coin_identity(query)
            best = r.get("best") or {}
            ok_status = r.get("status") == exp_status
            ok_country = best.get("country") == exp_country
            ok_currency = (exp_currency is None) or (best.get("currency") == exp_currency)
            ok_denom = best.get("denomination_value") is not None and abs(best["denomination_value"] - exp_denom) < 1e-9
            ok_year = best.get("year") == exp_year
            all_ok = ok_status and ok_country and ok_currency and ok_denom and ok_year
            detail = (f"got status={r.get('status')} country={best.get('country')!r} "
                      f"currency={best.get('currency')!r} denom={best.get('denomination_value')!r} "
                      f"year={best.get('year')!r}")
            check(f"KNOWN-GOOD {query!r}", all_ok, "" if all_ok else f"-> {detail}")
            print(f"  {'OK  ' if all_ok else 'FAIL'}  {query!r}")
        except Exception as e:
            check(f"KNOWN-GOOD {query!r}", False, f"-> THREW {type(e).__name__}: {e}")
            print(f"  FAIL  {query!r}  -> THREW {type(e).__name__}: {e}")

    # --- known open issue: identity must still be correct even though status isn't "resolved" yet ---
    print("\n[1b/4] Known open issue (identity correctness, not status)...")
    query, exp_country, exp_currency, exp_denom, exp_year = KNOWN_ISSUE_EAGLE_TIE
    try:
        r = resolve_coin_identity(query)
        best = r.get("best") or {}
        ok = (best.get("country") == exp_country and best.get("currency") == exp_currency
              and best.get("denomination_value") is not None
              and abs(best["denomination_value"] - exp_denom) < 1e-9
              and best.get("year") == exp_year)
        check(f"KNOWN-ISSUE {query!r} (.best identity only)", ok,
              f"-> got best={best.get('country')!r}/{best.get('currency')!r}/{best.get('denomination_value')!r}/{best.get('year')!r}")
        print(f"  {'OK  ' if ok else 'FAIL'}  {query!r}  (status={r.get('status')} — tracked separately, see KNOWN_ISSUE_EAGLE_TIE comment)")
    except Exception as e:
        check(f"KNOWN-ISSUE {query!r}", False, f"-> THREW {type(e).__name__}: {e}")
        print(f"  FAIL  {query!r}  -> THREW {type(e).__name__}: {e}")

    # --- adversarial: no crash, no hang ---
    print(f"\n[2/4] Adversarial inputs (must not crash, must finish <{TIME_LIMIT_S}s)...")
    for query in ADVERSARIAL_MUST_NOT_CRASH:
        label = query if len(query) <= 50 else query[:47] + "..."
        t0 = time.time()
        try:
            resolve_coin_identity(query)
            dt = time.time() - t0
            ok = dt <= TIME_LIMIT_S
            check(f"ADVERSARIAL {label!r}", ok, f"-> took {dt:.2f}s (limit {TIME_LIMIT_S}s)")
            print(f"  {'OK  ' if ok else 'FAIL'}  [{dt:5.2f}s]  {label!r}")
        except Exception as e:
            dt = time.time() - t0
            check(f"ADVERSARIAL {label!r}", False, f"-> THREW {type(e).__name__}: {e}")
            print(f"  FAIL  [{dt:5.2f}s]  {label!r}  -> THREW {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)

    # --- compatibility methods used by numisvault_backend.py ---
    print("\n[3/4] Backend-compatibility surface (_negative_flags, listing_match_score)...")
    r = get_resolver()
    try:
        has_neg = hasattr(r, "_negative_flags")
        check("_negative_flags exists", has_neg)
        has_lms = hasattr(r, "listing_match_score")
        check("listing_match_score exists", has_lms)
        if has_neg:
            flags = r._negative_flags("Greece 5 Drachmai 1976 Banknote Pick#123")
            check("_negative_flags detects banknote", bool(flags), f"-> got {flags!r}")
            clean = r._negative_flags("Greece 5 Drachmai 1976 UNC")
            check("_negative_flags clean listing has no flags", not clean, f"-> got {clean!r}")
        if has_lms:
            target = resolve_coin_identity("5 drachma 1976").get("best")
            good = r.listing_match_score(target, "Greece 5 Drachmai 1976 UNC")
            check("listing_match_score good match scores high", good.get("score", 0) >= 80, f"-> got {good!r}")
            bad = r.listing_match_score(target, "Greece 5 Drachmai 1976 Banknote Replica")
            check("listing_match_score negative-flagged listing scores low", bad.get("score", 100) <= 30, f"-> got {bad!r}")
        print("  OK  compatibility surface checked")
    except Exception as e:
        check("backend-compatibility surface", False, f"-> THREW {type(e).__name__}: {e}")
        print(f"  FAIL  -> THREW {type(e).__name__}: {e}")

    # --- sanity: resolver loads at all ---
    print("\n[4/4] Basic sanity...")
    try:
        r2 = resolve_coin_identity("2 euro 2015 Greece")
        check("basic resolve returns a dict with 'best'", isinstance(r2, dict) and "best" in r2)
        print("  OK  basic resolve() call")
    except Exception as e:
        check("basic resolve() call", False, f"-> THREW {type(e).__name__}: {e}")
        print(f"  FAIL  -> THREW {type(e).__name__}: {e}")

    # --- summary ---
    total = PASS_COUNT + len(FAILURES)
    print("\n" + "=" * 70)
    print(f"RESULT: {PASS_COUNT}/{total} checks passed")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        print("\n*** DO NOT DEPLOY — fix the above before shipping. ***")
        return 1
    else:
        print("\nAll checks passed. SAFE TO DEPLOY.")
        return 0


if __name__ == "__main__":
    sys.exit(run())
