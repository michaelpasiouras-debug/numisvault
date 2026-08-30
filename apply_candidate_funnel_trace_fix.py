from pathlib import Path

path = Path('numisvault_backend.py')
text = path.read_text(encoding='utf-8')

# Idempotent: if the final response contract already exposes funnel_trace, the
# production backend has already absorbed this patch.
if '"funnel_trace":funnel_trace if trace_enabled else None' in text:
    print('candidate funnel trace already applied')
    raise SystemExit(0)

old = '''    queries=make_queries(payload);all_offers=[];errors=[];used=[]
'''
new = '''    queries=make_queries(payload);all_offers=[];errors=[];used=[]
    # QA-only candidate-funnel trace. Normal Price Research requests pay only
    # a couple of boolean/list initializations; detailed evidence is collected
    # exclusively when qa_full_evidence=true. This is diagnostic metadata only:
    # it does not participate in filtering, pricing, shipping, ranking or cache
    # selection.
    trace_enabled=bool(payload.get("qa_full_evidence"))
    query_trace=[]
    candidate_trace=[]
'''
if old not in text:
    raise SystemExit('coin_search query initialization anchor not found')
text = text.replace(old, new, 1)

old = '''            if ma_url:used.append({"query":q,"source":"MA-Shops","url":ma_url})
            if ma_err:errors.append({"query":q,"source":"MA-Shops","error":ma_err})
            all_offers.extend(ma_offers)
            if len(all_offers)>=80:
'''
new = '''            if ma_url:used.append({"query":q,"source":"MA-Shops","url":ma_url})
            if ma_err:errors.append({"query":q,"source":"MA-Shops","error":ma_err})
            if trace_enabled:
                query_trace.append({
                    "query":q,
                    "raw_hits":len(ma_offers),
                    "search_url":ma_url,
                    "error":ma_err,
                    "listings":[{
                        "title":o.get("title"),"url":o.get("url"),
                        "raw_price":o.get("price"),"currency":o.get("currency"),
                        "raw_shipping":o.get("shipping"),
                        "shipping_status":o.get("shipping_status"),
                    } for o in ma_offers[:100]],
                })
            for o in ma_offers:
                if trace_enabled:
                    o.setdefault("_source_queries",[])
                    if q not in o["_source_queries"]:
                        o["_source_queries"].append(q)
            all_offers.extend(ma_offers)
            if len(all_offers)>=80:
'''
if old not in text:
    raise SystemExit('MA-Shops result collection anchor not found')
text = text.replace(old, new, 1)

old = '''    for o in all_offers:
        key=o.get("url") or (o.get("title"),o.get("price"))
        if key not in by or o.get("_score",0)>by[key].get("_score",0):by[key]=o
'''
new = '''    for o in all_offers:
        key=o.get("url") or (o.get("title"),o.get("price"))
        if key not in by:
            by[key]=o
        else:
            # Preserve query provenance across duplicate hits. Which copy wins
            # still follows the existing score rule; provenance is QA metadata.
            merged_queries=list(dict.fromkeys((by[key].get("_source_queries") or []) + (o.get("_source_queries") or [])))
            if o.get("_score",0)>by[key].get("_score",0):
                by[key]=o
            if trace_enabled:
                by[key]["_source_queries"]=merged_queries
'''
if old not in text:
    raise SystemExit('dedupe anchor not found')
text = text.replace(old, new, 1)

old = '''        if asset in ("BANKNOTE","OTHER"):rejected["asset"]+=1;continue
        if product_scope(match_text)!="SINGLE_COIN":rejected["scope"]+=1;continue
        if not passes_hard_filter(match_text,payload):
            rejected["identity"]+=1
            if _reject_log_budget[0]>0:
                _reject_log_budget[0]-=1
                print(f"[coin-search] reject identity reason={_why_rejected(match_text,payload)} title={match_text!r}",flush=True)
            continue
'''
new = '''        if asset in ("BANKNOTE","OTHER"):
            rejected["asset"]+=1
            if trace_enabled:
                candidate_trace.append({"status":"REJECTED","reason":"ASSET_"+asset,
                    "title":o.get("title"),"match_text":match_text,"url":o.get("url"),
                    "queries":o.get("_source_queries") or [],"raw_price":o.get("price"),
                    "currency":o.get("currency"),"raw_shipping":o.get("shipping")})
            continue
        if product_scope(match_text)!="SINGLE_COIN":
            rejected["scope"]+=1
            if trace_enabled:
                candidate_trace.append({"status":"REJECTED","reason":"NOT_SINGLE_COIN",
                    "title":o.get("title"),"match_text":match_text,"url":o.get("url"),
                    "queries":o.get("_source_queries") or [],"raw_price":o.get("price"),
                    "currency":o.get("currency"),"raw_shipping":o.get("shipping")})
            continue
        if not passes_hard_filter(match_text,payload):
            rejected["identity"]+=1
            _reject_reason=_why_rejected(match_text,payload)
            if trace_enabled:
                candidate_trace.append({"status":"REJECTED","reason":_reject_reason,
                    "title":o.get("title"),"match_text":match_text,"url":o.get("url"),
                    "queries":o.get("_source_queries") or [],"raw_price":o.get("price"),
                    "currency":o.get("currency"),"raw_shipping":o.get("shipping")})
            if _reject_log_budget[0]>0:
                _reject_log_budget[0]-=1
                print(f"[coin-search] reject identity reason={_reject_reason} title={match_text!r}",flush=True)
            continue
'''
if old not in text:
    raise SystemExit('filter rejection anchor not found')
