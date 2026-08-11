from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, re, html as ihtml, urllib.parse, time, os
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

app = Flask(__name__)
CORS(app)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9,de;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Not)A;Brand";v="99", "Google Chrome";v="128", "Chromium";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
})

# eBay's bot-detection (Akamai) is noticeably stricter than MA-Shops' — a bare
# request without an established browsing session tends to get an immediate
# 403 "access denied" interstitial. Visiting the homepage first to pick up
# normal session/anti-bot cookies before hitting the search page materially
# improves the odds of getting real results instead of a block page.
EBAY_WARMED_UP = False
def warm_up_ebay():
    global EBAY_WARMED_UP
    if EBAY_WARMED_UP:
        return
    try:
        SESSION.get("https://www.ebay.com/", timeout=15, headers={"Referer": "https://www.google.com/"})
        EBAY_WARMED_UP = True
    except Exception as e:
        print(f"[eBay] warm-up request failed: {e}", flush=True)

PRICE_PATTERNS = [
    re.compile(r"(?:EUR\b|€)\s*([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)", re.I),
    re.compile(r"([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)\s*(?:EUR\b|€)", re.I),
    re.compile(r"(?:US\$|\$|\bUSD\b)\s*([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)", re.I),
    re.compile(r"([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)\s*(?:US\$|\$|\bUSD\b)", re.I),
    re.compile(r"(?:\bGBP\b|£)\s*([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)", re.I),
    re.compile(r"([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)\s*(?:\bGBP\b|£)", re.I),
    re.compile(r"\bCHF\b\s*([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)", re.I),
    re.compile(r"([0-9]{1,6}(?:[.,][0-9]{1,3})*(?:[.,][0-9]{1,2})?)\s*\bCHF\b", re.I),
]

def detect_currency(text):
    t = text.upper()
    if "EUR" in t or "€" in text: return "EUR"
    if "US$" in t or "USD" in t or "$" in text: return "USD"
    if "GBP" in t or "£" in text: return "GBP"
    if "CHF" in t: return "CHF"
    return "EUR"

def num(s):
    if s is None: return None
    s = str(s).strip().replace("\xa0"," ").replace(" ","")
    # Handle European decimal comma; avoid turning 1.234,56 incorrectly.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".","").replace(",",".")
        else:
            s = s.replace(",","")
    elif "," in s:
        s = s.replace(",",".")
    try: return float(s)
    except: return None

def norm(s):
    s = (s or "").lower()
    s = ihtml.unescape(s)
    s = re.sub(r"[^a-z0-9€$£äöüßα-ωάέήίόύώϊϋΐΰ]+"," ",s,flags=re.I)
    return re.sub(r"\s+"," ",s).strip()

COUNTRY_SYNONYMS = {
    "Ηνωμένες Πολιτείες": ["usa","united states","america","us"],
    "Ελλάδα": ["greece","greek","hellas"],
    "Γερμανία": ["germany","deutschland"],
    "Γαλλία": ["france"],
    "Ιταλία": ["italy","italia"],
    "Ισπανία": ["spain","espana"],
    "Ηνωμένο Βασίλειο": ["united kingdom","great britain","britain","england"],
}

def make_queries(payload):
    coin = payload.get("coin") or {}
    raw = (payload.get("raw_query") or coin.get("raw") or "").strip()
    country = (coin.get("country") or "").strip()
    denom = (coin.get("denom") or coin.get("denomination") or "").strip()
    year = str(coin.get("year") or "").strip()
    variant = (coin.get("variant") or "").strip()
    grade = (coin.get("grade") or "").strip()

    qs = []
    if raw: qs.append(raw)
    core = " ".join(x for x in [country, denom, year] if x)
    if core: qs.append(core)
    if core and variant: qs.append(core+" "+variant)
    if country in COUNTRY_SYNONYMS:
        for syn in COUNTRY_SYNONYMS[country][:2]:
            qs.append(" ".join(x for x in [syn, denom, year, variant] if x))
    # US 25-cent special naming
    d = denom.lower()
    if country == "Ηνωμένες Πολιτείες" and ("25 cent" in d or "quarter" in raw.lower()):
        qs += [f"USA quarter {year}", f"United States 1/4 dollar {year}", f"Washington quarter {year}"]
    if grade and core: qs.append(core+" "+grade)
    # Deduplicate preserving order
    out=[]
    seen=set()
    for q in qs:
        q=re.sub(r"\s+"," ",q).strip()
        k=q.lower()
        if q and k not in seen:
            seen.add(k); out.append(q)
    return out[:8]

