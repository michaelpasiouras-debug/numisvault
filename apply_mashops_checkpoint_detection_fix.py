from pathlib import Path

backend = Path('numisvault_backend.py')
text = backend.read_text(encoding='utf-8')

helper = '''def _is_mashops_checkpoint_html(page_text, final_url=""):
    """Detect MA-Shops human-verification / WAF checkpoint pages.

    These pages can return HTTP 200, so status-code checks alone are not
    enough. Treat them as a source-availability failure, never as a parser
    failure or as evidence that no matching coin exists.
    """
    low = str(page_text or "").lower()
    url_low = str(final_url or "").lower()
    markers = (
        "we are verifying that you are a human",
        "verifying that you are human",
        "verify that you are human",
        "checking your browser",
        "human verification",
        "creoline checkpoint",
    )
    return (
        "/_creoline/checkpoint" in url_low
        or ("creoline" in low and "checkpoint" in low)
        or any(marker in low for marker in markers)
    )

'''

if 'def _is_mashops_checkpoint_html(' not in text:
    anchor = 'def fetch_search(query, payload):\n'
    if anchor not in text:
        raise SystemExit('fetch_search anchor not found')
    text = text.replace(anchor, helper + anchor, 1)

old = '''            if "captcha" in r.text.lower() and len(r.text)<200000:
                last_err="MA-Shops returned a CAPTCHA/anti-bot page"
                print(f"[MA-Shops]   -> looks like a CAPTCHA/anti-bot page", flush=True)
                continue
            soup=BeautifulSoup(r.text,"html.parser")
'''
new = '''            # MA-Shops can return a Creoline human-verification checkpoint
            # with HTTP 200. Detect it explicitly before parsing so the UI does
            # not misreport a blocked source as "No exact validated match".
            if _is_mashops_checkpoint_html(r.text, r.url):
                last_err="MA-Shops human-verification checkpoint blocked automated search"
                print(f"[MA-Shops]   -> human-verification/WAF checkpoint detected at {r.url}", flush=True)
                continue
            if "captcha" in r.text.lower() and len(r.text)<200000:
                last_err="MA-Shops returned a CAPTCHA/anti-bot page"
                print(f"[MA-Shops]   -> looks like a CAPTCHA/anti-bot page", flush=True)
                continue
            soup=BeautifulSoup(r.text,"html.parser")
'''
if old in text:
    text = text.replace(old, new, 1)
elif 'human-verification/WAF checkpoint detected' not in text:
    raise SystemExit('MA-Shops checkpoint insertion anchor not found')

backend.write_text(text, encoding='utf-8')

index = Path('index.html')
html = index.read_text(encoding='utf-8')
old_ui = '''   }else{
     if($('rMarketValue')) $('rMarketValue').value='No exact validated match';
     const diag=data.diagnostics||{};
     const rejects=Array.isArray(diag.cheap_rejections)?diag.cheap_rejections:[];
     const reasonCounts={};
'''
new_ui = '''   }else{
     const sourceErrors=Array.isArray(data.errors)?data.errors:[];
     const maBlocked=sourceErrors.some(e=>/human-verification|anti-bot|captcha|checkpoint/i.test(String((e&&e.error)||'')));
     if($('rMarketValue')) $('rMarketValue').value=maBlocked?'MA-Shops temporarily unavailable':'No exact validated match';
     const diag=data.diagnostics||{};
     const rejects=Array.isArray(diag.cheap_rejections)?diag.cheap_rejections:[];
     const reasonCounts={};
'''
if old_ui in html:
    html = html.replace(old_ui, new_ui, 1)
elif "const maBlocked=sourceErrors.some" not in html:
    raise SystemExit('Price Research no-match UI anchor not found')

old_explain = '''     }else explanation='MA-Shops returned no candidate listings for this exact search.';
     $('bestPriceBox').innerHTML=`<strong>No exact validated match.</strong><div class="muted" style="margin-top:7px">${esc(explanation)}</div>`;
'''
new_explain = '''     }else if(maBlocked){
       explanation='MA-Shops returned a human-verification / anti-bot checkpoint instead of search results. CoinBids did not receive any listings to validate, so this is a source-availability problem — not evidence that the coin has no matching offers.';
     }else explanation='MA-Shops returned no candidate listings for this exact search.';
     const noMatchTitle=maBlocked?'MA-Shops temporarily unavailable.':'No exact validated match.';
     $('bestPriceBox').innerHTML=`<strong>${noMatchTitle}</strong><div class="muted" style="margin-top:7px">${esc(explanation)}</div>`;
'''
if old_explain in html:
    html = html.replace(old_explain, new_explain, 1)
elif "const noMatchTitle=maBlocked" not in html:
    raise SystemExit('Price Research explanation UI anchor not found')

index.write_text(html, encoding='utf-8')
print('MA-Shops checkpoint detection and truthful UI handling applied')
