#!/usr/bin/env python3
"""Idempotently add same-year commemorative mutual exclusion to theme gate."""
from pathlib import Path

PATH = Path(__file__).resolve().parent / "numisvault_backend.py"
text = PATH.read_text(encoding="utf-8")

old = '''    if not matched:\n        return True\n    return theme_word_matches_title(theme_raw,title) or any(\n        norm(al) in norm(title) for al in (matched.get("aliases") or []) if al)\n'''

new = '''    if not matched:\n        return True\n\n    # Mutual exclusion for same-country / same-denomination / same-year\n    # commemoratives. Once the query identifies one seeded issue, a listing\n    # that explicitly names a DIFFERENT seeded issue from the same candidate\n    # set must be rejected before positive/fuzzy theme matching. This prevents\n    # one commemorative from contaminating another issue's market evidence.\n    title_norm=norm(title)\n    matched_title=norm(matched.get("canonical_title") or "")\n    for other_iss in candidates:\n        if other_iss is matched:\n            continue\n        other_title=norm(other_iss.get("canonical_title") or "")\n        if matched_title and other_title and other_title==matched_title:\n            continue\n        other_pool=[other_iss.get("canonical_title","")]+list(other_iss.get("aliases") or [])\n        if any(al and norm(al) and norm(al) in title_norm for al in other_pool):\n            print(f"[Theme Gate] REJECTED (Wrong Commemorative): "\n                  f"listing names another same-year issue. Title: {title!r}")\n            return False\n\n    return theme_word_matches_title(theme_raw,title) or any(\n        norm(al) in title_norm for al in (matched.get("aliases") or []) if al)\n'''

if new in text:
    print("Theme mutual-exclusion fix already present.")
elif old in text:
    PATH.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("Applied theme mutual-exclusion fix.")
else:
    raise SystemExit("Expected _theme_issue_gate tail not found; refusing unsafe patch")
