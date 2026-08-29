from pathlib import Path

PATH = Path('numisvault_backend.py')
text = PATH.read_text(encoding='utf-8')

MARKER = '# ISSUE-ALIAS SEARCH EXPANSION: query MA-Shops in known issue languages'
if MARKER in text:
    print('Issue-alias search expansion already applied.')
    raise SystemExit(0)

anchor = '''    qs = []\n    # Exact user wording first. It is usually the highest-information query\n'''
if anchor not in text:
    raise SystemExit('Expected make_queries() anchor not found; refusing unsafe patch.')

insert = '''    # ISSUE-ALIAS SEARCH EXPANSION: query MA-Shops in known issue languages\n    # Filtering already understands multilingual aliases, but MA-Shops search itself\n    # is language-sensitive. Without expanding the outgoing query, a cheaper German/\n    # French/Italian listing may never reach passes_hard_filter() at all.\n    issue_search_queries=[]\n    theme_for_issue=str(coin.get("theme") or "").strip()\n    if RESOLVER_AVAILABLE and theme_for_issue:\n        try:\n            issues=(get_resolver().issue_db or {}).get("issues") or []\n            country_n=norm(country)\n            issue_code=next((c for name,c in _ISSUE_COUNTRY_NAME_TO_CODE.items() if name in country_n),None)\n            dm=re.search(r"(\\d+(?:\\.\\d+)?)",str(denom or ""))\n            denom_val=float(dm.group(1)) if dm else None\n            try:\n                year_val=int(year) if year else None\n            except Exception:\n                year_val=None\n            issue_candidates=[iss for iss in issues\n                if (not issue_code or iss.get("country_code")==issue_code)\n                and (denom_val is None or iss.get("denomination_value")==denom_val)\n                and (year_val is None or iss.get("year")==year_val)\n                and iss.get("aliases")]\n            theme_n=norm(theme_for_issue)\n            selected_issue=None\n            for iss in issue_candidates:\n                pool=[iss.get("canonical_title","")]+list(iss.get("aliases") or [])\n                if any(p and (theme_n in norm(p) or norm(p) in theme_n or theme_word_matches_title(theme_for_issue,p)) for p in pool):\n                    selected_issue=iss\n                    break\n            if selected_issue:\n                raw_n=norm(raw)\n                seen_alias=set()\n                for alias in selected_issue.get("aliases") or []:\n                    alias=str(alias or "").strip()\n                    an=norm(alias)\n                    if not alias or not an or an in seen_alias:\n                        continue\n                    seen_alias.add(an)\n                    if an in raw_n:\n                        continue\n                    q=" ".join(x for x in [denom,year,alias] if x)\n                    if q:\n                        issue_search_queries.append(q)\n                    if len(issue_search_queries)>=3:\n                        break\n        except Exception as e:\n            print(f"[issue-query] alias expansion skipped: {type(e).__name__}: {e}",flush=True)\n\n    qs = []\n    # Exact user wording first. It is usually the highest-information query\n'''

text = text.replace(anchor, insert, 1)

anchor2 = '''    if raw: qs.append(raw)\n    qs.extend(resolver_queries)\n'''
replace2 = '''    if raw: qs.append(raw)\n    # Put issue-specific multilingual aliases ahead of broad resolver/core queries\n    # so cheap foreign-language listings are discovered before the 5-query cap.\n    qs.extend(issue_search_queries)\n    qs.extend(resolver_queries)\n'''
if anchor2 not in text:
    raise SystemExit('Expected query-order anchor not found; refusing unsafe patch.')
text = text.replace(anchor2, replace2, 1)

PATH.write_text(text, encoding='utf-8')
print('Applied multilingual issue-alias search expansion.')