def ma_urls(query):
    q = urllib.parse.quote_plus(query)
    return [
        f"https://www.ma-shops.com/shops/search.php?searchstr={q}",
        f"https://www.ma-shops.com/shops/search.php?searchstr={q}&sortby=preis_eur",
    ]

def ebay_urls(query):
    q = urllib.parse.quote_plus(query)
    return [f"https://www.ebay.com/sch/i.html?_nkw={q}&_sacat=11116&rt=nc"]

def passes_hard_filter(title, payload):
    """Hard relevance gate applied BEFORE fuzzy scoring/price sorting.

    Numismatic listing titles are short and specific — the year and the
    numeric part of the denomination ("1" in "1 ruble") are almost always
    stated verbatim. Requiring them to actually appear stops a cheap but
    unrelated coin from winning just because it scraped past a loose fuzzy
    similarity threshold and happened to be the lowest price.
    """
    coin = payload.get("coin") or {}
    a = norm(title)
    if not a:
        return False
    year = str(coin.get("year") or "").strip()
    if year and year not in a:
        return False
    denom = str(coin.get("denom") or "").strip()
    if denom:
        m = re.match(r"([0-9]+(?:[.,][0-9]+)?)", denom)
        if m and m.group(1) not in a:
            return False
    return True

def score_title(title, payload):
    coin = payload.get("coin") or {}
    query = " ".join([
        str(payload.get("raw_query") or ""),
        str(coin.get("country") or ""),
        str(coin.get("denom") or ""),
        str(coin.get("year") or ""),
        str(coin.get("variant") or "")
    ])
    a,b = norm(title), norm(query)
    if not a or not b: return 0.0
    score = SequenceMatcher(None,a,b).ratio()
    year = str(coin.get("year") or "")
    if year and year in a: score += 0.22
    # reward denom tokens
    for tok in norm(coin.get("denom") or "").split():
        if tok and tok in a: score += 0.06
    return score

def extract_from_jsonld(soup, source_url, payload):
    offers=[]
    import json
    for tag in soup.find_all("script", type="application/ld+json"):
        raw=tag.string or tag.get_text(" ",strip=True)
        if not raw: continue
        try: data=json.loads(raw)
        except: continue
        stack=data if isinstance(data,list) else [data]
        while stack:
            x=stack.pop()
            if isinstance(x,list): stack.extend(x); continue
            if not isinstance(x,dict): continue
            for v in x.values():
                if isinstance(v,(dict,list)): stack.append(v)
            typ=x.get("@type")
            if typ in ("Product","IndividualProduct"):
                title=x.get("name") or ""
                url=x.get("url") or source_url
                off=x.get("offers") or {}
                if isinstance(off,list): off=off[0] if off else {}
                price=num(off.get("price")) if isinstance(off,dict) else None
                curr=(off.get("priceCurrency") if isinstance(off,dict) else None) or "EUR"
                avail=(off.get("availability") if isinstance(off,dict) else "") or ""
                if price is not None:
                    offers.append({"title":title,"url":url,"price":price,"shipping":0.0,
                                   "currency":curr,"dealer":"","grade":"","availability":avail,
                                   "_score":score_title(title,payload)})
    return offers

