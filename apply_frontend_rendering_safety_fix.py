from pathlib import Path

PATH = Path("index.html")
text = PATH.read_text(encoding="utf-8")

old_money = "function money(v,c='EUR'){if(v===''||v==null||isNaN(Number(v)))return '—';try{return new Intl.NumberFormat('el-GR',{style:'currency',currency:c||'EUR'}).format(Number(v))}catch{return `${v} ${c}`}}"
new_money = """function money(v,c='EUR'){
  // Never coerce missing/structural values (false, [], {}, whitespace) into 0.00.
  if(v==null || typeof v==='boolean' || typeof v==='object' || (typeof v==='string' && v.trim()==='')) return '—';
  const num=Number(v);
  if(!Number.isFinite(num)) return '—';
  const cur=(typeof c==='string' && c.trim())?c.trim().toUpperCase():'EUR';
  try{return new Intl.NumberFormat('el-GR',{style:'currency',currency:cur}).format(num)}
  catch{return `${num.toFixed(2)} ${cur}`}
}"""
if old_money in text:
    text = text.replace(old_money, new_money, 1)
elif new_money not in text:
    raise SystemExit("money() anchor not found; refusing unsafe patch")

old_head = """function renderAuctionV3Panel(v3,mode,currency){
 if(!v3||!v3.snapshot)return '';
 const s=v3.snapshot,rm=s.realized_market||{},fus=s.fusion||{},conf=s.confidence||{},dem=s.demand||{},tr=s.trend||{};
 const m=x=>money(x,currency);
 const fxConverted=(s.comparables_evidence||[]).filter(c=>c.original_currency&&c.original_currency!==currency&&c.normalized_price!=null);
 const fxNote=fxConverted.length?`<div class=\"muted\" style=\"margin-top:4px\">FX: ${fxConverted.length} comparable(s) converted to ${esc(currency)} using ECB reference rates (Frankfurter API) — original amounts preserved in the evidence data.</div>`:'';
 const confColor=conf.score>=85?'#4C7A4C':conf.score>=65?'#3F6E93':conf.score>=40?'#B88E3A':'#A23B2E';
 const confBar=`<span class=\"v3-conf-bar\"><span class=\"v3-conf-bar-fill\" style=\"width:${Math.max(4,conf.score||0)}%;background:${confColor}\"></span></span>`;
"""
new_head = """function renderAuctionV3Panel(v3,mode,currency){
 if(!v3||!v3.snapshot)return '';
 const s=v3.snapshot,rm=s.realized_market||{},fus=s.fusion||{},conf=s.confidence||{},dem=s.demand||{},tr=s.trend||{};
 const m=x=>money(x,currency);
 const evidence=Array.isArray(s.comparables_evidence)?s.comparables_evidence:[];
 const fxConverted=evidence.filter(c=>c&&c.original_currency&&c.original_currency!==currency&&c.normalized_price!=null);
 const fxFallback=fxConverted.filter(c=>String(c.fx_confidence||'').toLowerCase()==='current_fallback');
 const fxNote=fxConverted.length
   ?`<div class=\"${fxFallback.length?'warn':'muted'}\" style=\"margin-top:4px\">FX: ${fxConverted.length} comparable(s) converted to ${esc(currency)} using ECB reference rates (Frankfurter API).${fxFallback.length?` ${fxFallback.length} conversion(s) used a current-rate fallback rather than the historical auction-date rate.`:''} Original amounts are preserved in the evidence data.</div>`
   :'';
 const confNum=Number(conf.score);
 const confScore=Number.isFinite(confNum)?Math.max(0,Math.min(100,confNum)):null;
 const confColor=confScore==null?'#A23B2E':confScore>=85?'#4C7A4C':confScore>=65?'#3F6E93':confScore>=40?'#B88E3A':'#A23B2E';
 const confBar=confScore==null?'':`<span class=\"v3-conf-bar\"><span class=\"v3-conf-bar-fill\" style=\"width:${Math.max(4,confScore)}%;background:${confColor}\"></span></span>`;
 const snapshotStatus=String(s.status||rm.status||'').trim().toUpperCase();
 const realizedCount=Number(rm.count);
 const insufficientRealized=snapshotStatus==='INSUFFICIENT_DATA' || !Number.isFinite(realizedCount) || realizedCount<=0;
"""
if old_head in text:
    text = text.replace(old_head, new_head, 1)
