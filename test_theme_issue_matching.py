#!/usr/bin/env python3
"""
COINBIDS — MULTILINGUAL ISSUE/THEME MATCHING REGRESSION SUITE
================================================================
Covers the fix for: "Greece 10 euros 2022 mechanism" wrongly requiring the
literal English word "mechanism" in every MA-Shops listing title, rejecting
genuine matches worded in other languages ("Antikythera-Mechanismus",
"Mécanisme d'Anticythère", or simply "Antikythera" alone) — and, as a
result, also silently starving the metal-spec consensus of listings (no
weight/fineness data found), since consensus only runs over surviving
"valid" candidates.

Architecture tested: canonical coin issue (coin_issue_database.json,
"issues" list, new "aliases" field) -> multilingual alias matching
(_theme_issue_gate in numisvault_backend.py) -> dealer title matching.
This is DELIBERATELY separate from variant_matches()/variant_tokens(),
which stay English-literal and strict on purpose for controlled condition/
type categories (proof/UNC/commemorative/error/...) — this suite also
asserts those are untouched.

Run: python3 test_theme_issue_matching.py
Exit code 0 -> safe to deploy. Exit code 1 -> DO NOT DEPLOY.
"""
import sys
import importlib.util

FAILURES = []
PASS_COUNT = 0


def check(label, condition, detail=""):
    global PASS_COUNT
    if condition:
        PASS_COUNT += 1
    else:
        FAILURES.append(f"{label}  {detail}")