def extract_prices_with_shipping(text):
    """Find the item price and, separately, a nearby shipping cost in free text."""
    matches=[]
    for pat in PRICE_PATTERNS:
        for m in pat.finditer(text):
            val=num(m.group(1))
            if val is not None and val>0:
                matches.append((m.start(), val, detect_currency(m.group(0))))
    if not matches: return None
    matches.sort(key=lambda x:x[0])
    _, price, currency = matches[0]
    shipping=0.0
    for start, val, _ in matches[1:]:
        ctx=text[max(0,start-15):start+60].lower()
        if "shipping" in ctx:
            shipping=val
            break
    return price, shipping, currency

def extract_cards(soup, source_url, payload):
    # Only anchors that point at an actual listing (item.php?id=NNN). Generic
    # "any anchor" matching previously picked up navigation/category links
    # (e.g. "Europe", "Euro Coins") and grabbed an unrelated nearby price.
    anchors=soup.find_all("a", href=re.compile(r"item\.php\?id="))
    groups={}
    for a in anchors:
        href=a.get("href","")
        if not href: continue
        absu=urllib.parse.urljoin(source_url,href)
        idm=re.search(r"item\.php\?id=(\d+)",absu)
        if not idm: continue
        iid=idm.group(1)
        groups.setdefault(iid,{"url":absu,"anchors":[]})["anchors"].append(a)

    offers=[]
    for iid,g in groups.items():
        title=""
        for a in g["anchors"]:
            tt=" ".join(a.stripped_strings).strip()
            if len(tt)>len(title): title=tt
        if not title or len(title)<4: continue

        # Scope the price search to the item's own table row, not the whole page.
        container=None
        for a in g["anchors"]:
            tr=a.find_parent("tr")
            if tr: container=tr; break
        if container is None:
            node=g["anchors"][0]
            for _ in range(6):
                if not node.parent: break
                node=node.parent
                txt=" ".join(node.stripped_strings)
                if any(p.search(txt) for p in PRICE_PATTERNS) and len(txt)<1500:
                    container=node; break
        if container is None: continue

        text=" ".join(container.stripped_strings)
        result=extract_prices_with_shipping(text)
        if not result: continue
        price, shipping, currency = result
        if price<=0 or price>200000: continue
        offers.append({"title":title,"url":g["url"],"price":price,"shipping":shipping,
                       "currency":currency,"dealer":"","grade":"","availability":"",
                       "_score":score_title(title,payload)})
    return offers

def fetch_search(query, payload):
    last_err=None
    for url in ma_urls(query):
        try:
            r=SESSION.get(url,timeout=20,allow_redirects=True)
            print(f"[MA-Shops] GET {url} -> HTTP {r.status_code}, {len(r.text)} chars", flush=True)
            if r.status_code!=200:
                last_err=f"HTTP {r.status_code}"
                continue
            if "captcha" in r.text.lower() and len(r.text)<200000:
                last_err="MA-Shops returned a CAPTCHA/anti-bot page"
                print(f"[MA-Shops]   -> looks like a CAPTCHA/anti-bot page", flush=True)
                continue
            soup=BeautifulSoup(r.text,"html.parser")
            offers=extract_from_jsonld(soup,r.url,payload)
            offers+=extract_cards(soup,r.url,payload)
            for o in offers: o["dealer_source"]="MA-Shops"
            print(f"[MA-Shops]   -> parsed {len(offers)} offer(s)", flush=True)
            if offers:
                return offers, r.url, None
            last_err="No offer blocks parsed"
        except Exception as e:
            last_err=str(e)
            print(f"[MA-Shops]   -> EXCEPTION: {e}", flush=True)
    return [], None, last_err

