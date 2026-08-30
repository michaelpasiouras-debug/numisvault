from pathlib import Path

p = Path("numisvault_backend.py")
s = p.read_text(encoding="utf-8")

# Correctness fix: never stop collecting query results merely because earlier
# completed queries already produced 80 raw offers. Futures complete out of
# order, so that old optimization could cancel a later/broader/cheapest-first
# query containing the actual cheapest valid listing. That made the final
# global sort mathematically correct over an incomplete candidate set.
old = '''            all_offers.extend(ma_offers)\n            if len(all_offers)>=80:\n                break\n'''
new = '''            all_offers.extend(ma_offers)\n            # IMPORTANT: consume every generated MA-Shops query. Futures finish\n            # out of order, so an early raw-count cutoff can silently discard\n            # the query containing the true cheapest valid listing. Bound each\n            # fetch_search() result instead; never bound correctness globally by\n            # whichever queries happen to finish first.\n'''

if old in s:
    s = s.replace(old, new, 1)
elif "if len(all_offers)>=80:" in s:
    raise SystemExit("unexpected candidate-funnel cutoff shape; refusing blind patch")
elif "IMPORTANT: consume every generated MA-Shops query" not in s:
    raise SystemExit("candidate-funnel target not found")

# The executor comment must also reflect the new correctness invariant.
old_comment = '''        # Any query still queued (not yet started, since only 3 run at once)\n        # is cancelled once we have enough offers — already-in-progress\n        # requests are simply left to finish in the background rather than\n        # forcibly killed mid-request.\n        executor.shutdown(wait=False,cancel_futures=True)\n'''
new_comment = '''        # Correctness requires every generated query to complete: the cheapest\n        # valid listing may live in any query, regardless of completion order.\n        executor.shutdown(wait=True,cancel_futures=False)\n'''
if old_comment in s:
    s = s.replace(old_comment, new_comment, 1)
elif "executor.shutdown(wait=True,cancel_futures=False)" not in s:
    raise SystemExit("executor shutdown target not found")

# Structural assertions: this patch must never regress to completion-order
# truncation, and the QA funnel trace must remain available for diagnosis.
assert "if len(all_offers)>=80:" not in s
assert "executor.shutdown(wait=True,cancel_futures=False)" in s
assert "funnel_trace" in s and "qa_full_evidence" in s

p.write_text(s, encoding="utf-8")
print("candidate-funnel completeness fix applied")