elif new_head not in text:
    raise SystemExit("renderAuctionV3Panel() header anchor not found; refusing unsafe patch")

old_comps = "const comps=(s.comparables_evidence||[]).slice().sort((a,b)=>(b.identity_match_score||0)-(a.identity_match_score||0));"
new_comps = "const comps=evidence.slice().sort((a,b)=>(Number(b?.identity_match_score)||0)-(Number(a?.identity_match_score)||0));"
if old_comps in text:
    text = text.replace(old_comps, new_comps, 1)
elif new_comps not in text:
    raise SystemExit("comparables anchor not found; refusing unsafe patch")

old_return = """ let verdictHtml='';
 return `<details style=\"margin-top:12px\" open><summary style=\"cursor:pointer;color:#8fd0c9\"><b>⚡ Realized-sale evidence</b></summary><div style=\"margin-top:8px;font-size:13px\">`+
   `<div><b>Realized market:</b> ${rm.count||0} matched sale(s) (${rm.exact_count||0} exact, ${rm.strong_count||0} strong, ${rm.supporting_count||0} supporting) across ${rm.distinct_auction_houses||0} house(s). ${esc(rm.sample_note||'')}</div>`+
   (rm.count?`<div style=\"margin-top:4px\"><b>Weighted median:</b> ${m(rm.weighted_median)} · <b>P25–P75:</b> ${m(rm.p25)} – ${m(rm.p75)} · <b>Dispersion:</b> ${esc(rm.dispersion_label||'—')}</div>`:'')+
   fxNote+
   `<div style=\"margin-top:4px\"><b>Confidence:</b> ${esc(conf.label||'—')} (${conf.score||0}/100)${confBar}</div>`+
"""
new_return = """ let verdictHtml='';
 if(insufficientRealized){
   return `<details style=\"margin-top:12px\" open><summary style=\"cursor:pointer;color:#8fd0c9\"><b>⚡ Realized-sale evidence</b></summary><div style=\"margin-top:8px;font-size:13px\">`+
     `<div class=\"notice\"><strong>Insufficient realized-sale data.</strong> Auction KPIs such as weighted median, P25/P75 and confidence are not shown as numeric values until validated comparable sales are available.${rm.sample_note?` <span class=\"muted\">${esc(rm.sample_note)}</span>`:''}</div>`+
     fxNote+compTable+`</div></details>`;
 }
 return `<details style=\"margin-top:12px\" open><summary style=\"cursor:pointer;color:#8fd0c9\"><b>⚡ Realized-sale evidence</b></summary><div style=\"margin-top:8px;font-size:13px\">`+
   `<div><b>Realized market:</b> ${realizedCount} matched sale(s) (${rm.exact_count||0} exact, ${rm.strong_count||0} strong, ${rm.supporting_count||0} supporting) across ${rm.distinct_auction_houses||0} house(s). ${esc(rm.sample_note||'')}</div>`+
   `<div style=\"margin-top:4px\"><b>Weighted median:</b> ${m(rm.weighted_median)} · <b>P25–P75:</b> ${m(rm.p25)} – ${m(rm.p75)} · <b>Dispersion:</b> ${esc(rm.dispersion_label||'—')}</div>`+
   fxNote+
   `<div style=\"margin-top:4px\"><b>Confidence:</b> ${esc(conf.label||'—')} (${confScore==null?'—':confScore}/100)${confBar}</div>`+
"""
if old_return in text:
    text = text.replace(old_return, new_return, 1)
elif new_return not in text:
    raise SystemExit("Auction KPI render anchor not found; refusing unsafe patch")

# Regression assertions: these are intentionally structural and idempotent.
required = [
    "typeof v==='boolean'",
    "typeof v==='object'",
    "Number.isFinite(num)",
    "fx_confidence",
    "current_fallback",
    "insufficientRealized",
    "Insufficient realized-sale data.",
    "confScore==null?'—':confScore",
]
missing = [needle for needle in required if needle not in text]
if missing:
    raise SystemExit(f"frontend safety patch incomplete; missing: {missing}")

PATH.write_text(text, encoding="utf-8")
print("Frontend rendering safety fixes verified/applied to index.html")