def extract_ebay_cards(soup, source_url, payload):
    offers=[]
    items = soup.select("li.s-item, div.s-item__wrapper")
    seen=set()
    for item in items:
        link = item.select_one('a.s-item__link, a[href*="/itm/"]')
        if not link: continue
        href = link.get("href","")
        if "/itm/" not in href: continue
        href = href.split("?")[0]
        if href in seen: continue
        title_el = item.select_one(".s-item__title")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title or title.strip().lower() in ("shop on ebay","new listing"):
            continue
        price_el = item.select_one(".s-item__price")
        if not price_el: continue
        price_text = price_el.get_text(" ", strip=True).split(" to ")[0]
        price = None
        currency = "USD"
        for pat in PRICE_PATTERNS:
            m = pat.search(price_text)
            if m:
                price = num(m.group(1)); currency = detect_currency(m.group(0)); break
        if price is None or price <= 0 or price > 200000:
            continue
        shipping = 0.0
        ship_el = item.select_one(".s-item__shipping, .s-item__logisticsCost")
        if ship_el:
            st = ship_el.get_text(" ", strip=True)
            if "free" not in st.lower():
                for pat in PRICE_PATTERNS:
                    m = pat.search(st)
                    if m:
                        shipping = num(m.group(1)) or 0.0; break
        seen.add(href)
        offers.append({"title":title,"url":href,"price":price,"shipping":shipping,
                       "currency":currency,"dealer":"eBay","grade":"","availability":"",
                       "_score":score_title(title,payload)})
    return offers

def fetch_ebay_search(query, payload):
    warm_up_ebay()
    last_err=None
    for url in ebay_urls(query):
        try:
            r=SESSION.get(url,timeout=20,allow_redirects=True,headers={"Referer":"https://www.google.com/"})
            print(f"[eBay] GET {url} -> HTTP {r.status_code}, {len(r.text)} chars", flush=True)
            if r.status_code!=200:
                last_err=f"HTTP {r.status_code}"
                snippet=re.sub(r"\s+"," ",r.text).strip()[:200]
                print(f"[eBay]   -> body snippet: {snippet}", flush=True)
                continue
            soup=BeautifulSoup(r.text,"html.parser")
            offers=extract_ebay_cards(soup,r.url,payload)
            for o in offers: o["dealer_source"]="eBay"
            print(f"[eBay]   -> parsed {len(offers)} offer(s)", flush=True)
            if offers:
                return offers, r.url, None
            last_err="No offer blocks parsed"
            # Diagnostics: if we got a full page but found 0 s-item cards, it's
            # very likely eBay served a bot-check/interstitial instead of real
            # results. Print tell-tale signs so this is visible in the console.
            low=r.text.lower()
            hints=[h for h in ("captcha","are you a human","pardon our interruption","verify yourself","robot check") if h in low]
            if hints:
                hint_str=", ".join(hints)
                print(f"[eBay]   -> page looks like a bot-check page (found: {hint_str})", flush=True)
            n_items=len(soup.select("li.s-item, div.s-item__wrapper"))
            print(f"[eBay]   -> raw 'li.s-item' matches on page: {n_items}", flush=True)
        except Exception as e:
            last_err=str(e)
            print(f"[eBay]   -> EXCEPTION: {e}", flush=True)
    return [], None, last_err

@app.get("/health")
def health():
    return jsonify({"ok":True,"service":"NumisVault backend (MA-Shops)"})

