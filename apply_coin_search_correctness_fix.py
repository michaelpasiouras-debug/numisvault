from pathlib import Path

p=Path('numisvault_backend.py')
s=p.read_text(encoding='utf-8')

old='''        if _raw_for_target:
            try:
                target_identity=resolve_coin_identity(_raw_for_target).get("best")
            except Exception as e:
'''
new='''        if _raw_for_target:
            try:
                target_identity=resolve_coin_identity(_raw_for_target).get("best")
                # Canonical resolver output must feed the hard filter even when
                # the client supplied only coin.raw.
                if target_identity:
                    _coin_for_target.setdefault("country", target_identity.get("country"))
                    _coin_for_target.setdefault("year", target_identity.get("year"))
                    _resolved_denom=target_identity.get("denomination_value")
                    if _resolved_denom is not None and not (_coin_for_target.get("denom") or _coin_for_target.get("denomination")):
                        _coin_for_target["denom"]=_resolved_denom
                    _resolved_currency=target_identity.get("currency") or target_identity.get("denomination_currency")
                    if _resolved_currency and not _coin_for_target.get("currency"):
                        _coin_for_target["currency"]=_resolved_currency
                    payload["coin"]=_coin_for_target
            except Exception as e:
'''
if old not in s: raise SystemExit('resolver enrichment block not found')
s=s.replace(old,new,1)
old2='''    top=valid[:max(1,min(int(payload.get("limit") or 2),2))]
'''
new2='''    # Normal UI remains top-2; QA can explicitly request all validated
    # evidence so the global cheapest delivered offer is independently provable.
    _requested_limit=int(payload.get("limit") or 2)
    _max_public_limit=200 if payload.get("qa_full_evidence") else 2
    top=valid[:max(1,min(_requested_limit,_max_public_limit))]
'''
if old2 not in s: raise SystemExit('top limit block not found')
s=s.replace(old2,new2,1)
old3='''        "ship_to_country":ship_to_country,
        "cache":"miss"
'''
new3='''        "ship_to_country":ship_to_country,
        "cheapest_known_delivered":public_offer(next((o for o in valid if o.get("total") is not None), valid[0])) if valid else None,
        "cache":"miss"
'''
if old3 not in s: raise SystemExit('result tail block not found')
s=s.replace(old3,new3,1)
p.write_text(s,encoding='utf-8')
print('coin-search correctness patch applied')
