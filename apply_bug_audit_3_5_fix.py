from pathlib import Path


def replace_once(path, old, new, label):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if new in text:
        print(f"{label}: already applied")
        return
    if old not in text:
        raise SystemExit(f"{label}: expected anchor not found; refusing unsafe patch")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"{label}: applied")


replace_once(
    "coin_identity_resolver.py",
    '        MAX_INPUT_LENGTH=200\n',
    '        MAX_INPUT_LENGTH=500  # Increased to preserve year/theme/variant in long numismatic listings\n',
    "Bug 3 resolver input length",
)

replace_once(
    "auction_matching.py",
    '        if comp.grade_bucket == "DETAILS" and t_grade_bucket != "DETAILS":\n            hard_reject_reasons.append("details/cleaned coin vs straight-grade target")\n',
    '        if comp.grade_bucket == "DETAILS" and t_grade_bucket and t_grade_bucket != "DETAILS":\n            hard_reject_reasons.append("details/cleaned coin vs straight-grade target")\n',
    "Bug 4 details grade guard",
)

replace_once(
    "auction_valuation.py",
    '    older_med = stats.weighted_median(older_prices, [1.0] * len(older_prices))\n    recent_med = stats.weighted_median(recent_prices, [1.0] * len(recent_prices))\n    if not older_med:\n        return {"label": "INSUFFICIENT_DATA", "annualized_change_pct": None, "note": "degenerate older-half median"}\n    change_pct = (recent_med - older_med) / older_med\n',
    '    older_med = stats.weighted_median(older_prices, [1.0] * len(older_prices))\n    recent_med = stats.weighted_median(recent_prices, [1.0] * len(recent_prices))\n\n    # Safety guard: never divide by a missing, zero, or negative older-half median.\n    if older_med is None or older_med <= 0:\n        return {"label": "INSUFFICIENT_DATA", "annualized_change_pct": None, "note": "degenerate or zero older-half median"}\n\n    change_pct = (recent_med - older_med) / older_med\n',
    "Bug 5 trend zero division guard",
)