def load_backend(path="numisvault_backend.py"):
    spec = importlib.util.spec_from_file_location("coinbids_backend_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    print("=" * 70)
    print("MULTILINGUAL ISSUE/THEME MATCHING — REGRESSION SUITE")
    print("=" * 70)
    backend = load_backend()

    # ------------------------------------------------------------------
    # 1. variant_matches() / variant_tokens() must be UNCHANGED — this
    #    feature must never make the controlled-category hard filter more
    #    permissive.
    # ------------------------------------------------------------------
    print("\n[1/4] variant_matches()/variant_tokens() untouched, still strict...")
    # "proof" is itself in variant_tokens()'s stopword set (pre-existing,
    # unrelated to this fix — grade-like words are handled via the separate
    # grade/grade_conflicts mechanism instead), so it's not a meaningful
    # word to test strictness with. "colorized" is not stopworded and
    # exercises the same code path.
    check("variant_matches requires literal 'colorized' for a colorized request",
          backend.variant_matches("colorized", "Greece 5 Drachmai 1976 UNC") is False)
    check("variant_matches accepts literal 'colorized' match",
          backend.variant_matches("colorized", "Greece 5 Drachmai 1976 Colorized") is True)
    check("variant_tokens still strips stopwords/short tokens",
          backend.variant_tokens("commemorative euro 5") == ["5"] or
          backend.variant_tokens("commemorative euro 5") == [])
    print("  OK  (see FAILURES below if any)")

    # ------------------------------------------------------------------
    # 2. _theme_issue_gate() — the new, isolated multilingual issue gate.
    #    country/denom/year kept in ENGLISH here specifically to isolate
    #    THIS fix from two separate, PRE-EXISTING, out-of-scope gaps found
    #    while building this suite (NOT fixed here, NOT this feature's
    #    responsibility — see NOTE at the bottom of this file):
    #      - country_in_title() has no Italian "Grecia" alias for Greece
    #      - denomination_matches() does not recognize Greek-script "Ευρώ"
    # ------------------------------------------------------------------
    print("\n[2/4] Multilingual Antikythera Mechanism issue matching...")
    coin = {"country": "Greece", "denom": "10 euro", "year": "2022", "theme": "mechanism", "variant": ""}
    payload = {"coin": coin}

    MUST_MATCH = [
        ("Greece 10 Euro 2022 Antikythera Mechanism Silver Proof", "EN — literal theme word (old behavior, must still work)"),
        ("Griechenland 10 Euro 2022 Antikythera-Mechanismus Silber PP", "DE — Antikythera-Mechanismus"),
        ("Grece 10 Euro 2022 Mecanisme dAnticythere Argent", "FR — Mecanisme d'Anticythere"),
        ("Greece 10 Euro 2022 Meccanismo di Anticitera Argento", "IT — Meccanismo di Anticitera"),
        ("Greece 10 Euro 2022 \u039c\u03b7\u03c7\u03b1\u03bd\u03b9\u03c3\u03bc\u03cc\u03c2 \u03c4\u03c9\u03bd \u0391\u03bd\u03c4\u03b9\u03ba\u03c5\u03b8\u03ae\u03c1\u03c9\u03bd \u0391\u03c3\u03ae\u03bc\u03b9", "GR — \u039c\u03b7\u03c7\u03b1\u03bd\u03b9\u03c3\u03bc\u03cc\u03c2 \u03c4\u03c9\u03bd \u0391\u03bd\u03c4\u03b9\u03ba\u03c5\u03b8\u03ae\u03c1\u03c9\u03bd"),
        ("Greece 10 Euro 2022 Antikythera Silver Coin Box COA", "bare 'Antikythera', no 'mechanism' word at all"),
    ]
    MUST_REJECT = [
        ("Greece 10 Euro 2022 Olympic Games Athens Silver Proof", "a DIFFERENT Greece 10-euro 2022 issue"),
        ("Greece 10 Euro 2022 Some Random Commemorative Silver", "a DIFFERENT Greece 10-euro 2022 issue, no theme overlap"),
    ]

    for title, label in MUST_MATCH:
        got = backend.passes_hard_filter(title, payload)
        check(f"MATCH {label!r}", got is True, f"-> passes_hard_filter returned {got}, title={title!r}")
        print(f"  {'OK  ' if got else 'FAIL'}  {label}")

    for title, label in MUST_REJECT:
        got = backend.passes_hard_filter(title, payload)
        check(f"REJECT {label!r}", got is False, f"-> passes_hard_filter returned {got}, title={title!r}")
        print(f"  {'OK  ' if not got else 'FAIL'}  {label}")

    # ------------------------------------------------------------------
    # 3. No leftover theme / issue without aliases -> gate must stay OUT
    #    OF THE WAY (no new false rejections for coins this feature does
    #    not apply to).
    # ------------------------------------------------------------------
    print("\n[3/5] Gate stays inert when there's no specific theme or no alias data...")
    no_theme_coin = {"country": "Greece", "denom": "5 drachma", "year": "1901", "theme": "", "variant": ""}
    check("empty theme -> gate does not block a normal, unrelated coin",
          backend.passes_hard_filter("Greece 5 Drachmai 1901", {"coin": no_theme_coin}) is True)

    unrelated_theme_coin = {"country": "Greece", "denom": "5 drachma", "year": "1901", "theme": "mechanism", "variant": ""}
    check("theme present but no matching issue record for this country+denom+year -> gate does not block",
          backend.passes_hard_filter("Greece 5 Drachmai 1901 UNC", {"coin": unrelated_theme_coin}) is True)
    print("  OK  (see FAILURES below if any)")

    # ------------------------------------------------------------------
    # 4. GENERIC multilingual theme matching — works for ANY coin/word,
    #    not just the seeded Antikythera Mechanism issue. Covers both the
    #    dictionary path (THEME_WORD_TRANSLATIONS: anniversary/battle/
    #    olympics, none of which were part of the original fix) and the
    #    fuzzy/transliteration path for proper nouns not in any dictionary
    #    (a Greek-script place name vs. its Latin spelling).
    # ------------------------------------------------------------------
    print("\n[4/5] GENERIC multilingual theme matching (any word, any coin)...")
    generic_tests = [
        ("anniversary", "Germany 2 Euro 2021 30 Jahre Mauerfall Jubil\u00e4um PP", True, "DE dictionary: Jubil\u00e4um"),
        ("anniversary", "France 2 Euro 2020 30e Anniversaire Erasmus", True, "FR dictionary: Anniversaire"),
        ("anniversary", "Belgium 2 Euro 2022 Random unrelated design", False, "no match -> low score"),
        ("battle", "Austria 2 Euro Schlacht bei Austerlitz Commemorative", True, "DE dictionary: Schlacht"),
        ("battle", "Italy 2 Euro Battaglia di Legnano 2026", True, "IT dictionary: Battaglia"),
        ("olympics", "France 2 Euro 2024 Jeux Olympiques Paris", True, "FR dictionary: Jeux Olympiques"),
        ("acropolis", "Greece 2 Euro \u0391\u03ba\u03c1\u03cc\u03c0\u03bf\u03bb\u03b7 \u0391\u03b8\u03b7\u03bd\u03ce\u03bd Silver", True, "fuzzy+transliteration, proper noun NOT in any dictionary"),
        ("acropolis", "Greece 2 Euro Random other design Silver", False, "no match -> low score"),
    ]
    for theme, title, expect_match, label in generic_tests:
        score = backend.theme_match_score(theme, title)
        got_match = score >= 0.5
        check(f"theme_match_score {label}", got_match == expect_match,
              f"-> score={score:.2f}, theme={theme!r}, title={title!r}")
        print(f"  {'OK  ' if got_match == expect_match else 'FAIL'}  score={score:.2f}  {label}")

    # ------------------------------------------------------------------
    # 5. coin_issue_database.json sanity — new record present, well-formed.
    # ------------------------------------------------------------------
    print("\n[5/5] coin_issue_database.json — new issue record present...")
    try:
        issues = (backend.get_resolver().issue_db or {}).get("issues") or []
        match = next((i for i in issues if i.get("country_code") == "GR"
                      and i.get("denomination_value") == 10 and i.get("year") == 2022), None)
        check("Antikythera Mechanism issue record exists", match is not None)
        check("issue record has a non-empty aliases list", bool(match and match.get("aliases")))
        check("issue record aliases include 'antikythera'", bool(match and any("antikythera" in a.lower() for a in match.get("aliases", []))))
        print("  OK  (see FAILURES below if any)")
    except Exception as e:
        check("coin_issue_database.json sanity", False, f"-> THREW {type(e).__name__}: {e}")
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
    print("\nAll checks passed. SAFE TO DEPLOY.")
    print("\nNOTE — two PRE-EXISTING, OUT-OF-SCOPE gaps found while building this")
    print("suite (NOT fixed here, flagged for a separate, explicit decision):")
    print("  - country_in_title(): no Italian 'Grecia' alias registered for Greece")
    print("  - denomination_matches(): does not recognize Greek-script 'Ευρώ'")
    return 0


if __name__ == "__main__":
    sys.exit(run())