text = text.replace(old, new, 1)

old = '''        valid.append(o)
    print(f"[coin-search] candidates raw={raw_count} unique={unique_count} valid={len(valid)} rejected={rejected}", flush=True)
'''
new = '''        valid.append(o)
        if trace_enabled:
            candidate_trace.append({"status":"ACCEPTED","reason":"HARD_FILTER_PASS",
                "title":o.get("title"),"match_text":match_text,"url":o.get("url"),
                "queries":o.get("_source_queries") or [],"raw_price":o.get("price"),
                "currency":o.get("currency"),"raw_shipping":o.get("shipping")})
    print(f"[coin-search] candidates raw={raw_count} unique={unique_count} valid={len(valid)} rejected={rejected}", flush=True)
'''
if old not in text:
    raise SystemExit('valid append anchor not found')
text = text.replace(old, new, 1)

old = '''    valid.sort(
        key=lambda o:(
            o.get("total") is None,
            o.get("total") if o.get("total") is not None else float("inf"),
            o.get("price") if o.get("price") is not None else float("inf"),
            -o.get("_score",0)
        )
    )

    # Normal UI remains top-2; QA can explicitly request all validated
'''
new = '''    valid.sort(
        key=lambda o:(
            o.get("total") is None,
            o.get("total") if o.get("total") is not None else float("inf"),
            o.get("price") if o.get("price") is not None else float("inf"),
            -o.get("_score",0)
        )
    )

    funnel_trace=None
    if trace_enabled:
        final_by_url={}
        for rank,o in enumerate(valid,1):
            k=o.get("url") or str((o.get("title"),o.get("price")))
            final_by_url[k]={
                "final_rank":rank,"normalized_price":o.get("price"),
                "normalized_currency":o.get("currency"),"shipping":o.get("shipping"),
                "shipping_status":o.get("shipping_status"),"delivered_total":o.get("total"),
                "score":o.get("_score"),
            }
        for row in candidate_trace:
            if row.get("status")!="ACCEPTED":
                continue
            k=row.get("url") or str((row.get("title"),row.get("raw_price")))
            row.update(final_by_url.get(k,{}))
        funnel_trace={
            "generated_queries":queries,
            "per_query":query_trace,
            "candidates":candidate_trace,
            "winner":({
                "title":valid[0].get("title"),"url":valid[0].get("url"),
                "price":valid[0].get("price"),"shipping":valid[0].get("shipping"),
                "shipping_status":valid[0].get("shipping_status"),"total":valid[0].get("total"),
                "currency":valid[0].get("currency"),
            } if valid else None),
        }

    # Normal UI remains top-2; QA can explicitly request all validated
'''
if old not in text:
    raise SystemExit('final sort anchor not found')
text = text.replace(old, new, 1)

old = '''        "ship_to_country":ship_to_country,
        "cheapest_known_delivered":public_offer(next((o for o in valid if o.get("total") is not None), valid[0])) if valid else None,
        "cache":"miss"
'''
new = '''        "ship_to_country":ship_to_country,
        "cheapest_known_delivered":public_offer(next((o for o in valid if o.get("total") is not None), valid[0])) if valid else None,
        "funnel_trace":funnel_trace if trace_enabled else None,
        "cache":"miss"
'''
if old not in text:
    raise SystemExit('result response anchor not found')
text = text.replace(old, new, 1)

# Do not leak private tracing helpers into the normal public offer projection.
old = '''        d.pop("_match_text",None)
        return d
'''
new = '''        d.pop("_match_text",None)
        d.pop("_source_queries",None)
        return d
'''
if old not in text:
    raise SystemExit('public_offer anchor not found')
text = text.replace(old, new, 1)

path.write_text(text,encoding='utf-8')
print('candidate funnel QA tracing applied')