@app.post("/api/coin-search")
def coin_search():
    payload=request.get_json(silent=True) or {}
    queries=make_queries(payload)
    all_offers=[]
    errors=[]
    used=[]
    for q in queries:
        ma_offers,ma_url,ma_err=fetch_search(q,payload)
        if ma_url: used.append({"query":q,"source":"MA-Shops","url":ma_url})
        if ma_err: errors.append({"query":q,"source":"MA-Shops","error":ma_err})
        all_offers.extend(ma_offers)

        # NOTE: eBay auto-search is intentionally disabled. eBay's search results
        # page is protected by a PerimeterX "Pardon Our Interruption" JS challenge
        # that no amount of header spoofing or session warm-up can pass from a
        # plain HTTP client — it requires a real, JS-executing browser. Rather
        # than silently return wrong/no results, eBay stays available as a
        # one-click manual search link in the UI instead (opens in the user's
        # own browser, which passes the challenge normally).
        # eb_offers,eb_url,eb_err=fetch_ebay_search(q,payload)
        # if eb_url: used.append({"query":q,"source":"eBay","url":eb_url})
        # if eb_err: errors.append({"query":q,"source":"eBay","error":eb_err})
        # all_offers.extend(eb_offers)

        if len(all_offers)>=40: break
        time.sleep(0.35)

    # Deduplicate per source+URL and keep best-scoring parse.
    by={}
    for o in all_offers:
        key=(o.get("dealer_source") or o.get("dealer") or ""), o.get("url") or (o.get("title"),o.get("price"))
        if key not in by or o.get("_score",0)>by[key].get("_score",0):
            by[key]=o
    offers=list(by.values())

    # Hard relevance gate FIRST: a cheap-but-unrelated listing must not be able
    # to win purely on price. Only offers whose title actually contains the
    # coin's year and denomination number are eligible at all.
    strict=[o for o in offers if passes_hard_filter(o.get("title",""), payload)]

    note=""
    if strict:
        relevant=[o for o in strict if o.get("_score",0)>=0.32]
        if not relevant:
            relevant=sorted(strict, key=lambda o:-o.get("_score",0))[:10]
            note="Titles matched year/denomination but fuzzy similarity was low; showing best candidates."
    else:
        # Nothing passed the strict gate — fall back to fuzzy-only ranking but
        # say so explicitly, since this is a materially weaker match.
        offers.sort(key=lambda o:-o.get("_score",0))
        relevant=[o for o in offers if o.get("_score",0)>=0.45][:10]
        if relevant:
            note="No listing title contained an exact year/denomination match; showing the closest fuzzy matches instead — verify carefully before trusting this price."
    relevant=relevant[:25]

    include_shipping=bool(payload.get("include_shipping"))
    for o in relevant:
        if not o.get("dealer"): o["dealer"]=o.get("dealer_source","")
        o["total"]=round(float(o["price"])+(float(o.get("shipping") or 0) if include_shipping else 0),2)
        o.pop("_score",None)
        o.pop("dealer_source",None)

    # Only NOW sort by price — and only within the already-relevance-filtered set.
    relevant.sort(key=lambda o:o.get("total",10**9))
    best=relevant[0] if relevant else None
    sources_ok=sorted(set(u["source"] for u in used))
    sources_failed=sorted(set(e["source"] for e in errors) - set(sources_ok))

    return jsonify({
        "source":"MA-Shops",
        "queries":queries,
        "used_search_pages":used,
        "offers":relevant,
        "best_offer":best,
        "count":len(relevant),
        "note":note,
        "sources_ok":sources_ok,
        "sources_failed":sources_failed,
        "errors":errors[-8:],
        "shipping_note":"Shipping is 0 unless explicitly visible in the parsed listing/search page."
    })

def get_lan_ip():
    """Best-effort local network IP (the one other devices on the same
    WiFi/LAN would use to reach this machine) — no packets are actually sent,
    this just asks the OS which interface it would use for an outbound route."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    lan_ip = get_lan_ip()
    print("NumisVault backend")
    print(f"  On this PC:        http://127.0.0.1:{port}")
    if lan_ip and port == 8765:
        print(f"  From phone/tablet:  http://{lan_ip}:{port}   (same WiFi required)")
        print(f"  -> In the app's Research Settings, set the endpoint to:")
        print(f"     http://{lan_ip}:{port}/api/coin-search")
    print(f"  Health check: http://127.0.0.1:{port}/health")
    print("  If Windows Firewall prompts you, click 'Allow access'.")
    app.run(host="0.0.0.0",port=port,debug=False)
