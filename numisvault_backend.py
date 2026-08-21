from flask import Flask, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
import requests, re, html as ihtml, urllib.parse, time, os, math, threading, json
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import email.utils
from datetime import timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
try:
    from coin_identity_resolver import resolve_coin_identity, get_resolver
    RESOLVER_AVAILABLE=True
except Exception as _resolver_import_err:
    RESOLVER_AVAILABLE=False
    print(f"[resolver] coin_identity_resolver not available: {_resolver_import_err}")
try:
    from auction_models import AuctionComparable
    from auction_matching import classify_comparable
    from auction_sources import ManualComparableAdapter, CSVComparableAdapter
    import auction_valuation as auction_val
    import auction_bid_advisor as auction_bid
    import auction_sell_engine as auction_sell
    import auction_fx as auction_fx
    AUCTION_INTELLIGENCE_V3_AVAILABLE=True
except Exception as _auction_v3_import_err:
    AUCTION_INTELLIGENCE_V3_AVAILABLE=False
    print(f"[auction-v3] Auction Intelligence 3.0 modules not available: {_auction_v3_import_err}")
try:
    from resolver_corrections import get_store as get_corrections_store
    CORRECTIONS_AVAILABLE=True
except Exception as _corrections_import_err:
    CORRECTIONS_AVAILABLE=False
    print(f"[corrections] resolver_corrections not available: {_corrections_import_err}")
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

app = Flask(__name__)
ALLOWED_ORIGINS=os.environ.get("COINBIDS_CORS_ORIGINS","*").split(",")
CORS(app, resources={r"/api/*":{"origins":ALLOWED_ORIGINS}}, supports_credentials=False)


APP_DIR=os.path.dirname(os.path.abspath(__file__))

@app.get("/")
def public_homepage():
    # Public, crawlable marketing homepage — separate from the authenticated
    # app (see /app below). Real, server-rendered HTML with no dependency on
    # JS execution for content/SEO purposes.
    return send_from_directory(APP_DIR,"public_home.html")

@app.get("/app")
def frontend():
    # The authenticated CoinBids application (Supabase login gate + the full
    # SPA once signed in) — unchanged, just moved to its own clean URL so it
    # no longer doubles as the public marketing homepage.
    return send_from_directory(APP_DIR,"index.html")

@app.get("/index.html")
def frontend_legacy_redirect():
    # Anyone with an old bookmark/link to the previous root entry point is
    # sent to the app rather than getting a broken or misleading page.
    return redirect("/app", code=301)

@app.get("/identify-coin")
def public_identify_coin():
    return send_from_directory(APP_DIR,"identify-coin.html")

@app.get("/coin-value")
def public_coin_value():
    return send_from_directory(APP_DIR,"coin-value.html")

@app.get("/auction-intelligence")
def public_auction_intelligence():
    return send_from_directory(APP_DIR,"auction-intelligence.html")

@app.get("/metal-value")
def public_metal_value():
    return send_from_directory(APP_DIR,"metal-value.html")

@app.get("/robots.txt")
def robots_txt():
    return send_from_directory(APP_DIR,"robots.txt",mimetype="text/plain")

@app.get("/sitemap.xml")
def sitemap_xml():
    return send_from_directory(APP_DIR,"sitemap.xml",mimetype="application/xml")

@app.get("/site.webmanifest")
def site_webmanifest():
    return send_from_directory(APP_DIR,"site.webmanifest",mimetype="application/manifest+json")

@app.get("/favicon.ico")
def favicon_ico():
    return send_from_directory(APP_DIR,"favicon.ico",mimetype="image/vnd.microsoft.icon")

# Favicons, the Open Graph share image, and the shared stylesheet used by the
# public pages — all real files derived from the existing CoinBids logo (not
# newly designed artwork), served directly from the repo root since that's
# how they're actually deployed (uploaded individually via the GitHub web UI
# rather than as a folder). An explicit whitelist — not a wildcard — so this
# can never serve an arbitrary file from the app directory.
_PUBLIC_ROOT_ASSETS={
    "favicon-16x16.png":"image/png",
    "favicon-32x32.png":"image/png",
    "apple-touch-icon.png":"image/png",
    "android-chrome-192x192.png":"image/png",
    "android-chrome-512x512.png":"image/png",
    "og-image.png":"image/png",
    "public.css":"text/css",
}
@app.get("/<any(%s):filename>" % ",".join(f"'{k}'" for k in _PUBLIC_ROOT_ASSETS.keys()))
def public_root_assets(filename):
    return send_from_directory(APP_DIR,filename,mimetype=_PUBLIC_ROOT_ASSETS[filename])

@app.errorhandler(404)
def not_found(e):
    # A genuine, useful 404 page (links back to the real public pages)
    # rather than redirecting every unknown URL to the homepage.
    return send_from_directory(APP_DIR,"404.html"),404

_RATE_LOCK=threading.Lock()
_RATE_BUCKET={}
_RATE_LIMIT_PER_MIN=int(os.environ.get("COINBIDS_RATE_LIMIT_PER_MIN","30"))
@app.before_request
def _rate_limit():
    if not request.path.startswith("/api/"):return None
    ip=(request.headers.get("X-Forwarded-For","").split(",")[0].strip() or request.remote_addr or "unknown")
    now=time.time();window=int(now//60)
    with _RATE_LOCK:
        key=(ip,window)
        _RATE_BUCKET[key]=_RATE_BUCKET.get(key,0)+1
        count=_RATE_BUCKET[key]
        if len(_RATE_BUCKET)>5000:
            for k in [k for k in _RATE_BUCKET if k[1]<window-2]: _RATE_BUCKET.pop(k,None)
    if count>_RATE_LIMIT_PER_MIN:
        return jsonify({"error":"Rate limit exceeded. Please slow down and try again shortly."}),429
    return None

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

# --- MA-Shops shipping database -----------------------------------------
# Shipping is resolved locally from ma_shops_shipping.csv. This removes the
# paid ScrapingBee dependency and avoids per-search shipping-page requests.
def _resolve_shipping_csv_path():
    env=(os.environ.get("MA_SHOPS_SHIPPING_CSV") or "").strip()
    candidates=[
        env,
        os.path.join(APP_DIR,"ma_shops_shipping.csv"),
        os.path.join(os.getcwd(),"ma_shops_shipping.csv"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    # Keep the canonical expected path for clear diagnostics.
    return os.path.join(APP_DIR,"ma_shops_shipping.csv")

MA_SHOPS_SHIPPING_CSV = _resolve_shipping_csv_path()
_MA_SHOPS_SHIPPING_ROWS = None
_MA_SHOPS_SHIPPING_MTIME = None
_MA_SHOPS_SHIPPING_MISSING_LOGGED = False

def _fnum(v):
    try:
        if v is None or str(v).strip()=="" or str(v).lower()=="nan":
            return None
        return float(v)
    except Exception:
        return None

def load_mashops_shipping_rows():
    global _MA_SHOPS_SHIPPING_ROWS, _MA_SHOPS_SHIPPING_MTIME, _MA_SHOPS_SHIPPING_MISSING_LOGGED, MA_SHOPS_SHIPPING_CSV
    # Re-resolve on each cold/missing load so deployments that add the CSV
    # after process start recover automatically.
    if not os.path.isfile(MA_SHOPS_SHIPPING_CSV):
        MA_SHOPS_SHIPPING_CSV=_resolve_shipping_csv_path()
    try:
        mtime=os.path.getmtime(MA_SHOPS_SHIPPING_CSV)
    except OSError:
        if not _MA_SHOPS_SHIPPING_MISSING_LOGGED:
            print(f"[shipping-db] missing file: {MA_SHOPS_SHIPPING_CSV}", flush=True)
            _MA_SHOPS_SHIPPING_MISSING_LOGGED=True
        return []
    _MA_SHOPS_SHIPPING_MISSING_LOGGED=False
    if _MA_SHOPS_SHIPPING_ROWS is not None and _MA_SHOPS_SHIPPING_MTIME==mtime:
        return _MA_SHOPS_SHIPPING_ROWS
    rows=[]
    try:
        import csv
        with open(MA_SHOPS_SHIPPING_CSV,"r",encoding="utf-8-sig",newline="") as fh:
            for r in csv.DictReader(fh):
                rows.append({
                    "dealer_slug":(r.get("dealer_slug") or "").strip().lower(),
                    "destination":(r.get("destination") or "").strip(),
                    "price_min":_fnum(r.get("price_tier_min")),
                    "price_max":_fnum(r.get("price_tier_max")),
                    "weight_min":_fnum(r.get("weight_tier_min_g")),
                    "weight_max":_fnum(r.get("weight_tier_max_g")),
                    "cost":_fnum(r.get("cost")),
                    "currency":(r.get("currency") or "EUR").strip().upper(),
                    "free_shipping":str(r.get("free_shipping") or "").strip().lower() in ("1","true","yes"),
                    "tier_label":r.get("tier_label") or "",
                })
        _MA_SHOPS_SHIPPING_ROWS=rows
        _MA_SHOPS_SHIPPING_MTIME=mtime
        print(f"[shipping-db] loaded {len(rows)} MA-Shops shipping rules", flush=True)
        return rows
    except Exception as e:
        print(f"[shipping-db] load failed: {type(e).__name__}: {e}", flush=True)
        return []

EU_COUNTRIES={
    "austria","belgium","bulgaria","croatia","cyprus","czech republic","denmark",
    "estonia","finland","france","germany","greece","hungary","ireland","italy",
    "latvia","lithuania","luxembourg","malta","netherlands","poland","portugal",
    "romania","slovakia","slovenia","spain","sweden"
}
EUROPE_COUNTRIES=EU_COUNTRIES | {
    "albania","andorra","armenia","azerbaijan","belarus","bosnia and herzegovina",
    "georgia","iceland","kosovo","liechtenstein","moldova","monaco","montenegro",
    "north macedonia","norway","russia","san marino","serbia","switzerland",
    "turkey","ukraine","united kingdom","vatican city"
}
DEST_ALIASES={
    "usa":"united states","us":"united states","u.s.":"united states",
    "united states of america":"united states",
    "uk":"united kingdom","great britain":"united kingdom","britain":"united kingdom",
    "england":"united kingdom","scotland":"united kingdom","wales":"united kingdom",
    "northern ireland":"united kingdom","northern ireland (uk)":"united kingdom",
    "hellas":"greece","ellada":"greece","ελλαδα":"greece",
    "czechia":"czech republic",
    "croatia, republic of":"croatia",
    "macedonia":"north macedonia",
    "korea, south":"south korea","republic of korea":"south korea",
    "russian federation":"russia",
    "vatican city state":"vatican city",
    "eu":"european union","worldwide":"world"
}
def _norm_dest(s):
    s=(s or "").strip().lower()
    return DEST_ALIASES.get(s,s)

def _dest_priority(destination,target):
    """Global destination matching.

    Exact country always wins. EU fallback is used ONLY for EU members;
    European non-EU countries never inherit EU rates. Europe is used only
    when a dealer actually publishes a Europe row; World is final fallback.
    """
    d=_norm_dest(destination); t=_norm_dest(target)
    if not t:return 99
    if d==t:return 0
    if t in EU_COUNTRIES and d=="european union":return 1
    if t in EUROPE_COUNTRIES and d=="europe":return 2
    if d=="world":return 3
    return 99

def _dealer_slug_from_offer(offer):
    u=str(offer.get("url") or "")
    m=re.search(r"ma-shops\.com/([^/?#]+)/",u,re.I)
    return m.group(1).lower() if m else ""

def lookup_mashops_shipping(offer, ship_to_country, item_weight_g=None):
    """Resolve shipping from the local 289-dealer tier database.

    Exact country wins; regional fallback is selected from the requested
    country (EU only for EU members, then Europe where published, then World).
    Price and weight constraints are respected. If a rule needs
    weight but coin weight is unknown, it is not guessed.
    """
    slug=_dealer_slug_from_offer(offer)
    price=_fnum(offer.get("price"))
    weight=_fnum(item_weight_g)
    if not slug or price is None:return False
    candidates=[]
    for r in load_mashops_shipping_rows():
        if r["dealer_slug"]!=slug:continue
        pr=_dest_priority(r["destination"],ship_to_country)
        if pr>=99:continue
        if r["price_min"] is not None and price<r["price_min"]:continue
        if r["price_max"] is not None and price>r["price_max"]:continue
        if r["weight_min"] is not None or r["weight_max"] is not None:
            if weight is None:continue
            if r["weight_min"] is not None and weight<r["weight_min"]:continue
            if r["weight_max"] is not None and weight>r["weight_max"]:continue
        pspan=(r["price_max"]-r["price_min"]) if r["price_min"] is not None and r["price_max"] is not None else float("inf")
        wspan=(r["weight_max"]-r["weight_min"]) if r["weight_min"] is not None and r["weight_max"] is not None else float("inf")
        candidates.append((pr,pspan,wspan,r))
    if not candidates:return False
    candidates.sort(key=lambda x:(x[0],x[1],x[2]))
    r=candidates[0][3]
    offer["shipping"]=0.0 if r["free_shipping"] else r["cost"]
    offer["shipping_status"]="known_target_db"
    offer["shipping_destination"]=r["destination"]
    offer["shipping_currency"]=r["currency"]
    offer["shipping_source"]="MA-Shops dealer shipping database"
    offer["shipping_tier"]=r["tier_label"]
    return offer["shipping"] is not None

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
    elif s.count(".") > 1:
        # Unambiguous case: more than one dot can only mean grouped thousands
        # (e.g. "1.234.567" -> 1234567), never a single decimal point.
        s = s.replace(".","")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        # Single dot but grouped-by-three with no fractional remainder
        # (e.g. "1.234" with nothing after) is still ambiguous in isolation —
        # we deliberately do NOT rewrite it here because a genuine "€1.23"-style
        # decimal price is far more common in scraped listings than a bare
        # thousands amount with no decimals. Left as a documented limitation
        # (see NUMISVAULT_BUG_AUDIT H11) rather than guessed silently.
        pass
    try: return float(s)
    except: return None

GRADE_MINT_TOKENS=("unc","bu","proof","pf","ms60","ms61","ms62","ms63","ms64","ms65","ms66","ms67","ms68","ms69","ms70","gem unc","fdc","brilliant uncirculated","uncirculated")
GRADE_CIRC_TOKENS=("poor","fair","good","vg","fine","vf","xf","ef","au","about uncirculated","very fine","very good","almost uncirculated")
def grade_tier(g):
    """Classify a grade string into MINT / CIRCULATED / UNKNOWN. Best-effort,
    token based — never used to invent a grade that wasn't actually stated."""
    a=norm(g)
    if not a:return None
    if re.search(r"\bms\s*-?\s*(6[0-9]|70)\b",a) or re.search(r"\bpf\s*-?\s*(6[0-9]|70)\b",a):return "MINT"
    if any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])",a) for t in GRADE_MINT_TOKENS):return "MINT"
    if any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])",a) for t in GRADE_CIRC_TOKENS):return "CIRCULATED"
    return None

def grade_conflicts(target_grade, title):
    """Hard grade gate: only rejects when BOTH sides state a tier and they
    disagree (e.g. requested UNC/Proof vs a listing explicitly stated VF/Fine).
    Unknown/unstated listing grade is never treated as a mismatch — per spec,
    absence of a grade token is not negative evidence."""
    want=grade_tier(target_grade)
    if not want:return False
    have=grade_tier(title)
    if not have:return False
    return want!=have

_GREEK_ACCENT_MAP = str.maketrans({
    "ά":"α","έ":"ε","ή":"η","ί":"ι","ό":"ο","ύ":"υ","ώ":"ω","ϊ":"ι","ϋ":"υ","ΐ":"ι","ΰ":"υ",
})
def norm(s):
    s = (s or "").lower()
    s = ihtml.unescape(s)
    # Central/Eastern/Northern European diacritics (ł ø é ř š ž etc.) used to
    # fall outside this whitelist and get replaced with a space, silently
    # splitting words apart (e.g. "Złoty" -> "Z oty", "Øre" -> " re") before
    # any currency/country matching ever saw them.
    s = re.sub(r"[^a-z0-9€$£äöüßłøéèêëáàâíìîïóòôúùûýÿñçřšžćčęąśźżěůďťňđőű"
               r"α-ωάέήίόύώϊϋΐΰ]+"," ",s,flags=re.I)
    # Greek tonos accents (ά/έ/ή/...) are a separate concern from Latin
    # diacritics above: COUNTRY_CANON's Greek alias strings are stored
    # unaccented ("ελλαδα"), so "Ελλάδα" (with a real tonos accent) failed
    # to match at all via plain substring comparison — strip the accent so
    # both spellings normalize the same way.
    s = s.translate(_GREEK_ACCENT_MAP)
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


COUNTRY_CANON = {
    "croatia":["croatia","croatian","hrvatska","kroatien","croatie","croazia","croacia","κροατια"],
    "greece":["greece","greek","hellas","ellada","griechenland","grèce","grece","ελλαδα"],
    "germany":["germany","deutschland","allemagne","γερμανια"],
    "france":["france","francais","française","frankreich","γαλλια"],
    "italy":["italy","italia","italien","ιταλια"],
    "spain":["spain","espana","españa","spanien","ισπανια"],
    "united states":["united states","usa","u.s.a","america","vereinigte staaten"],
    "united kingdom":["united kingdom","great britain","britain","england","uk"],
    "monaco":["monaco","monegasque"],
    "slovenia":["slovenia","slovenian"],
    "slovakia":["slovakia","slovak"],
    "austria":["austria","osterreich","österreich"],
    "belgium":["belgium","belgie","belgique"],
    "netherlands":["netherlands","nederland","holland"],
    "portugal":["portugal"],
    "finland":["finland"],
    "ireland":["ireland"],
    "cyprus":["cyprus"],
    "malta":["malta"],
    "luxembourg":["luxembourg"],
    "estonia":["estonia"],
    "latvia":["latvia"],
    "lithuania":["lithuania"],
    "poland":["poland","polska","polen"],
    "czech republic":["czech republic","czechia","tschechien"],
    "sweden":["sweden","sverige","schweden"],
    "norway":["norway","norge","norwegen"],
    "denmark":["denmark","danmark","danemark"],
    "switzerland":["switzerland","suisse","schweiz","svizzera"],
    "australia":["australia"],
    "canada":["canada"],
    "bulgaria":["bulgaria"],
    "romania":["romania","roumanie"],
    "hungary":["hungary","magyarorszag","ungarn"],
    "japan":["japan"],
}
# ISO-3166 alpha-2, used only as a best-effort query-string hint (see
# enrich_offer_from_item_page) — never assumed to actually work.
COUNTRY_ISO2 = {
    "croatia":"HR","greece":"GR","germany":"DE","france":"FR","italy":"IT","spain":"ES",
    "united states":"US","united kingdom":"GB","monaco":"MC","slovenia":"SI","slovakia":"SK",
    "austria":"AT","belgium":"BE","netherlands":"NL","portugal":"PT","finland":"FI","ireland":"IE",
    "cyprus":"CY","malta":"MT","luxembourg":"LU","estonia":"EE","latvia":"LV","lithuania":"LT",
    "poland":"PL","czech republic":"CZ","sweden":"SE","norway":"NO","denmark":"DK",
    "switzerland":"CH","australia":"AU","canada":"CA","bulgaria":"BG","romania":"RO",
    "hungary":"HU","japan":"JP",
}
BANKNOTE_TERMS=("banknote","bank note","paper money","billet","banknoten","banknoten","pick #","pick p","watermark","serial number")
SET_TERMS=("kursmünzensatz","kursmunzensatz","coin set","coins set","annual set","year set","complete set","roll","rouleau","lot of","lot ")
COIN_TERMS=("coin","münze","munze","monnaie","moneta","commemorative","gedenkmünze","gedenkmunze","mint","proof","unc","bu")
SOLD_TERMS=("sold out","sold","verkauft","vendu","venduto","vendido","out of stock","not available","nicht verfügbar","nicht verfuegbar","reserved","reserviert")
def looks_unavailable(text):
    a=norm(text)
    return any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])",a) for t in SOLD_TERMS)

def canonical_country(s):
    a=norm(s)
    for canon,aliases in COUNTRY_CANON.items():
        if a==canon or any(norm(x) in a for x in aliases): return canon
    return a

def destination_pattern_and_iso(country_name):
    """Given whatever the user typed/selected as their destination country,
    return (regex_pattern_matching_any_known_name_for_it, iso2_code_or_empty,
    display_name). Falls back to a plain literal match of whatever text was
    given when the country isn't in COUNTRY_CANON, so an unusual destination
    still works (just without multi-language alias coverage)."""
    raw=(country_name or "Greece").strip()
    canon=canonical_country(raw)
    aliases=COUNTRY_CANON.get(canon)
    if not aliases:
        aliases=[raw.lower()] if raw else ["greece"]
        canon=raw.lower()
    iso=COUNTRY_ISO2.get(canon,"")
    display=canon.title() if canon in COUNTRY_CANON else raw.title()
    alt="|".join(re.escape(a) for a in sorted(set(aliases),key=len,reverse=True))
    pattern=rf"(?:{alt})"
    return pattern,iso,display

CURRENCY_UNIT_ALIASES = {
    # Modern
    "euro":["euro","euros","eur"],
    "dollar":["dollar","dollars","usd"],
    "pound":["pound","pounds","gbp","sterling"],
    "franc":["franc","francs","franken","frank","frs"],
    "yen":["yen"],
    "yuan":["yuan","renminbi"],
    # Historical / pre-euro European currencies — a coin listing rarely says
    # "Greece" in a Greek-numismatic term the same way a search query does,
    # so recognizing every common spelling variant (English/German/French/
    # native-plural) is what actually prevents a real, correct match from
    # being silently rejected as "wrong denomination".
    "drachma":["drachma","drachmas","drachmai","drachmae","drachme","drachmen"],
    "lira":["lira","lire","liras"],
    "peseta":["peseta","pesetas","ptas"],
    "mark":["mark","marks","deutsche mark","reichsmark","dm"],
    "guilder":["guilder","guilders","gulden"],
    "schilling":["schilling","schillings"],
    "markka":["markka","markkaa","finnmark","finnmarkka"],
    "krona":["krona","kronor","krone","kroner","kronur","krona"],
    "zloty":["zloty","zlote","zlotych","złoty"],
    "koruna":["koruna","koruny","korun"],
    "forint":["forint","forintok"],
    "leu":["leu","lei"],
    "lev":["lev","leva"],
    "kuna":["kuna","kune"],
    "dinar":["dinar","dinara","dinars"],
    "litas":["litas","litai","litu"],
    "lats":["lats","lati","latu"],
    "kroon":["kroon","krooni"],
    "ruble":["ruble","rubles","rouble","roubles","rubl","rublei"],
    "escudo":["escudo","escudos"],
    "tolar":["tolar","tolarjev","tolarja"],
    # Minor/subdivision units — kept distinct per currency family so, e.g., a
    # German pfennig can never accidentally satisfy a search for kopecks.
    "cent":["cent","cents","centesimo","centesimi","centesimos","céntimo","centimo",
            "centimos","centime","centimes"],
    "pfennig":["pfennig","pfennige","pfennigs"],
    "pence":["penny","pence"],
    "ore":["öre","ore","øre"],
    "grosz":["grosz","groszy","grosze"],
    "filler":["filler","fillér","fillerek"],
    "haler":["haler","haléř","halere","halierov","halier"],
    "ban":["ban","bani"],
    "stotinka":["stotinka","stotinki","stotin"],
    "lepton":["lepton","lepta","lepto"],
    "santim":["santim","santims"],
    "centas":["centas","centai","centu"],
    "kopeck":["kopeck","kopecks","kopek","kopeks","kopeyka"],
    "para":["para"],
    "lipa":["lipa","lipe"],
    # Pre-decimal / historical denominations — extremely common in actual
    # numismatic listings, often more so than modern currency for a
    # collector-focused search. Added on request after finding real gaps.
    "shilling":["shilling","shillings"],
    "crown":["crown","crowns"],
    "farthing":["farthing","farthings"],
    "sovereign":["sovereign","sovereigns"],
    "guinea":["guinea","guineas"],
    "groschen":["groschen","groschens"],
    "taler":["taler","talers","thaler","thalers"],
    "kreuzer":["kreuzer","kreutzer"],
    "real":["real","reales","reais"],
    "maravedi":["maravedi","maravedis","maravedí"],
    "soldo":["soldo","soldi"],
    "scudo":["scudo","scudi"],
    "baiocco":["baiocco","baiocchi"],
    "skilling":["skilling","skillingar"],
    "lek":["lek","leke"],
    "denar":["denar","denari"],
    "lari":["lari"],
    "dram":["dram","drams"],
    "piastre":["piastre","piastres","kurus","kuruş"],
    # Ancient Greek / Roman — common on MA-Shops ancient-coin listings.
    "obol":["obol","obols","obolos","obolus"],
    "stater":["stater","staters"],
    "drachm":["drachm","drachms"],
    "denarius":["denarius","denarii"],
    "sestertius":["sestertius","sestertii","sesterce"],
    "solidus":["solidus","solidi"],
    "tremissis":["tremissis","tremisses"],
}
# "as" (the Roman bronze coin) was deliberately left out of the dict above —
# "as" is an extremely common English word, and the digit+word boundary
# pattern used elsewhere ("2 as ...") is too easy to trigger on ordinary
# sentence text ("...sold as 2 as-is items..."), producing false positive
# denomination matches. Not worth the risk for one ancient unit name.

# eurocent is handled as its own compound before the plain "euro"/"cent"
# alternatives — see the H11/H06-adjacent note in _normalize_denom_unit.
_EUROCENT_ALTS = ["euro\\s*cents?","eurocents?"]
_ALT_TO_CANON = {}
_alt_parts = list(_EUROCENT_ALTS)
for _canon, _variants in CURRENCY_UNIT_ALIASES.items():
    for _v in sorted(set(_variants), key=len, reverse=True):
        _ALT_TO_CANON[_v.lower()] = _canon
        _alt_parts.append(re.escape(_v))
DENOM_UNIT_ALT = "€|£|\\$|¢|" + "|".join(_alt_parts)

def _normalize_denom_unit(unit):
    u=re.sub(r"\s+"," ",unit.lower()).strip()
    # "2 Euro Cent" is NOT "2 euro" — it's 1/100th the value and a completely
    # different, much more common coin. The bare "euro" alternative used to
    # match here first and silently drop the trailing "Cent", causing a
    # 2-euro-coin search to accept 2-eurocent listings/catalogue entries as
    # if they were the same denomination.
    if re.fullmatch(r"euro\s*cents?|eurocents?",u):return "cent"
    if u=="€":return "euro"
    if u=="£":return "pound"
    if u=="$":return "dollar"
    if u=="¢":return "cent"
    return _ALT_TO_CANON.get(u,u)

def parse_denomination(s):
    a=norm(s).replace(",",".")
    m=re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(" + DENOM_UNIT_ALT + r")(?![a-z])",a,re.I)
    if not m:return None
    val=float(m.group(1));unit=_normalize_denom_unit(m.group(2))
    return val,unit

def denomination_matches(target, title):
    td=parse_denomination(target)
    if not td:return True
    candidates=re.findall(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(" + DENOM_UNIT_ALT + r")(?![a-z])",norm(title),re.I)
    for v,u in candidates:
        d=parse_denomination(f"{v} {u}")
        if d and abs(d[0]-td[0])<1e-9 and d[1]==td[1]: return True
    return False

def classify_asset(title):
    a=norm(title)
    bank=sum(1 for x in BANKNOTE_TERMS if x in a)
    coin=sum(1 for x in COIN_TERMS if x in a)
    if re.search(r"\bp[- ]?\d{1,5}[a-z]?\b",a,re.I): bank+=3
    if bank>=2 and bank>coin:return "BANKNOTE",min(.99,.65+.08*bank)
    if coin>=1:return "COIN",min(.98,.70+.06*coin)
    return "UNKNOWN",.45

def product_scope(title):
    a=norm(title)
    if any(x in a for x in SET_TERMS):return "SET"
    return "SINGLE_COIN"

def variant_tokens(s):
    stop={"coin","commemorative","unc","bu","proof","euro","cent","dollar","year"}
    return [x for x in norm(s).split() if len(x)>=3 and x not in stop and not x.isdigit()]

def variant_matches(target_variant,title):
    toks=variant_tokens(target_variant)
    if not toks:return True
    a=norm(title)
    hits=sum(1 for x in toks if x in a)
    return hits>=max(1,math.ceil(len(toks)*0.6))

_FX_CACHE={"at":0,"rates":{"EUR":1.0}}
_FX_LOCK=threading.Lock()
def fx_rates():
    now=time.time()
    with _FX_LOCK:
        if now-_FX_CACHE["at"]<3600 and len(_FX_CACHE["rates"])>1:return _FX_CACHE["rates"]
    try:
        r=requests.get("https://open.er-api.com/v6/latest/EUR",timeout=8)
        d=r.json() if r.ok else {}
        rates=d.get("rates") or {}
        if rates:
            with _FX_LOCK:_FX_CACHE.update(at=now,rates=rates)
            return rates
    except Exception:pass
    return _FX_CACHE["rates"]

def to_eur(value,currency):
    if value is None:return None
    cur=(currency or "EUR").upper()
    if cur=="EUR":return float(value)
    rates=fx_rates()
    rate=rates.get(cur)
    return float(value)/float(rate) if rate else None

def make_queries(payload):
    coin = payload.get("coin") or {}
    raw = (payload.get("raw_query") or coin.get("raw") or "").strip()
    country = (coin.get("country") or "").strip()
    denom = (coin.get("denom") or coin.get("denomination") or "").strip()
    year = str(coin.get("year") or "").strip()
    variant = (coin.get("variant") or "").strip()
    grade = (coin.get("grade") or "").strip()

    # Server-side Coin Identity Resolver fallback: the frontend already calls
    # /api/resolve-coin before building this payload, so in the normal path
    # country/denom/year are already filled in. This is defense-in-depth for
    # callers that only send raw_query (e.g. a future API client, or if the
    # frontend resolver call failed silently) — it only FILLS IN missing
    # fields, never overwrites an explicit country/denom/year the caller
    # already supplied, and only uses resolver output that isn't ambiguous.
    resolver_queries=[]
    if RESOLVER_AVAILABLE and raw and not (country and denom and year):
        try:
            resolved=resolve_coin_identity(raw)
        except Exception as e:
            resolved=None
            print(f"[resolver] make_queries fallback failed for {raw!r}: {type(e).__name__}: {e}")
        if resolved and resolved.get("best") and resolved.get("status")=="resolved":
            # Only a resolver status of exactly "resolved" is treated as a
            # validated identity here. "review" means the resolver itself is
            # NOT confident — e.g. an invalid historical currency/year
            # combination, an unresolved ambiguity, or a negative product term
            # (replica/banknote/set/...). Server-side automatic query
            # construction must never treat a REVIEW verdict as equivalent to
            # a validated identity, even though "review" is still a reasonable
            # status to display to a human for manual confirmation elsewhere.
            b=resolved["best"]
            if not country and b.get("country"): country=b["country"]
            if not year and b.get("year"): year=str(b["year"])
            if not denom and b.get("denomination_value") is not None:
                unit=b.get("currency") or b.get("currency_code") or ""
                denom=f'{b["denomination_value"]:g} {unit}'.strip()
            resolver_queries=b.get("search_variants") or []

    qs = []
    # Exact user wording first. It is usually the highest-information query
    # and MA-Shops' cheapest-first ordering can then surface the cheapest
    # matching candidates immediately.
    if raw: qs.append(raw)
    qs.extend(resolver_queries)
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
    # Frontend-generated variant queries (searchVariants()) — previously computed
    # but silently discarded here; merge them in (validated/deduped below) so
    # frontend query-generation effort actually reaches MA-Shops.
    fv=payload.get("variants")
    if isinstance(fv,list):
        for v in fv[:5]:
            if isinstance(v,str) and v.strip(): qs.append(v.strip())
    # Deduplicate preserving order
    out=[]
    seen=set()
    for q in qs:
        q=re.sub(r"\s+"," ",q).strip()
        k=q.lower()
        if q and k not in seen:
            seen.add(k); out.append(q)
    return out[:5]

def ma_urls(query):
    """MA-Shops purchase discovery: request cheapest-first directly.

    Price Research is trying to find the cheapest VALID matching offers, so
    fetching the normal/relevance page as well only doubles network traffic.
    A broader relevance-ranked sample belongs to Auction Intelligence, not to
    the top-2 purchase-discovery path.
    """
    q = urllib.parse.quote_plus(query)
    return [
        f"https://www.ma-shops.com/shops/search.php?searchstr={q}&sortby=preis_eur",
    ]

def ebay_urls(query):
    q = urllib.parse.quote_plus(query)
    return [f"https://www.ebay.com/sch/i.html?_nkw={q}&_sacat=11116&rt=nc"]

COUNTRY_ALIASES = {
    "united states": ["united states","usa","u.s.a","u s a"],
    "united kingdom": ["united kingdom","uk","great britain","britain","england"],
}
def country_in_title(country, title_norm):
    if not country:return True
    target=canonical_country(country)
    a=norm(title_norm)
    aliases=COUNTRY_CANON.get(target,[target])
    return any(norm(x) in a for x in aliases)

def passes_hard_filter(title, payload):
    coin=payload.get("coin") or {}
    a=norm(title)
    if not a:return False
    asset,conf=classify_asset(title)
    if asset=="BANKNOTE":return False
    if product_scope(title)!="SINGLE_COIN":return False
    year=str(coin.get("year") or "").strip()
    if year and not re.search(rf"(?<!\d){re.escape(year)}(?!\d)",a):return False
    denom=str(coin.get("denom") or coin.get("denomination") or "").strip()
    if denom and not denomination_matches(denom,title):return False
    country=str(coin.get("country") or "").strip()
    if country and not country_in_title(country,a):return False
    variant=str(coin.get("variant") or "").strip()
    if variant and not variant_matches(variant,title):return False
    grade=str(coin.get("grade") or "").strip()
    if grade and grade_conflicts(grade,title):return False
    # Coin Intelligence Core: reject explicit non-coin product listings
    # (banknote/replica/copy/reproduction/medal/token/set/roll/lot/...) as a
    # second, independent layer alongside classify_asset/product_scope above.
    if RESOLVER_AVAILABLE:
        try:
            if get_resolver()._negative_flags(title):return False
        except Exception:
            pass
    return True

def score_title(title, payload):
    coin=payload.get("coin") or {}
    query=" ".join(str(x or "") for x in [coin.get("country"),coin.get("denom"),coin.get("year"),coin.get("variant")])
    a,b=norm(title),norm(query)
    if not a or not b:return 0.0
    score=SequenceMatcher(None,a,b).ratio()
    if country_in_title(coin.get("country") or "",a):score+=.18
    if denomination_matches(coin.get("denom") or "",title):score+=.22
    year=str(coin.get("year") or "")
    if year and re.search(rf"(?<!\d){re.escape(year)}(?!\d)",a):score+=.20
    if variant_matches(coin.get("variant") or "",title):score+=.10
    grade=coin.get("grade") or ""
    if grade and not grade_conflicts(grade,title) and grade_tier(title):score+=.08
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
                    avail_l=str(avail).lower()
                    if "outofstock" in avail_l.replace(" ","") or "discontinued" in avail_l or "soldout" in avail_l.replace(" ",""):
                        continue
                    offers.append({"title":title,"url":url,"price":price,"shipping":None,"shipping_status":"unknown",
                                   "currency":curr,"dealer":"","grade":"","availability":avail,
                                   "asset_type":classify_asset(title)[0],"product_scope":product_scope(title),"_score":score_title(title,payload)})
    return offers

def extract_prices_with_shipping(text, title=""):
    """Parse item price and shipping without confusing face value in the title for price."""
    text=str(text or "")
    # Prefer explicit price labels.
    explicit=[]
    for rx in (
        r"(?:price|preis|prix|prezzo|precio|our price|verkaufspreis)\s*:?\s*((?:EUR\s*|€\s*|US\$\s*|\$\s*|GBP\s*|£\s*|CHF\s*)?[0-9][0-9., ]*(?:\s*(?:EUR|€|USD|\$|GBP|£|CHF))?)",
        r"((?:EUR\s*|€\s*|US\$\s*|\$\s*|GBP\s*|£\s*|CHF\s*)[0-9][0-9., ]*)\s*(?:tax included|inkl\.?\s*mwst)"
    ):
        for m in re.finditer(rx,text,re.I):
            seg=m.group(1)
            for pat in PRICE_PATTERNS:
                pm=pat.search(seg)
                if pm:
                    v=num(pm.group(1))
                    if v and v>0: explicit.append((m.start(),v,detect_currency(pm.group(0))))
    matches=[]
    for pat in PRICE_PATTERNS:
        for m in pat.finditer(text):
            v=num(m.group(1))
            if v is not None and 0<v<=200000:matches.append((m.start(),v,detect_currency(m.group(0)),m.group(0)))
    if explicit:
        explicit.sort(key=lambda x:x[0]);_,price,currency=explicit[0]
    else:
        # Remove title text if it is embedded verbatim in the same row; denomination amounts in titles are not prices.
        title_norm=" ".join(str(title or "").split())
        working=text
        if title_norm and title_norm in working:working=working.replace(title_norm," ",1)
        candidates=[]
        for pat in PRICE_PATTERNS:
            for m in pat.finditer(working):
                v=num(m.group(1))
                if v and v>0:candidates.append((m.start(),v,detect_currency(m.group(0))))
        if not candidates:return None
        candidates.sort(key=lambda x:x[0]);_,price,currency=candidates[0]
    shipping=None;shipping_status="unknown";shipping_currency=currency
    ship_word=r"(?:shipping|postage|versand|porto|frais de port|spedizione|envio|envío)"
    money_seg=r"((?:EUR\s*|€\s*|US\$\s*|\$\s*|GBP\s*|£\s*|CHF\s*)?[0-9][0-9., ]*(?:\s*(?:EUR|€|USD|\$|GBP|£|CHF))?)"
    # MA-Shops commonly writes the amount BEFORE the word "shipping"
    # (e.g. "+ 7,50 EUR shipping (to Greece)"), not just "shipping: 7,50 EUR".
    # Try the safer "label: amount" ordering first; only fall back to
    # "amount + shipping" when it is unambiguously marked with a leading "+"
    # or a following destination, so an unrelated "item price ... shipping:"
    # sentence can't have its item price misread as the shipping amount.
    ship_patterns=[
        rf"{ship_word}(?:[^€$£0-9]{{0,60}}){money_seg}",
        rf"\+\s*{money_seg}\s*{ship_word}",
        rf"{money_seg}\s*{ship_word}\s*\(?\s*(?:to\s*)?(?:Greece|Griechenland|Gr[eè]ce|Ελλάδα)",
    ]
    for rxp in ship_patterns:
        sm=re.search(rxp,text,re.I)
        if not sm: continue
        seg=sm.group(1)
        for pat in PRICE_PATTERNS:
            pm=pat.search(seg)
            if pm:
                shipping=num(pm.group(1));shipping_currency=detect_currency(pm.group(0));shipping_status="free" if shipping==0 else "known";break
        if shipping is not None: break
    if re.search(r"(?:free shipping|versandkostenfrei|portofrei|livraison gratuite)",text,re.I):
        shipping=0.0;shipping_status="free";shipping_currency=currency
    # If item price and shipping were parsed in different currencies, do not
    # silently add them together — normalize shipping into the item's currency
    # first (bridged through EUR) so downstream all-in totals stay correct.
    if shipping is not None and shipping_currency!=currency:
        bridged=to_eur(shipping,shipping_currency)
        if bridged is not None:
            back = bridged if currency=="EUR" else None
            if currency!="EUR":
                rates=fx_rates();rate=rates.get(currency)
                back = bridged*rate if rate else None
            shipping = round(back,2) if back is not None else None
            if shipping is None: shipping_status="unknown"
    return price,shipping,currency,shipping_status

def smart_join(strings):
    """Join .stripped_strings fragments without inserting a space between
    pieces that are actually parts of the same number. Some sites render a
    price with the leading digit(s) in a separate styled element from the
    rest (e.g. "33" in one <span>, ",90" in the next) purely for visual
    effect — a plain " ".join() turns that into "3 3,90 EUR", and PRICE_
    PATTERNS then matches only the trailing "3,90" as the price, silently
    dropping the leading digit (33,90 misread as 3,90). Only merges when a
    fragment boundary is digit-then-digit/comma/period; ordinary words are
    unaffected."""
    out=""
    for s in strings:
        if not s: continue
        if out and out[-1].isdigit() and s[0] in "0123456789,.":
            out+=s
        else:
            if out: out+=" "
            out+=s
    return out


def detect_discount_from_price_cell(node, parsed_price=None):
    """Detect MA-Shops sale/discount pricing without confusing two arbitrary
    currency values with a discount.

    Preferred evidence:
      1) an explicitly struck/deleted old price (<del>, <s>, <strike> or CSS
         line-through) plus a lower current price;
      2) sale/discount lexical markers plus two prices.

    Returns metadata only. The effective/current price remains the price used
    for sorting and totals.
    """
    if node is None:
        return {"is_discounted":False}

    def _money_values(txt):
        vals=[]
        for pat in PRICE_PATTERNS:
            for m in pat.finditer(str(txt or "")):
                v=num(m.group(1))
                if v is not None and 0 < v <= 200000:
                    vals.append((v,detect_currency(m.group(0))))
        return vals

    old_candidates=[]
    try:
        marked=node.find_all(["del","s","strike"])
        # Include elements styled as line-through.
        marked += [
            x for x in node.find_all(style=True)
            if "line-through" in str(x.get("style","")).lower()
        ]
        seen=set()
        for el in marked:
            key=id(el)
            if key in seen: continue
            seen.add(key)
            old_candidates.extend(_money_values(smart_join(el.stripped_strings)))
    except Exception:
        pass

    full_text=smart_join(node.stripped_strings) if hasattr(node,"stripped_strings") else str(node or "")
    all_values=_money_values(full_text)
    sale_marker=bool(re.search(
        r"\\b(?:sale|special\\s*price|offer|discount|reduced|aktion|angebot|"
        r"sonderpreis|rabatt|promo(?:tion)?|soldes|remise|sconto|oferta)\\b",
        full_text,re.I
    ))

    current=float(parsed_price) if parsed_price is not None else None
    old_price=None
    currency=None

    # Strongest signal: crossed-out old price.
    for v,c in old_candidates:
        if current is None or v > current:
            if old_price is None or v > old_price:
                old_price=float(v); currency=c

    # Fallback only when sale language is present.
    if old_price is None and sale_marker and current is not None:
        higher=[(float(v),c) for v,c in all_values if float(v)>current]
        if higher:
            old_price,currency=max(higher,key=lambda x:x[0])

    if old_price is None or current is None or old_price<=current:
        return {"is_discounted":False}

    pct=round((old_price-current)/old_price*100.0,1)
    return {
        "is_discounted":True,
        "original_price":round(old_price,2),
        "sale_price":round(current,2),
        "discount_pct":pct,
        "discount_currency":currency,
    }


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
            tt=smart_join(a.stripped_strings).strip()
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
                txt=smart_join(node.stripped_strings)
                if any(p.search(txt) for p in PRICE_PATTERNS) and len(txt)<1500:
                    container=node; break
        if container is None: continue

        # Prefer the specific <td> that actually holds the price+shipping text
        # (identifiable by "shipping" / "tax included" / a currency marker) over
        # the whole row. Titles sometimes contain price-shaped substrings of
        # their own (e.g. "1 $ 1923 Dollar Peace Type...") which would
        # otherwise get misread as the real price.
        price_cell_text=None
        if hasattr(container,"find_all"):
            for td in container.find_all(["td","div","span"]):
                td_txt=smart_join(td.stripped_strings)
                if not td_txt or len(td_txt)>500: continue
                low=td_txt.lower()
                if ("shipping" in low or "tax included" in low) and any(p.search(td_txt) for p in PRICE_PATTERNS):
                    price_cell_text=td_txt
                    break
        # Keep the complete listing row as INTERNAL matching evidence.
        # MA-Shops often puts Country / Nominal value / Year in separate <td>
        # cells, while the clickable item anchor contains only descriptive Info.
        row_text=smart_join(container.stripped_strings)
        match_text=smart_join([title, row_text])
        text = price_cell_text if price_cell_text else row_text
        if looks_unavailable(text) or looks_unavailable(match_text): continue
        result=extract_prices_with_shipping(text,title)
        if not result: continue
        price, shipping, currency, shipping_status = result
        if price<=0 or price>200000: continue

        # Detect a genuine sale price (e.g. 105 EUR crossed out -> 75 EUR).
        # Use the CURRENT parsed price for ranking; keep the old price only as
        # evidence/UI metadata.
        discount_meta=detect_discount_from_price_cell(container,price)
        # A shipping figure read off the compact search-results snippet is
        # never destination-verified — MA-Shops only shows the seller's own
        # default-country shipping here, not Greece-specific, and cramped
        # card text is exactly where a stray nearby number gets misread as
        # shipping. Mark it explicitly "unverified" so coin_search() never
        # uses it to compute an all-in total unless/until the item-detail
        # page actually confirms a number (enrich_offer_from_item_page).
        # A plain "free shipping" claim is the one exception: sellers who
        # advertise that generally mean it broadly, not per-destination.
        if shipping is not None and shipping_status!="free":
            shipping_status="unverified"
        offer={"title":title,"url":g["url"],"price":price,"shipping":shipping,"shipping_status":shipping_status,
               "currency":currency,"dealer":"","grade":"","availability":"",
               "_match_text":match_text,
               "asset_type":classify_asset(match_text)[0],"product_scope":product_scope(match_text),
               "_score":score_title(match_text,payload)}
        offer.update(discount_meta)
        offers.append(offer)
    return offers


def enrich_offer_from_item_page(offer, ship_to_country="Greece", use_geo_proxy=False):
    """Best-effort item-page enrichment for real sale price and destination-visible shipping.

    use_geo_proxy controls whether ScrapingBee (paid beyond its free tier) is
    even attempted. Callers should pass False for a first, free pass over
    many candidates, and only pass True for the final handful of offers that
    actually get shown to the user — see coin_search()'s two-phase design,
    which keeps ScrapingBee usage to at most a couple of requests per search
    instead of one per candidate.

    MA-Shops item pages usually show only ONE default shipping destination —
    the seller's own base country (e.g. "+ 10,00 EUR shipping (to Germany)")
    — not necessarily the buyer's actual destination, unless the seller
    happens to match it or the page does geo-detection we can't replicate
    with a plain GET request. This reports FOUR distinct confidence levels
    instead of collapsing everything into unknown/known:
      known_target                   - confirmed for the buyer's chosen destination
      known_other_destination        - a specific OTHER destination was found
                                        (destination name is preserved)
      known_unconfirmed_destination  - a flat shipping figure was found with
                                        no destination text at all
      unknown                        - nothing reliable found

    IMPORTANT: matching is scoped to individual DOM elements that themselves
    mention shipping (short tag text, like the price-cell isolation already
    used for search-results cards), NOT the whole page flattened into one
    string. Flattening the entire page loses element boundaries, so an
    unrelated number from a "related items" widget, header, or sidebar can
    end up textually adjacent to this item's "shipping"/destination mention
    purely by coincidence — and get misread as this item's shipping cost.
    """
    url=offer.get("url")
    if not url:return offer
    print(f"[geo-fetch] enrich_offer_from_item_page ENTER url={url} target={ship_to_country} use_geo_proxy={use_geo_proxy}")
    dest_pattern,dest_iso,dest_display=destination_pattern_and_iso(ship_to_country)
    try:
        if use_geo_proxy:
            r,geo_targeted=fetch_geo_targeted(url,dest_iso)
        else:
            geo_targeted=False
            try:
                r=SESSION.get(url,timeout=12,allow_redirects=True)
            except Exception:
                r=None
        # If ScrapingBee wasn't used/available and the plain page doesn't
        # already confirm the target destination, fall back to the old
        # zero-cost URL-parameter guess as a last resort — costs nothing,
        # occasionally helps, never assumed to work.
        if r is not None and r.ok and not geo_targeted and not re.search(dest_pattern,r.text,re.I) and dest_iso:
            parsed=urlsplit(url);qs=dict(parse_qsl(parsed.query))
            for param in ("country","land","ship_to","ship_country","dest","destination"):
                if param not in qs:
                    guess_url=urlunsplit(parsed._replace(query=urlencode({**qs,param:dest_iso})))
                    try:
                        guess_resp=SESSION.get(guess_url,timeout=12,allow_redirects=True)
                        if guess_resp.ok and re.search(dest_pattern,guess_resp.text,re.I):
                            r=guess_resp
                    except Exception:
                        pass
                    break
        if r is None or not r.ok:return offer
        offer["detail_page_checked"]=True
        print(f"[geo-fetch] fetched page for target={ship_to_country}({dest_iso}) via_scrapingbee={use_geo_proxy} "
              f"contains_target_text={bool(re.search(dest_pattern,r.text,re.I))} url={url}")
        soup=BeautifulSoup(r.text,"html.parser")
        # JSON-LD product price is authoritative when present.
        j=extract_from_jsonld(soup,r.url,{"coin":{}})
        if j:
            same=next((x for x in j if x.get("price")),None)
            if same:
                offer["price"]=same["price"];offer["currency"]=same.get("currency") or offer.get("currency")

        ship_words=r"(?:shipping|postage|versand|porto|frais de port|spedizione|envio|envío)"
        # Currency marker is now MANDATORY in the captured amount — the old
        # pattern made EUR/€ optional on both sides, so a bare, unrelated
        # digit near "shipping" (a delivery-days estimate, a quantity, an
        # unrelated related-item price fragment) could be misread as money.
        money_rx=r"((?:EUR\s*|€\s*)[0-9][0-9., ]*|[0-9][0-9., ]*\s*(?:EUR|€))"
        generic_dest=r"([A-ZÀ-Ÿ][\wÀ-ÿ\-]{2,}(?:\s+[A-ZÀ-Ÿ][\wÀ-ÿ\-]{2,})?)"

        def find_money(seg):
            for pat in PRICE_PATTERNS:
                m=pat.search(seg)
                if m: return num(m.group(1))
            return None

        # Collect DOM-scoped text segments that themselves mention shipping —
        # this is what actually gets searched, instead of the whole page.
        segments=[]
        ship_word_re=re.compile(ship_words,re.I)
        for tag in soup.find_all(["p","div","span","li","td","tr","dd","section"]):
            own_text=smart_join(tag.stripped_strings)
            if not own_text or len(own_text)>400: continue
            if ship_word_re.search(own_text):
                segments.append(own_text)
        # Fall back to the flattened page only if no scoped element was found
        # (some markup keeps shipping text at a level our tag list missed);
        # still safer now that money_rx requires a currency marker.
        if not segments:
            segments=[smart_join(soup.stripped_strings)]

        def search_segments(patterns):
            for seg_text in segments:
                for pattern in patterns:
                    rx=re.search(pattern,seg_text,re.I)
                    if rx: return rx
            return None

        # 1) Target-destination-specific, amount-before-label and label-before-amount orderings.
        target_patterns=[
            rf"(?:\+\s*)?{money_rx}\s*{ship_words}\s*\(?\s*(?:to\s*)?{dest_pattern}\s*\)?",
            rf"{ship_words}\s*\(?\s*(?:to\s*)?{dest_pattern}\s*\)?[^€0-9]{{0,20}}{money_rx}",
            rf"{dest_pattern}[^€0-9]{{0,40}}?{ship_words}[^€0-9]{{0,20}}{money_rx}",
        ]
        rx=search_segments(target_patterns)
        if rx:
            found_shipping=find_money(rx.group(1))
            if found_shipping is not None:
                offer["shipping"]=found_shipping
                offer["shipping_status"]="free" if found_shipping==0 else "known_target"
                offer["shipping_destination"]=dest_display
                offer["shipping_geo_verified"]=bool(geo_targeted)
                return offer

        # 2) A specific OTHER destination was found (e.g. "to Germany" when
        # the buyer asked for Greece) — real information, just not for the
        # buyer's chosen destination. Preserve the destination name found.
        other_pattern=rf"(?:\+\s*)?{money_rx}\s*{ship_words}\s*\(?\s*(?:to|nach|vers|a)\s+{generic_dest}\s*\)?"
        rx=search_segments([other_pattern])
        if rx:
            amt=find_money(rx.group(1));found_dest=rx.group(2)
            if amt is not None and found_dest and not re.fullmatch(dest_pattern,found_dest,re.I):
                offer["shipping"]=amt
                offer["shipping_status"]="known_unconfirmed_destination" if amt==0 else "known_other_destination"
                offer["shipping_destination"]=found_dest.strip()
                return offer

        # 3) A flat shipping figure with no destination text at all.
        bare_pattern=rf"(?:\+\s*)?{money_rx}\s*{ship_words}(?!\s*\()"
        rx=search_segments([bare_pattern])
        if rx:
            amt=find_money(rx.group(1))
            if amt is not None:
                offer["shipping"]=amt
                offer["shipping_status"]="free" if amt==0 else "known_unconfirmed_destination"
                offer["shipping_destination"]=None
                return offer

        # 4) General free-shipping claim (with or without a target-destination mention).
        free_patterns=[
            rf"(?:free shipping|versandkostenfrei|portofrei|livraison gratuite)[^€0-9]{{0,40}}?{dest_pattern}",
            rf"{dest_pattern}[^€0-9]{{0,40}}?(?:free shipping|versandkostenfrei|portofrei|livraison gratuite)",
        ]
        if search_segments(free_patterns):
            offer["shipping"]=0.0;offer["shipping_status"]="free";offer["shipping_destination"]=dest_display
            return offer
        if search_segments([r"(?:free shipping|versandkostenfrei|portofrei|livraison gratuite)"]):
            offer["shipping"]=0.0;offer["shipping_status"]="free";offer["shipping_destination"]=None
            return offer

        # Nothing reliable found — never preserve a possibly-bogus search-page guess.
        offer["shipping"]=None;offer["shipping_status"]="unknown";offer["shipping_destination"]=None
        return offer
    except Exception as e:
        import traceback
        print(f"[geo-fetch] EXCEPTION in enrich_offer_from_item_page for {url}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return offer


def extract_mashops_partial_specs_from_html(html_text, source_url=None):
    """Extract only explicit physical facts from compact visible MA-Shops blocks.

    Each returned fact carries its own evidence text. We never infer a missing
    value and never scan scripts/JSON/related-product blobs as catalogue truth.
    """
    if not html_text:
        return {}
    soup=BeautifulSoup(html_text,"html.parser")
    for bad in soup(["script","style","noscript","template","svg"]):
        bad.decompose()

    label_re=re.compile(
        r"\b(?:composition|material|metal|legierung|materiale|m[ée]tal|"
        r"weight|gewicht|poids|peso|fineness|feinheit|titre|ley|"
        r"diameter|durchmesser|diam[eè]tre|diametro|ø)\b",re.I)
    blocks=[]
    for tr in soup.find_all("tr"):
        txt=smart_join(tr.stripped_strings)
        if txt and len(txt)<=320 and label_re.search(txt): blocks.append(txt)
    for node in soup.find_all(["p","li","td","span","div"]):
        # leaf-ish visible blocks only; avoids huge containers with related items
        if node.find(["p","li","td","tr","div"]): continue
        txt=smart_join(node.stripped_strings)
        if txt and len(txt)<=260 and label_re.search(txt): blocks.append(txt)
    for st in soup.stripped_strings:
        txt=str(st).strip()
        if txt and len(txt)<=240 and label_re.search(txt): blocks.append(txt)

    seen=set()
    blocks=[x for x in blocks if not (x in seen or seen.add(x))]
    out={}
    metal_aliases={
        "silver":"Silver","silber":"Silver","argent":"Silver","argento":"Silver","plata":"Silver",
        "gold":"Gold","or":"Gold","oro":"Gold",
        "platinum":"Platinum","platin":"Platinum","platine":"Platinum","platino":"Platinum",
        "palladium":"Palladium",
        "copper":"Copper","kupfer":"Copper","cuivre":"Copper","rame":"Copper","cobre":"Copper",
        "bronze":"Bronze","brass":"Brass","messing":"Brass",
        "nickel":"Nickel","zinc":"Zinc","zink":"Zinc","steel":"Steel","stahl":"Steel",
    }

    for evidence in blocks:
        # Weight
        wm=re.search(
            r"(?:weight|gewicht|poids|peso)\s*[:\-]?\s*"
            r"([0-9]+(?:[.,][0-9]+)?)\s*(?:g|gr|gram|grams|gramm)\b",
            evidence,re.I)
        if wm and "weight_g" not in out:
            v=num(wm.group(1))
            if v is not None and 0.05<=v<=2000:
                out["weight_g"]={"value":float(v),"evidence":evidence}

        # Diameter (also forms such as "Ø 38.10 mm")
        dm=re.search(
            r"(?:diameter|durchmesser|diam[eè]tre|diametro)\s*[:\-]?\s*"
            r"([0-9]+(?:[.,][0-9]+)?)\s*mm\b",evidence,re.I)
        if not dm:
            dm=re.search(r"(?:ø|Ø)\s*([0-9]+(?:[.,][0-9]+)?)\s*mm\b",evidence)
        if dm and "diameter_mm" not in out:
            v=num(dm.group(1))
            if v is not None and 3<=v<=200:
                out["diameter_mm"]={"value":float(v),"evidence":evidence}

        # Explicit composition/material only.
        cm=re.search(
            r"(?:composition|material|metal|legierung|materiale|m[ée]tal)\s*[:\-]?\s*"
            r"(.{1,110}?)(?=\s+(?:weight|gewicht|poids|peso|fineness|feinheit|titre|ley|"
            r"diameter|durchmesser|diam[eè]tre|diametro)\b|$)",
            evidence,re.I)
        comp_text=cm.group(1).strip(" .;,-") if cm else ""
        if comp_text and "primary_metal" not in out:
            for raw,canon in metal_aliases.items():
                if re.search(rf"\b{re.escape(raw)}\b",comp_text,re.I):
                    out["primary_metal"]={"value":canon,"evidence":evidence,"raw":comp_text}
                    break

        # Explicit fineness. Allow .900 / 900‰ / 0.900 / Fineness: 900.
        fin=None
        fm=re.search(r"(?<!\d)\.\s*([0-9]{3})\b",evidence)
        if fm: fin=int(fm.group(1))
        if fin is None:
            fm=re.search(r"\b([0-9]{3})\s*‰",evidence)
            if fm: fin=int(fm.group(1))
        if fin is None:
            fm=re.search(r"(?:fineness|feinheit|titre|ley)\s*[:\-]?\s*0[.,]([0-9]{3})\b",evidence,re.I)
            if fm: fin=int(fm.group(1))
        if fin is None:
            fm=re.search(r"(?:fineness|feinheit|titre|ley)\s*[:\-]?\s*([0-9]{3})\b",evidence,re.I)
            if fm: fin=int(fm.group(1))
        if fin is not None and 100<=fin<=999 and "fineness_per_mille" not in out:
            out["fineness_per_mille"]={"value":fin,"evidence":evidence}

    return out


def extract_mashops_physical_specs_from_html(html_text, source_url=None):
    """Backward-compatible strict single-listing extractor.

    A single listing is considered complete only if it explicitly contains
    metal + fineness + weight. Diameter is optional.
    """
    p=extract_mashops_partial_specs_from_html(html_text,source_url)
    if not all(k in p for k in ("primary_metal","fineness_per_mille","weight_g")):
        return None
    metal=p["primary_metal"]["value"]; fine=int(p["fineness_per_mille"]["value"])
    weight=float(p["weight_g"]["value"])
    evid=[]
    for k in ("primary_metal","fineness_per_mille","weight_g","diameter_mm"):
        if k in p and p[k].get("evidence") not in evid: evid.append(p[k].get("evidence"))
    return {
        "composition":f"{metal} (.{fine:03d})","primary_metal":metal,
        "fineness_per_mille":fine,"weight_g":weight,
        "diameter_mm":p.get("diameter_mm",{}).get("value"),
        "fine_metal_g":weight*fine/1000.0,
        "spec_source":"MA-Shops exact visible item specification",
        "data_provider":"MA-Shops","source_url":source_url,
        "source_evidence_text":" | ".join(x for x in evid if x),
    }


_MA_SPEC_CACHE = {}

def _coin_identity_key(coin):
    return "|".join(str(coin.get(k) or "").strip().lower() for k in ("countryEN","country","denom","year","variant"))

def cache_mashops_spec(coin, spec):
    if spec:
        _MA_SPEC_CACHE[_coin_identity_key(coin)] = dict(spec)

def cached_mashops_spec(coin):
    return _MA_SPEC_CACHE.get(_coin_identity_key(coin))


def _independent_evidence(items):
    """Prefer one vote per dealer; fall back to URL when dealer slug is absent."""
    out=[]; seen=set()
    for x in items:
        identity=(x.get("dealer") or "").strip().lower() or (x.get("url") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity); out.append(x)
    return out


def _cluster_numeric_evidence(items, abs_tol, rel_tol=0.0):
    """Return the strongest numeric consensus from independent dealers."""
    items=_independent_evidence(items)
    best=[]
    for anchor in items:
        av=float(anchor["value"])
        cluster=[]
        for x in items:
            xv=float(x["value"])
            tol=max(abs_tol,abs(av)*rel_tol)
            if abs(xv-av)<=tol: cluster.append(x)
        if len(cluster)>len(best): best=cluster
    if not best:return None,[]
    vals=sorted(float(x["value"]) for x in best)
    mid=vals[len(vals)//2] if len(vals)%2 else (vals[len(vals)//2-1]+vals[len(vals)//2])/2
    return mid,best


def _cluster_exact_evidence(items, normalizer=lambda x:x):
    items=_independent_evidence(items)
    groups={}
    for x in items:
        key=normalizer(x["value"])
        groups.setdefault(key,[]).append(x)
    if not groups:return None,[]
    key,grp=max(groups.items(),key=lambda kv:len(kv[1]))
    return grp[0]["value"],grp


def mashops_spec_consensus(coin, raw_query="", max_item_pages=20, min_agree=2):
    """Independent MA-Shops specification search across exact matching listings.

    Price rank is irrelevant to catalogue truth: valid item pages are inspected
    progressively until independent listings agree. Each field reaches consensus
    separately, so weight/diameter/metal/fineness need not appear in the same
    dealer listing. A field is published only with >= min_agree independent
    exact-identity listings.
    """
    cached=cached_mashops_spec(coin)
    if cached:return cached

    country=str(coin.get("countryEN") or coin.get("country") or "").strip()
    denom=str(coin.get("denom") or "").strip()
    year=str(coin.get("year") or "").strip()
    variant=str(coin.get("variant") or "").strip()
    query=(raw_query or " ".join(x for x in [country,denom,year,variant] if x)).strip()
    if not query:return None

    payload={"coin":{"country":country,"countryEN":country,"denom":denom,"year":year,"variant":variant},
             "raw_query":raw_query or query,"asset_type":"COIN"}
    offers,_,err=fetch_search(query,payload)
    if err and not offers:return None

    valid=[]; seen_urls=set()
    for o in offers:
        mt=o.get("_match_text") or o.get("title","")
        if classify_asset(mt)[0]=="BANKNOTE":continue
        if product_scope(mt)!="SINGLE_COIN":continue
        if not passes_hard_filter(mt,payload):continue
        url=o.get("url")
        if not url or url in seen_urls:continue
        seen_urls.add(url); valid.append(o)

    # Do not couple specification discovery to "two cheapest". We still start
    # from inexpensive results for deterministic ordering, but scan progressively.
    valid.sort(key=lambda o:(o.get("price") is None,o.get("price") or float("inf")))

    ev={"primary_metal":[],"fineness_per_mille":[],"weight_g":[],"diameter_mm":[]}
    checked=0
    for o in valid[:max_item_pages]:
        try:
            r=SESSION.get(o.get("url"),timeout=7,allow_redirects=True)
            checked+=1
            if not r.ok:continue
            page_soup=BeautifulSoup(r.text,"html.parser")
            h1=page_soup.find("h1")
            page_title=h1.get_text(" ",strip=True) if h1 else (o.get("title") or "")
            identity_text=" ".join(x for x in [page_title,o.get("_match_text"),o.get("title")] if x)
            if not passes_hard_filter(identity_text,payload):
                continue
            partial=extract_mashops_partial_specs_from_html(r.text,r.url)
            dealer=_dealer_slug_from_offer({"url":r.url})
            for field,item in partial.items():
                if field not in ev:continue
                ev[field].append({
                    "value":item["value"],"url":r.url,"dealer":dealer,
                    "title":page_title,"evidence":item.get("evidence","")
                })

            metal,metal_grp=_cluster_exact_evidence(ev["primary_metal"],lambda x:str(x).lower())
            fine,fine_grp=_cluster_exact_evidence(ev["fineness_per_mille"],lambda x:int(x))
            weight,weight_grp=_cluster_numeric_evidence(ev["weight_g"],abs_tol=0.08,rel_tol=0.004)
            diam,diam_grp=_cluster_numeric_evidence(ev["diameter_mm"],abs_tol=0.18,rel_tol=0.003)
            # Stop early once the four main fields have independent consensus.
            if all(len(g)>=min_agree for g in (metal_grp,fine_grp,weight_grp,diam_grp)):
                break
        except Exception as e:
            print(f"[MA-Shops consensus] item fetch failed: {type(e).__name__}: {e}",flush=True)

    metal,metal_grp=_cluster_exact_evidence(ev["primary_metal"],lambda x:str(x).lower())
    fine,fine_grp=_cluster_exact_evidence(ev["fineness_per_mille"],lambda x:int(x))
    weight,weight_grp=_cluster_numeric_evidence(ev["weight_g"],abs_tol=0.08,rel_tol=0.004)
    diam,diam_grp=_cluster_numeric_evidence(ev["diameter_mm"],abs_tol=0.18,rel_tol=0.003)

    accepted={}
    groups={}
    for field,val,grp in (
        ("primary_metal",metal,metal_grp),("fineness_per_mille",fine,fine_grp),
        ("weight_g",weight,weight_grp),("diameter_mm",diam,diam_grp)):
        if val is not None and len(grp)>=min_agree:
            accepted[field]=val; groups[field]=grp

    if not accepted:
        print(f"[MA-Shops consensus] checked={checked} exact={len(valid)} no field reached {min_agree}-source consensus",flush=True)
        return None

    if "fineness_per_mille" in accepted:
        accepted["fineness_per_mille"]=int(round(accepted["fineness_per_mille"]))
    if "weight_g" in accepted: accepted["weight_g"]=round(float(accepted["weight_g"]),4)
    if "diameter_mm" in accepted: accepted["diameter_mm"]=round(float(accepted["diameter_mm"]),3)

    if accepted.get("primary_metal") and accepted.get("fineness_per_mille"):
        accepted["composition"]=f"{accepted['primary_metal']} (.{accepted['fineness_per_mille']:03d})"
    elif accepted.get("primary_metal"):
        accepted["composition"]=accepted["primary_metal"]

    if accepted.get("weight_g") is not None and accepted.get("fineness_per_mille") is not None:
        accepted["fine_metal_g"]=accepted["weight_g"]*accepted["fineness_per_mille"]/1000.0

    evidence_by_field={}
    source_urls=[]
    for field,grp in groups.items():
        evidence_by_field[field]=[
            {"dealer":x.get("dealer"),"title":x.get("title"),"url":x.get("url"),
             "evidence":x.get("evidence"),"value":x.get("value")} for x in grp
        ]
        for x in grp:
            if x.get("url") and x["url"] not in source_urls:source_urls.append(x["url"])

    counts={k:len(v) for k,v in groups.items()}
    min_count=min(counts.values()) if counts else 0
    confidence=min(0.98,0.80+0.05*min(3,min_count))
    result={
        **accepted,
        "id":None,"title":query,"issuer":"","obverse_image":None,"reverse_image":None,
        "url":source_urls[0] if source_urls else None,
        "match_class":"MA_SHOPS_MULTI_LISTING_CONSENSUS",
        "confidence":confidence,
        "spec_source":"MA-Shops multi-listing consensus",
        "data_provider":"MA-Shops",
        "spec_evidence_counts":counts,
        "spec_evidence_by_field":evidence_by_field,
        "spec_source_urls":source_urls,
        "listings_checked":checked,
        "exact_listings_available":len(valid),
    }
    print(f"[MA-Shops consensus] checked={checked} accepted={accepted} counts={counts}",flush=True)
    cache_mashops_spec(coin,result)
    return result


# Compatibility name for older callers.
def mashops_spec_fallback(coin, raw_query=""):
    return mashops_spec_consensus(coin,raw_query)


def fetch_search(query, payload):
    """Fetch the explicit MA-Shops cheapest-first result page.

    We parse the page in its displayed order. Validation still happens later,
    so a wrong-year / wrong-denomination / set / banknote result is skipped
    rather than blindly accepted merely because it is cheap.
    """
    last_err=None
    combined=[]
    used_urls=[]
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
                combined.extend(offers)
                used_urls.append(r.url)
            else:
                last_err="No offer blocks parsed"
        except Exception as e:
            last_err=str(e)
            print(f"[MA-Shops]   -> EXCEPTION: {e}", flush=True)
    if combined:
        return combined, " | ".join(used_urls), None
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

# ---- Numista catalogue lookup: composition, weight, diameter, images. ----
# Official, documented, keyed REST API (not scraping) — https://en.numista.com/api/doc/index.php
# Auth: HTTP header "Numista-API-Key: <key>". Key is read ONLY from the
# NUMISTA_API_KEY environment variable — it must never be hardcoded here.
NUMISTA_API_KEY = os.environ.get("NUMISTA_API_KEY", "")
NUMISTA_BASE = "https://api.numista.com/v3"


# ---- CoinBids local physical-specification layer -------------------------
# Checked BEFORE Numista.  This deliberately contains only curated records
# whose physical specification is invariant for the matched issue family.
# Add records from official mints/central banks here without consuming API quota.
COIN_SPECS_PATH=os.path.join(APP_DIR,"coin_specs_database.json")
try:
    with open(COIN_SPECS_PATH,"r",encoding="utf-8") as _f:
        _COIN_SPECS=(json.load(_f) or {}).get("records",[])
except Exception as _spec_err:
    _COIN_SPECS=[]
    print(f"[coin-specs] database unavailable: {_spec_err}")

# ---- CoinBids PostgreSQL physical-specification layer ---------------------
# Checked BEFORE the small bundled local JSON above (which stays as the
# offline/no-database fallback). Backed by coin_specs, a shared REFERENCE
# table (not per-user, no RLS) loaded by coinbids_pg_loader.py from
# coinbids_database_builder_v2.py's output — currently ~591 records after
# migrating the two supplied legacy JSON databases and fixing a real bug
# found during testing (the original migrate_legacy() only kept the FIRST
# country in each record's country list, silently dropping the rest —
# see the builder's own docstring for details).
#
# DATABASE_URL is optional: if unset, or if the connection/query fails for
# ANY reason, this returns None and the existing local JSON / Numista
# fallback chain runs exactly as it did before this feature existed — a
# Postgres outage or missing configuration can never break Metal
# Intelligence, only make it use a smaller (but still real, still correct)
# fallback dataset.
DATABASE_URL=os.environ.get("DATABASE_URL","")
_PG_POOL=None
_PG_DISABLED_REASON=None

def _get_pg_connection():
    global _PG_POOL, _PG_DISABLED_REASON
    if not DATABASE_URL or _PG_DISABLED_REASON:return None
    if _PG_POOL is None:
        try:
            import psycopg2.pool
            _PG_POOL=psycopg2.pool.SimpleConnectionPool(1,5,DATABASE_URL)
        except Exception as e:
            _PG_DISABLED_REASON=f"{type(e).__name__}: {e}"
            print(f"[coin-specs-pg] disabled for this process after pool init failure: {_PG_DISABLED_REASON}")
            return None
    try:
        return _PG_POOL.getconn()
    except Exception as e:
        print(f"[coin-specs-pg] getconn failed: {type(e).__name__}: {e}")
        return None

def _release_pg_connection(conn):
    if _PG_POOL and conn is not None:
        try:_PG_POOL.putconn(conn)
        except Exception:pass

def pg_coin_spec_match(coin):
    if not DATABASE_URL:return None
    country=str(coin.get("countryEN") or coin.get("country") or "").strip()
    year_txt=str(coin.get("year") or "").strip()
    try:year=int(year_txt) if year_txt else None
    except Exception:year=None
    denom=_spec_denom_value(coin.get("denom"))
    if not country or denom is None:return None
    conn=_get_pg_connection()
    if conn is None:return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select country,currency_code,denomination_value,composition_text,
                       weight_g,diameter_mm,confidence,source_priority,verified
                from coin_specs
                where lower(country)=lower(%s)
                  and abs(denomination_value-%s)<0.0001
                  and (year_from is null or %s is null or %s>=year_from)
                  and (year_to is null or %s is null or %s<=year_to)
                order by source_priority desc, confidence desc
                limit 1
                """,
                (country,denom,year,year,year,year),
            )
            row=cur.fetchone()
            if not row:return None
            (m_country,m_currency,m_denom,m_comp,m_weight,m_diam,m_conf,m_priority,m_verified)=row
            return {
                "id":None,"title":f"{coin.get('denom','')} {coin.get('countryEN') or coin.get('country','')} {coin.get('year','')}".strip(),
                "issuer":"","composition":m_comp,"weight_g":float(m_weight) if m_weight is not None else None,
                "diameter_mm":float(m_diam) if m_diam is not None else None,"obverse_image":None,"reverse_image":None,
                "url":None,"match_class":"LOCAL_SPEC_PG","confidence":float(m_conf) if m_conf is not None else 1.0,
                "spec_source":"CoinBids PostgreSQL specifications database","data_provider":"CoinBids local specifications (PostgreSQL)"
            }
    except Exception as e:
        print(f"[coin-specs-pg] query failed: {type(e).__name__}: {e}")
        return None
    finally:
        _release_pg_connection(conn)

def _spec_denom_value(text):
    s=str(text or "").lower().replace(",",".").strip()
    m=re.search(r"(\d+(?:\.\d+)?)",s)
    if not m:return None
    v=float(m.group(1))
    if "cent" in s or "λεπτ" in s:v/=100.0
    return round(v,4)

VERIFIED_SPEC_OVERRIDES={
    ("greece",30.0,1963):{
        "composition":"Silver (.835)","weight_g":18.0,"diameter_mm":34.0,
        "spec_source":"Verified catalogue correction","data_provider":"CoinBids verified overrides",
        "source_reference":"uCoin Greece 30 drachmas 1963 / KM#86",
        "source_url":"https://en.ucoin.net/coin/greece-30-drachmas-1963/?tid=33528"
    },
    ("greece",30.0,1964):{
        "composition":"Silver (.835)","weight_g":12.0,"diameter_mm":30.3,
        "spec_source":"Verified catalogue correction","data_provider":"CoinBids verified overrides",
        "source_reference":"uCoin Greece 30 drachmas 1964 / KM#87",
        "source_url":"https://en.ucoin.net/coin/greece-30-drachmas-1964/?tid=33529"
    },
}

def verified_spec_override(coin):
    country=str(coin.get("countryEN") or coin.get("country") or "").strip().lower()
    denom=_spec_denom_value(coin.get("denom"))
    try:year=int(str(coin.get("year") or "").strip())
    except Exception:return None
    rec=VERIFIED_SPEC_OVERRIDES.get((country,denom,year))
    if not rec:return None
    out=dict(rec)
    out.update({
        "id":None,
        "title":f"{coin.get('denom','')} {coin.get('countryEN') or coin.get('country','')} {year}".strip(),
        "issuer":"","obverse_image":None,"reverse_image":None,"url":out.get("source_url"),
        "match_class":"VERIFIED_SPEC_OVERRIDE","confidence":1.0,
    })
    return out

def _safe_local_spec_record(rec):
    """Reject incomplete precious-metal catalogue records.

    A precious-metal composition without weight is not useful for melt value
    and, more importantly, may be a broad/mis-keyed legacy record. Prefer
    returning no metal data over confidently displaying a wrong fineness.
    """
    comp=str(rec.get("composition") or "").lower()
    precious=any(x in comp for x in ("silver","gold","platinum","palladium"))
    if precious and rec.get("weight_g") in (None,""):
        return False
    return True

def local_coin_spec_match(coin):
    country=str(coin.get("countryEN") or coin.get("country") or "").strip().lower()
    year_txt=str(coin.get("year") or "").strip()
    try: year=int(year_txt) if year_txt else None
    except: year=None
    denom=_spec_denom_value(coin.get("denom"))
    if not country or denom is None:return None
    matches=[]
    for r in _COIN_SPECS:
        countries=[str(x).lower() for x in r.get("countries",[])]
        if country not in countries:continue
        if abs(float(r.get("denomination",-999))-denom)>1e-6:continue
        yf=r.get("year_from");yt=r.get("year_to")
        if year is not None and ((yf is not None and year<int(yf)) or (yt is not None and year>int(yt))):continue
        if not _safe_local_spec_record(r):continue
        # Exact/small year spans beat broad legacy family records.
        span=(int(yt)-int(yf)) if yf is not None and yt is not None else 10**9
        exact=0 if year is not None and yf is not None and yt is not None and int(yf)==year==int(yt) else 1
        matches.append((exact,span,-float(r.get("confidence",1.0)),r))
    if not matches:return None
    matches.sort(key=lambda x:(x[0],x[1],x[2]))
    r=matches[0][3]
    return {
        "id":None,"title":f"{coin.get('denom','')} {coin.get('countryEN') or coin.get('country','')} {coin.get('year','')}".strip(),
        "issuer":"","composition":r.get("composition"),"weight_g":r.get("weight_g"),
        "diameter_mm":r.get("diameter_mm"),"obverse_image":None,"reverse_image":None,
        "url":None,"match_class":"LOCAL_SPEC","confidence":r.get("confidence",1.0),
        "spec_source":r.get("source","CoinBids curated specifications"),
        "data_provider":"CoinBids local specifications",
        "source_record_year_from":r.get("year_from"),"source_record_year_to":r.get("year_to"),
    }

def numista_search(query, category="coin", count=12, year=None):
    if not NUMISTA_API_KEY:return None,"NUMISTA_API_KEY is not configured on the server."
    params={"q":query,"count":count,"lang":"en"}
    # Official v3 search accepts year/date; category remains as a compatibility hint.
    if year:params["year"]=year
    if category:params["category"]=category
    r,transport_err=_numista_get_with_backoff(f"{NUMISTA_BASE}/types",params=params,timeout=15)
    if transport_err:return None,transport_err
    if r.status_code!=200:return None,f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        data=r.json();types=data.get("types")
        if types is None and isinstance(data.get("data"),dict):types=data["data"].get("types")
        return types or [],None
    except Exception as e:return None,str(e)

# ---- Numista caching policy (IMPORTANT — read before changing) -----------
# Per the Numista API Terms of Use §8.1: "Licensed Data must not be
# persistently stored, archived or cached" except (§8.2) Numista
# identifiers themselves with no time limit, and (§8.3) the narrowly
# defined "Catalogue Metadata" (issuers/mints/catalogues LIST endpoints
# only — NOT type/issue physical-spec data) for up to 7 days. §8.4's
# "Personal Project" exception does not apply — CoinBids is a public
# service, not a private non-commercial individual project.
#
# type/issues detail (composition, weight, diameter, images) does not fall
# under any of those exceptions, so it must NOT be cached across time. A
# previous version of this file kept a 24-hour TTL cache here — that was a
# real compliance gap, found and removed. What remains below is NOT a
# cache: it only coalesces genuinely CONCURRENT requests for the same
# type_id (e.g. two people looking up the same coin in the same instant)
# so only one real API call is made for that instant, with the result held
# in memory just long enough to hand to the other in-flight callers, then
# discarded. A request arriving even a moment later always triggers a
# fresh API call, exactly like any single request would.
_NUMISTA_INFLIGHT={}  # key -> {"event":threading.Event, "result":(data,err)|None}
_NUMISTA_INFLIGHT_LOCK=threading.Lock()
_NUMISTA_INFLIGHT_WAIT_SECONDS=20

def _numista_single_flight(key,fetch_fn):
    is_leader=False
    with _NUMISTA_INFLIGHT_LOCK:
        entry=_NUMISTA_INFLIGHT.get(key)
        if entry is None:
            entry={"event":threading.Event(),"result":None}
            _NUMISTA_INFLIGHT[key]=entry
            is_leader=True
    if not is_leader:
        entry["event"].wait(timeout=_NUMISTA_INFLIGHT_WAIT_SECONDS)
        if entry["result"] is not None:
            return entry["result"]
        # The leader failed or didn't finish within the wait window — fetch
        # independently rather than permanently fail every follower.
    data,err=fetch_fn()
    if is_leader:
        entry["result"]=(data,err)
        with _NUMISTA_INFLIGHT_LOCK:
            _NUMISTA_INFLIGHT.pop(key,None)
        entry["event"].set()
    return data,err

# ---- Rate-limit safety: bounded retry with backoff on HTTP 429 only ----
# Numista's own documentation (https://en.numista.com/api/doc/index.php)
# confirms a 429-style "too many simultaneous requests / reached your
# monthly quota" response exists, but does not document specific rate-limit
# header names — so this honors the standard HTTP `Retry-After` header when
# present (a safe, widely-used convention, not a Numista-specific
# assumption) and otherwise falls back to a short, strictly bounded
# exponential backoff. Never retries on any status other than 429, and never
# more than _NUMISTA_MAX_RETRIES extra attempts — this is deliberately NOT
# an aggressive retry loop.
_NUMISTA_MAX_RETRIES=2
_NUMISTA_BACKOFF_BASE_SECONDS=1.0
_NUMISTA_BACKOFF_MAX_SECONDS=10.0

def _numista_get_with_backoff(url,params=None,timeout=15):
    attempt=0
    while True:
        try:
            r=requests.get(url,params=params,headers={"Numista-API-Key":NUMISTA_API_KEY},timeout=timeout)
        except Exception as e:
            return None,str(e)
        if r.status_code!=429 or attempt>=_NUMISTA_MAX_RETRIES:
            return r,None
        retry_after=r.headers.get("Retry-After")
        try:
            wait_s=float(retry_after) if retry_after is not None else _NUMISTA_BACKOFF_BASE_SECONDS*(2**attempt)
        except (TypeError,ValueError):
            wait_s=_NUMISTA_BACKOFF_BASE_SECONDS*(2**attempt)
        time.sleep(min(wait_s,_NUMISTA_BACKOFF_MAX_SECONDS))
        attempt+=1

def numista_get_type(type_id):
    def _fetch():
        r,transport_err=_numista_get_with_backoff(f"{NUMISTA_BASE}/types/{type_id}",timeout=15)
        if transport_err:return None,transport_err
        if r.status_code!=200:return None,f"HTTP {r.status_code}: {r.text[:200]}"
        try:
            data=r.json()
        except Exception as e:return None,str(e)
        return data,None
    return _numista_single_flight(("type",type_id),_fetch)

def numista_get_issues(type_id):
    def _fetch():
        r,transport_err=_numista_get_with_backoff(f"{NUMISTA_BASE}/types/{type_id}/issues",timeout=15)
        if transport_err:return [],transport_err
        if r.status_code!=200:return [],f"HTTP {r.status_code}"
        try:
            d=r.json()
        except Exception as e:return [],str(e)
        if isinstance(d,list):issues=d
        else:issues=d.get("issues") or (d.get("data",{}).get("issues") if isinstance(d.get("data"),dict) else []) or []
        return issues,None
    return _numista_single_flight(("issues",type_id),_fetch)

def numista_pick(d,*paths):
    for path in paths:
        node=d;ok=True
        for key in path.split("."):
            if isinstance(node,dict) and key in node:node=node[key]
            else:ok=False;break
        if ok and node is not None:return node
    return None

def flatten_text(x):
    if x is None:return ""
    if isinstance(x,(str,int,float)):return str(x)
    if isinstance(x,dict):return " ".join(flatten_text(v) for v in x.values())
    if isinstance(x,list):return " ".join(flatten_text(v) for v in x)
    return str(x)

def numista_search_prefilter_rank(target, cand):
    """Cheap filter/rank using ONLY fields the /types SEARCH result itself
    provides — id, title, category, issuer{code,name}, thumbnails. These
    fields are confirmed present on a real, unmodified /types search
    response (verified against an actual captured Numista API response, not
    assumed). Runs BEFORE any /types/{id} detail call, to avoid spending a
    detail request on candidates that are already clearly the wrong asset
    type or wrong country.

    Denomination and year are deliberately NOT filtered here: they are not
    confirmed to be present on the bare search-result item (only confirmed
    on the /types/{id} detail response), so this stage never rejects a
    candidate for lacking data it may simply not have been given — only
    ranks/filters using fields actually present, per instruction. Detail-
    level denomination/year checks still happen afterward in
    validate_numista_detail_precheck / validate_numista_detail, unchanged.

    Returns (keep: bool, rank_score: float) — higher rank score means a
    stronger candidate, used to decide which candidates are worth spending a
    /types/{id} call on when there are more than NUMISTA_MAX_DETAIL_CANDIDATES."""
    category=norm(str(numista_pick(cand,"category") or ""))
    if category and category in ("banknote","exonumia"):
        return False,0.0
    country=target.get("countryEN") or target.get("country") or ""
    issuer_name=flatten_text(numista_pick(cand,"issuer.name","issuer") or "")
    title=flatten_text(numista_pick(cand,"title") or "")
    rank=0.0
    if country:
        if issuer_name and canonical_country(country)==canonical_country(issuer_name):
            rank+=1.0
        elif country_in_title(country,norm(title)):
            rank+=0.5
        # No usable issuer/title country signal on this search-result item —
        # rank stays 0 (lowest priority), but the candidate is still KEPT:
        # absence of a signal is not evidence of a wrong country.
    return True,rank

NUMISTA_MAX_DETAIL_CANDIDATES=6

def validate_numista_detail_precheck(target, detail):
    """Cheap pre-check using ONLY the /types/{id} detail payload already in
    hand — country/issuer and denomination — used to decide whether it's
    worth also paying for a /types/{id}/issues call. A candidate that's
    already clearly the wrong country or wrong denomination doesn't need its
    per-year issue data fetched just to be rejected a moment later; this cuts
    a real fraction of Numista API calls on typical multi-candidate,
    mixed-relevance /types text-search results without changing which
    candidates ultimately validate (validate_numista_detail below still runs
    its own full check, including this same country/denomination logic, on
    whatever survives this gate — this is a call-avoidance filter, not a
    replacement for it)."""
    reasons=[]
    typ=norm(str(numista_pick(detail,"object_type","category","type") or ""))
    if typ and any(x in typ for x in ("banknote","note","exonumia")):reasons.append("WRONG_ASSET")
    country=target.get("countryEN") or target.get("country") or ""
    blob=norm(flatten_text(detail))
    issuer=flatten_text(numista_pick(detail,"issuer","issuer.name") or "")
    if country and canonical_country(country)!=canonical_country(issuer):
        if not country_in_title(country,blob):reasons.append("WRONG_ISSUER")
    denom=target.get("denom") or target.get("denomination") or ""
    title=flatten_text(numista_pick(detail,"title") or "")
    if denom:
        detail_value=flatten_text(numista_pick(detail,"value","value.text","denomination","denomination.text") or "")
        if not denomination_matches(denom," ".join([title,detail_value])):
            reasons.append("WRONG_DENOMINATION")
    return reasons

def validate_numista_detail(target, detail, issues):
    reasons=[];score=0.0
    blob=norm(flatten_text(detail))
    # Asset category/object type
    typ=norm(str(numista_pick(detail,"object_type","category","type") or ""))
    if typ and any(x in typ for x in ("banknote","note","exonumia")):reasons.append("WRONG_ASSET")
    country=target.get("countryEN") or target.get("country") or ""
    issuer=flatten_text(numista_pick(detail,"issuer","issuer.name") or "")
    if country and canonical_country(country)!=canonical_country(issuer):
        # Some details encode issuer deeper; title/blob fallback is permitted only if alias is visible.
        if not country_in_title(country,blob):reasons.append("WRONG_ISSUER")
    else:score+=.25
    denom=target.get("denom") or target.get("denomination") or ""
    title=flatten_text(numista_pick(detail,"title") or "")
    if denom:
        detail_value=flatten_text(numista_pick(detail,"value","value.text","denomination","denomination.text") or "")
        if not denomination_matches(denom," ".join([title,detail_value])):
            reasons.append("WRONG_DENOMINATION")
        else:score+=.30
    year=str(target.get("year") or "").strip()
    if year:
        miny=numista_pick(detail,"min_year");maxy=numista_pick(detail,"max_year")
        try:
            if miny is not None and int(year)<int(miny):reasons.append("WRONG_YEAR")
            if maxy is not None and int(year)>int(maxy):reasons.append("WRONG_YEAR")
        except Exception:pass
        if issues:
            iblob=" ".join(flatten_text(x) for x in issues)
            if not re.search(rf"(?<!\d){re.escape(year)}(?!\d)",iblob):reasons.append("NO_ISSUE_FOR_YEAR")
            else:score+=.25
        elif re.search(rf"(?<!\d){re.escape(year)}(?!\d)",blob):score+=.15
    variant=target.get("variant") or ""
    if variant:
        if not variant_matches(variant," ".join([title,blob])):reasons.append("WRONG_VARIANT")
        else:score+=.20
    return list(dict.fromkeys(reasons)),min(1.0,score)


def _spec_has_any(rec):
    return bool(rec and any(rec.get(k) not in (None,"") for k in
        ("composition","primary_metal","fineness_per_mille","weight_g","diameter_mm")))

def _spec_value_close(field,a,b):
    if a in (None,"") or b in (None,""): return False
    try:
        if field=="weight_g":
            av=float(a);bv=float(b)
            return abs(av-bv)<=max(0.08,0.004*max(abs(av),abs(bv)))
        if field=="diameter_mm":
            av=float(a);bv=float(b)
            return abs(av-bv)<=max(0.18,0.003*max(abs(av),abs(bv)))
        if field=="fineness_per_mille":
            return int(round(float(a)))==int(round(float(b)))
    except Exception:
        return False
    return str(a).strip().lower()==str(b).strip().lower()


def _ma_field_strength(ma,field):
    counts=ma.get("spec_evidence_counts") or {}
    count=int(counts.get(field) or 0)
    evidence=(ma.get("spec_evidence_by_field") or {}).get(field) or []
    dealers=len({(x.get("dealer") or x.get("url") or "").strip().lower() for x in evidence
                 if (x.get("dealer") or x.get("url"))})
    return max(count,dealers)


def _resolve_primary_spec_records(source1, source2, source1_provider=None):
    """Resolve specifications per field instead of blindly preferring one row.

    Priority:
      * hand-verified CoinBids override: authoritative and never auto-overwritten;
      * otherwise a >=2-independent-dealer MA-Shops consensus may correct a
        conflicting local/PostgreSQL catalogue field;
      * single-source MA-Shops evidence never overrides an existing local value;
      * missing local fields may be filled by accepted MA consensus.

    The result records per-field provenance and conflicts for auditability.
    """
    if not source1 and not source2:return {}
    if not source1:
        out=dict(source2 or {})
        out["field_provenance"]={
            k:{"winner":"ma_shops_consensus","evidence_count":_ma_field_strength(source2,k)}
            for k in ("primary_metal","fineness_per_mille","weight_g","diameter_mm")
            if source2.get(k) not in (None,"")
        }
        return out
    if not source2:
        out=dict(source1)
        out["field_provenance"]={
            k:{"winner":source1_provider or "coinbids_catalogue"}
            for k in ("primary_metal","fineness_per_mille","weight_g","diameter_mm")
            if source1.get(k) not in (None,"")
        }
        return out

    out=dict(source1)
    verified=(source1_provider=="verified_override" or
              source1.get("match_class")=="VERIFIED_SPEC_OVERRIDE")
    field_prov={}
    conflicts=[]

    # Derive source1 metal/fineness from composition where the old catalogue
    # stores only a composition string.
    comp1=str(source1.get("composition") or "")
    if out.get("primary_metal") in (None,""):
        for token,canon in (("silver","Silver"),("gold","Gold"),("platinum","Platinum"),
                            ("palladium","Palladium"),("copper","Copper"),
                            ("bronze","Bronze"),("nickel","Nickel")):
            if token in comp1.lower():
                out["primary_metal"]=canon;break
    if out.get("fineness_per_mille") in (None,""):
        m=re.search(r"\.\s*([0-9]{3})\b|\b([0-9]{3})\s*‰",comp1)
        if m:
            out["fineness_per_mille"]=int(next(x for x in m.groups() if x))

    for field in ("primary_metal","fineness_per_mille","weight_g","diameter_mm"):
        local=out.get(field)
        ma=source2.get(field)
        strength=_ma_field_strength(source2,field)

        if ma in (None,""):
            if local not in (None,""):
                field_prov[field]={"winner":source1_provider or "coinbids_catalogue"}
            continue

        if local in (None,""):
            if strength>=2:
                out[field]=ma
                field_prov[field]={"winner":"ma_shops_consensus","evidence_count":strength,
                                   "reason":"filled_missing_catalogue_field"}
            else:
                field_prov[field]={"winner":source1_provider or "coinbids_catalogue",
                                   "note":"MA-Shops evidence below consensus threshold"}
            continue

        if _spec_value_close(field,local,ma):
            # Agreement increases confidence but does not change the value.
            field_prov[field]={"winner":source1_provider or "coinbids_catalogue",
                               "confirmed_by":"ma_shops_consensus",
                               "evidence_count":strength}
            continue

        # Real conflict.
        conflict={"field":field,"catalogue_value":local,"ma_shops_value":ma,
                  "ma_shops_evidence_count":strength}
        if verified:
            conflict["resolution"]="kept_verified_override"
            field_prov[field]={"winner":"verified_override","evidence_count":strength,
                               "conflict":True}
        elif strength>=2:
            out[field]=ma
            conflict["resolution"]="ma_shops_consensus_override"
            field_prov[field]={"winner":"ma_shops_consensus","evidence_count":strength,
                               "overrode":source1_provider or "coinbids_catalogue",
                               "conflict":True}
        else:
            conflict["resolution"]="kept_catalogue_insufficient_ma_evidence"
            field_prov[field]={"winner":source1_provider or "coinbids_catalogue",
                               "conflict":True,"evidence_count":strength}
        conflicts.append(conflict)

    # Rebuild dependent values from the resolved winning fields. Never keep a
    # stale fine-metal amount calculated from a losing weight/fineness record.
    metal=out.get("primary_metal")
    fine=out.get("fineness_per_mille")
    weight=out.get("weight_g")
    if metal:
        out["composition"]=f"{metal} (.{int(fine):03d})" if fine not in (None,"") else str(metal)
    if weight not in (None,"") and fine not in (None,""):
        out["fine_metal_g"]=float(weight)*float(fine)/1000.0
    else:
        out["fine_metal_g"]=None

    # Carry MA evidence to the UI even when the catalogue won some fields.
    out["spec_evidence_counts"]=source2.get("spec_evidence_counts") or {}
    out["spec_evidence_by_field"]=source2.get("spec_evidence_by_field") or {}
    out["spec_source_urls"]=source2.get("spec_source_urls") or []
    out["listings_checked"]=source2.get("listings_checked")
    out["exact_listings_available"]=source2.get("exact_listings_available")
    out["field_provenance"]=field_prov
    out["spec_conflicts"]=conflicts

    chain=[]
    for rec,label in ((source1,source1_provider or "coinbids_catalogue"),
                      (source2,"ma_shops_consensus")):
        chain.append({
            "provider":label,
            "source":rec.get("spec_source"),
            "url":rec.get("source_url") or rec.get("url"),
            "evidence_counts":rec.get("spec_evidence_counts"),
        })
    out["source_chain"]=chain
    out["spec_source"]="CoinBids field-level evidence resolution"
    out["data_provider"]="CoinBids resolved specifications"
    out["match_class"]="COINBIDS_FIELD_EVIDENCE_RESOLUTION"
    out["confidence"]=max(float(source1.get("confidence") or 0),
                          float(source2.get("confidence") or 0))
    return out


def _merge_spec_records(primary, secondary):
    """External fallback merger: fill only missing fields, never overwrite sources 1+2."""
    if not primary:return dict(secondary or {})
    out=dict(primary)
    if not secondary:return out
    for k in ("composition","primary_metal","fineness_per_mille","weight_g","diameter_mm",
              "fine_metal_g","obverse_image","reverse_image"):
        if out.get(k) in (None,"") and secondary.get(k) not in (None,""):
            out[k]=secondary.get(k)
    chain=list(out.get("source_chain") or [])
    src={"provider":secondary.get("data_provider") or secondary.get("provider"),
         "source":secondary.get("spec_source"),
         "url":secondary.get("source_url") or secondary.get("url")}
    if src not in chain:chain.append(src)
    out["source_chain"]=chain
    # Recompute fine metal if external fallback filled a missing component.
    if out.get("weight_g") not in (None,"") and out.get("fineness_per_mille") not in (None,""):
        out["fine_metal_g"]=float(out["weight_g"])*float(out["fineness_per_mille"])/1000.0
    return out

def _spec_complete_for_melt(rec):
    if not rec:return False
    comp=str(rec.get("composition") or rec.get("primary_metal") or "").lower()
    precious=any(x in comp for x in ("silver","gold","platinum","palladium"))
    if precious:
        return rec.get("weight_g") not in (None,"") and (
            rec.get("fineness_per_mille") not in (None,"") or
            re.search(r"(?:\.|0[.,])\d{3}\b|\b\d{3}\s*‰",comp))
    return bool(comp and rec.get("weight_g") not in (None,""))

@app.post("/api/coin-lookup")
def coin_lookup():
    payload=request.get_json(silent=True) or {};coin=payload.get("coin") or {}
    query=" ".join(str(x) for x in [coin.get("countryEN") or coin.get("country"),coin.get("denom"),coin.get("year"),coin.get("variant")] if x).strip()
    if not query:query=(payload.get("raw_query") or "").strip()
    if not query:return jsonify({"error":"empty query"}),400

    # SOURCE 1 — CoinBids verified/persistent catalogue.
    # A hand-verified correction is the strongest record; otherwise PostgreSQL,
    # then the packaged local catalogue. Source #1 is never overwritten by a
    # marketplace or an external fallback.
    source1=verified_spec_override(coin)
    source1_provider="verified_override" if source1 else None
    if not source1:
        source1=pg_coin_spec_match(coin)
        source1_provider="local_pg" if source1 else None
    if not source1:
        source1=local_coin_spec_match(coin)
        source1_provider="local" if source1 else None

    # SOURCE 2 — independent MA-Shops specification search.
    # This is deliberately NOT tied to the first/second cheapest offer.
    # It scans exact-identity listings progressively and publishes each field
    # only after >=2 independent listings agree.
    source2=mashops_spec_consensus(coin,(payload.get("raw_query") or "").strip())
    baseline=_resolve_primary_spec_records(source1,source2,source1_provider)

    if _spec_has_any(baseline):
        print(f"[coin-lookup] baseline source1={source1_provider} ma_consensus={bool(source2)} "
              f"composition={baseline.get('composition')} weight={baseline.get('weight_g')} "
              f"diameter={baseline.get('diameter_mm')} conflicts={baseline.get('spec_conflicts') or []}",flush=True)

    # If sources 1+2 already give a complete usable specification, stop here.
    # External sources are verification/fill-in layers, never prerequisites.
    if _spec_complete_for_melt(baseline) and baseline.get("diameter_mm") not in (None,""):
        return jsonify({"match":baseline,"provider":"coinbids_primary",
                        "fallback_used":False,
                        "note":"Resolved from CoinBids catalogue and/or MA-Shops multi-listing consensus."})

    # SOURCES 3/4 are intentionally optional integrations. They must never make
    # a good result from sources 1/2 disappear. Source 5 (Numista) is attempted
    # only as a fill/verification layer when configured/quota is available.
    # If it fails (quota, timeout, no match), return the best baseline from 1+2.
    results,err=numista_search(query,category="coin",count=12,year=coin.get("year"))
    if err:
        if _spec_has_any(baseline):
            return jsonify({"match":baseline,"provider":"coinbids_primary_fallback",
                            "fallback_used":True,"external_error":err,
                            "note":"External catalogue fallback failed; returning verified CoinBids/MA-Shops evidence."}),200
        return jsonify({"match":None,"error":err}),200
    survivors=[];rejected=[]
    numista_calls_saved=0
    # Stage 1: filter/rank using ONLY the /types search-result fields
    # (id/title/category/issuer) — no /types/{id} call spent yet. Clearly
    # wrong asset types are dropped outright; the rest are ranked by country
    # match strength so the strongest candidates get the limited detail-call
    # budget below (see numista_search_prefilter_rank for exactly which
    # fields this uses and why denomination/year are intentionally excluded
    # from this stage).
    ranked=[]
    for cand in results[:12]:
        keep,rank=numista_search_prefilter_rank(coin,cand)
        if not keep:
            tid=numista_pick(cand,"id")
            rejected.append({"id":tid,"title":numista_pick(cand,"title"),"reasons":["WRONG_ASSET_PRESEARCH"]})
            numista_calls_saved+=1
            continue
        ranked.append((rank,cand))
    ranked.sort(key=lambda x:x[0],reverse=True)
    top_candidates=[c for _,c in ranked[:NUMISTA_MAX_DETAIL_CANDIDATES]]
    numista_calls_saved+=max(0,len(ranked)-len(top_candidates))

    # Stage 2: for the (now much smaller) surviving candidate set, fetch
    # /types/{id} detail, cheaply gate on it before /issues, then fully
    # validate — unchanged from before, just running over fewer candidates.
    for cand in top_candidates:
        tid=numista_pick(cand,"id")
        if tid is None:continue
        detail,derr=numista_get_type(tid)
        if derr or not detail:continue
        # Cheap gate BEFORE paying for a /types/{id}/issues call: country and
        # denomination mismatches are already visible from the type detail
        # alone (a /types text search commonly returns loosely-related hits
        # across several countries/denominations). Skipping the issues call
        # for these cuts real API usage without changing which candidates
        # ultimately validate.
        precheck_reasons=validate_numista_detail_precheck(coin,detail)
        if precheck_reasons:
            rejected.append({"id":tid,"title":numista_pick(detail,"title") or numista_pick(cand,"title"),"reasons":precheck_reasons})
            numista_calls_saved+=1
            continue
        issues,_=numista_get_issues(tid)
        reasons,confidence=validate_numista_detail(coin,detail,issues)
        if reasons:
            rejected.append({"id":tid,"title":numista_pick(detail,"title") or numista_pick(cand,"title"),"reasons":reasons})
            continue
        survivors.append((confidence,detail,cand,issues))
    survivors.sort(key=lambda x:x[0],reverse=True)
    if not survivors:
        if _spec_has_any(baseline):
            return jsonify({"match":baseline,"provider":"coinbids_primary_fallback",
                            "fallback_used":True,
                            "note":"No reliable external catalogue match; returning CoinBids/MA-Shops evidence.",
                            "rejected":rejected[:8]})
        return jsonify({"match":None,"note":"No reliable external catalogue match satisfied country + denomination + year.","rejected":rejected[:8]})
    # Multiple validated candidates with no explicit variant = ambiguous, do not choose arbitrarily.
    if len(survivors)>1 and not (coin.get("variant") or "").strip():
        candidates=[]
        for conf,detail,cand,_ in survivors[:5]:
            tid=numista_pick(detail,"id") or numista_pick(cand,"id")
            candidates.append({"id":tid,"title":numista_pick(detail,"title") or numista_pick(cand,"title"),"url":f"https://en.numista.com/catalogue/pieces{tid}.html","confidence":conf})
        if _spec_has_any(baseline):
            baseline["external_crosscheck_ambiguous"]=True
            return jsonify({"match":baseline,"provider":"coinbids_primary_fallback",
                            "fallback_used":True,"external_candidates":candidates,
                            "note":"External catalogue was ambiguous; returning CoinBids/MA-Shops evidence."})
        return jsonify({"match":None,"ambiguous":True,"candidates":candidates,"note":"Multiple exact catalogue candidates remain; specify the issue/theme."})
    conf,detail,best,issues=survivors[0];tid=numista_pick(detail,"id") or numista_pick(best,"id")
    wanted_year=str(coin.get("year") or "").strip()
    issue_match=None
    if wanted_year and issues:
        for issue in issues:
            if re.search(rf"(?<!\\d){re.escape(wanted_year)}(?!\\d)",flatten_text(issue)):
                issue_match=issue;break
    composition=numista_pick(detail,"composition.text","composition")
    if issue_match: composition=numista_pick(issue_match,"composition.text","composition") or composition
    if isinstance(composition,(dict,list)):composition=flatten_text(composition)
    weight=numista_pick(detail,"weight","weight_g")
    diameter=numista_pick(detail,"size","diameter","diameter_mm")
    if issue_match:
        weight=numista_pick(issue_match,"weight","weight_g") or weight
        diameter=numista_pick(issue_match,"size","diameter","diameter_mm") or diameter
    spec_url=f"https://en.numista.com/catalogue/pieces{tid}.html"
    provenance="Numista API issue record for requested year" if issue_match else "Numista API type detail"
    external_match={
        "id":tid,"title":numista_pick(detail,"title") or numista_pick(best,"title"),
        "issuer":numista_pick(detail,"issuer.name") or flatten_text(numista_pick(detail,"issuer")),
        "composition":composition,"weight_g":weight,"diameter_mm":diameter,
        "obverse_image":numista_pick(detail,"obverse.picture","obverse_picture","obverse.thumbnail"),
        "reverse_image":numista_pick(detail,"reverse.picture","reverse_picture","reverse.thumbnail"),
        "url":spec_url,"spec_source":provenance,"data_provider":"Numista",
        "source_reference":spec_url,
        "match_class":"EXACT" if conf>=.75 else "STRONG","confidence":conf
    }
    final_match=_merge_spec_records(baseline,external_match) if _spec_has_any(baseline) else external_match
    print(f"[coin-lookup] provider={'coinbids+numista' if _spec_has_any(baseline) else 'numista'} "
          f"source={spec_url} composition={final_match.get('composition')} weight={final_match.get('weight_g')}",flush=True)
    return jsonify({"provider":"coinbids_plus_external" if _spec_has_any(baseline) else "numista",
                    "match":final_match,"fallback_used":False})

@app.post("/api/resolve-coin")
def resolve_coin():
    """Shared Coin Identity Resolver, used by Identify Coin, Price Research
    and Auction Intelligence so all three interpret spelling/transliteration/
    currency-naming variation the same way. Never invents a country when the
    input is genuinely ambiguous (e.g. bare "2 euro 2025") — returns a
    review/unresolved status with candidates instead.

    Before running the normal resolver pipeline, checks for an exact-
    normalized-text manual correction (see resolver_corrections.py) — the
    feedback-loop mechanism where a deliberate, reasoned override ("this
    alias means X") takes precedence over the automatic resolver for that
    specific input. This is NOT automatic learning from ordinary usage; a
    correction only exists if someone explicitly submitted one via
    /api/resolver/corrections (which itself requires a shared secret)."""
    if not RESOLVER_AVAILABLE:
        return jsonify({"status":"unresolved","ambiguous":False,"best":None,"candidates":[],
                        "error":"Coin identity resolver is not available on this server."}),200
    payload=request.get_json(silent=True) or {}
    text=(payload.get("text") or "").strip()
    if not text:
        return jsonify({"error":"empty text"}),400
    if CORRECTIONS_AVAILABLE:
        try:
            override=get_corrections_store().get_override(text)
        except Exception as e:
            override=None
            print(f"[corrections] EXCEPTION checking override for {text!r}: {type(e).__name__}: {e}")
        if override:
            return jsonify({"status":"resolved","ambiguous":False,"best":override["corrected"],
                            "candidates":[override["corrected"]],"source":"manual_correction",
                            "correction_reason":override.get("reason"),"correction_id":override.get("id")})
    try:
        result=resolve_coin_identity(text)
        return jsonify(result)
    except Exception as e:
        print(f"[resolver] EXCEPTION resolving {text!r}: {type(e).__name__}: {e}")
        return jsonify({"status":"unresolved","ambiguous":False,"best":None,"candidates":[],
                        "error":str(e)}),200

# =============================================================================
# RESOLVER CORRECTIONS — persistent feedback loop (see resolver_corrections.py
# for full design notes, including the honest caveat about ephemeral disk on
# free hosting tiers). Write endpoints require a shared secret
# (COINBIDS_CORRECTIONS_SECRET) since this app has no other admin/auth system
# — without that env var configured, write access is disabled entirely
# rather than left open to the public internet by default.
# =============================================================================
CORRECTIONS_SECRET=os.environ.get("COINBIDS_CORRECTIONS_SECRET","")

def _corrections_write_guard():
    if not CORRECTIONS_AVAILABLE:
        return jsonify({"error":"Resolver corrections store is not available on this server."}),200
    if not CORRECTIONS_SECRET:
        return jsonify({"error":"Corrections are disabled: COINBIDS_CORRECTIONS_SECRET is not configured on this server."}),403
    supplied=request.headers.get("X-Corrections-Secret","")
    if supplied!=CORRECTIONS_SECRET:
        return jsonify({"error":"Invalid or missing X-Corrections-Secret header."}),403
    return None

@app.post("/api/resolver/corrections")
def add_resolver_correction():
    guard=_corrections_write_guard()
    if guard:return guard
    payload=request.get_json(silent=True) or {}
    try:
        record=get_corrections_store().add(
            raw_input=payload.get("raw_input") or "",
            corrected=payload.get("corrected") or {},
            reason=payload.get("reason") or "",
            source_url=payload.get("source_url"),
            submitted_by=payload.get("submitted_by"),
        )
        return jsonify(record)
    except ValueError as e:
        return jsonify({"error":str(e)}),400
    except Exception as e:
        print(f"[corrections] EXCEPTION adding correction: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.get("/api/resolver/corrections")
def list_resolver_corrections():
    # Reading the list also requires the secret — corrections can include
    # notes/URLs the owner may not want publicly browsable, and read access
    # isn't needed by the ordinary resolver flow (which only ever looks up
    # ONE exact key at a time via get_override, not the full list).
    guard=_corrections_write_guard()
    if guard:return guard
    if not CORRECTIONS_AVAILABLE:
        return jsonify({"error":"Resolver corrections store is not available on this server."}),200
    try:
        return jsonify({"corrections":get_corrections_store().list_all()})
    except Exception as e:
        print(f"[corrections] EXCEPTION listing corrections: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.delete("/api/resolver/corrections/<correction_id>")
def delete_resolver_correction(correction_id):
    guard=_corrections_write_guard()
    if guard:return guard
    try:
        ok=get_corrections_store().delete(correction_id)
        return jsonify({"deleted":ok})
    except Exception as e:
        print(f"[corrections] EXCEPTION deleting correction {correction_id!r}: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.post("/api/validate-issue")
def validate_issue():
    """Coin Intelligence Core: resolves identity AND reports whether that exact
    country+currency+denomination+year combination is confirmed in the local
    issue seed database, or simply not yet present there (never treated as
    'does not exist' — see coin_identity_resolver._issue_validation)."""
    if not RESOLVER_AVAILABLE:
        return jsonify({"error":"Coin identity resolver is not available on this server."}),200
    payload=request.get_json(silent=True) or {}
    raw=(payload.get("text") or payload.get("raw_query") or "").strip()
    if not raw:
        return jsonify({"error":"Missing text"}),400
    try:
        result=resolve_coin_identity(raw)
        best=result.get("best")
        return jsonify({
            "identity":result,
            "issue_validation":best.get("issue_validation") if best else None
        })
    except Exception as e:
        print(f"[resolver] EXCEPTION validating issue for {raw!r}: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.post("/api/listing-match-score")
def listing_match_score_api():
    """Coin Intelligence Core: 0-100 explainable identity score for a single
    marketplace listing title against a resolved (or externally supplied)
    target identity. Supplementary ranking/evidence signal only — it never
    overrides the existing hard filters (passes_hard_filter) used to decide
    whether a listing is shown at all."""
    if not RESOLVER_AVAILABLE:
        return jsonify({"error":"Coin identity resolver is not available on this server."}),200
    payload=request.get_json(silent=True) or {}
    target=payload.get("target")
    raw=payload.get("target_text")
    if not target and raw:
        try:
            target=resolve_coin_identity(raw).get("best")
        except Exception as e:
            return jsonify({"error":str(e)}),200
    title=(payload.get("listing_title") or "").strip()
    if not target or not title:
        return jsonify({"error":"target/target_text and listing_title are required"}),400
    try:
        return jsonify(get_resolver().listing_match_score(target,title))
    except Exception as e:
        print(f"[resolver] EXCEPTION scoring listing {title!r}: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

# =============================================================================
# AUCTION INTELLIGENCE 3.0 — stateless endpoints (Phase 8b/§40).
# The frontend holds all state (comparables list, snapshot) and passes it
# back in each call; the backend never stores an Auction Intelligence
# session. This matches the existing CoinBids architecture (no server-side
# session store) and keeps these endpoints trivially cacheable/scalable.
#
# NOTE: no automated external auction-source scraping is performed by any of
# these endpoints — only the Manual and CSV adapters are enabled. See
# auction_source_matrix.md for the researched reasoning (CoinArchives and
# acsearch.info both explicitly prohibit automated harvesting in their own
# Terms of Service; Biddr and NumisBids have no documented permission/API).
# =============================================================================

_AUCTION_COMPARABLE_FIELDS=None
def _comparable_from_dict(d):
    global _AUCTION_COMPARABLE_FIELDS
    if _AUCTION_COMPARABLE_FIELDS is None:
        _AUCTION_COMPARABLE_FIELDS=set(AuctionComparable.__dataclass_fields__.keys())
    clean={k:v for k,v in (d or {}).items() if k in _AUCTION_COMPARABLE_FIELDS}
    return AuctionComparable(**clean)

def _require_auction_v3():
    if not AUCTION_INTELLIGENCE_V3_AVAILABLE:
        return jsonify({"error":"Auction Intelligence 3.0 modules are not available on this server."}),200
    return None

# Short-TTL cache for the (relatively) expensive /api/auction/valuation call,
# mirroring the existing _SEARCH_CACHE pattern for /api/coin-search. The
# result is a pure function of its input (identity + comparables + dealer
# sample + target currency) except for FX lookups, which are themselves
# already cached inside auction_fx.py — this cache mainly protects against
# accidental duplicate submissions (e.g. a double click) within a short
# window, not against genuinely stale data.
_AUCTION_VALUATION_CACHE={}
_AUCTION_VALUATION_CACHE_LOCK=threading.Lock()
_AUCTION_VALUATION_CACHE_TTL=60

def _auction_valuation_cache_key(identity,identity_quality,raw_comparables,dealer_sample_prices,
                                  dealer_lowest,dealer_second_lowest,target_currency):
    payload={"identity":identity,"identity_quality":round(float(identity_quality or 0),3),
             "comparables":raw_comparables,"dealer_sample_prices":sorted(dealer_sample_prices),
             "dealer_lowest":dealer_lowest,"dealer_second_lowest":dealer_second_lowest,
             "target_currency":target_currency}
    try:
        return json.dumps(payload,sort_keys=True,default=str)
    except Exception:
        return str(payload)

@app.post("/api/auction/comparables/manual")
def auction_comparables_manual():
    """Parses free-text realized comparables (legacy one-price-per-line AND
    the new 'Date | Hammer | Currency | Grade | Auction House | URL' format,
    mixed freely) into structured AuctionComparable records. Does NOT
    classify them against a target identity yet — that happens in
    /api/auction/valuation, once the caller has a resolved identity."""
    guard=_require_auction_v3()
    if guard:return guard
    payload=request.get_json(silent=True) or {}
    text=payload.get("text") or ""
    default_currency=(payload.get("default_currency") or "EUR").upper()
    try:
        comps=ManualComparableAdapter().parse(text,default_currency=default_currency)
        return jsonify({"comparables":[c.to_dict() for c in comps],"count":len(comps)})
    except Exception as e:
        print(f"[auction-v3] manual parse EXCEPTION: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.post("/api/auction/comparables/csv")
def auction_comparables_csv():
    """Parses a CSV/XLSX-exported-as-CSV import of realized comparables.
    Required column: hammer. Optional: date, currency, grade, auction_house,
    auction_name, lot_number, url, grading_company, cert_number."""
    guard=_require_auction_v3()
    if guard:return guard
    payload=request.get_json(silent=True) or {}
    csv_text=payload.get("csv_text") or ""
    default_currency=(payload.get("default_currency") or "EUR").upper()
    if not csv_text.strip():
        return jsonify({"error":"Missing csv_text"}),400
    try:
        comps=CSVComparableAdapter().parse(csv_text,default_currency=default_currency)
        return jsonify({"comparables":[c.to_dict() for c in comps],"count":len(comps)})
    except ValueError as e:
        return jsonify({"error":str(e)}),400
    except Exception as e:
        print(f"[auction-v3] csv parse EXCEPTION: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.post("/api/auction/valuation")
def auction_valuation_endpoint():
    """Classifies each supplied comparable against `identity`, then builds a
    full Valuation Snapshot (realized market, dealer market, disagreement,
    fusion, Fair Value range, Confidence 2.0, Demand, Trend). This is the
    (relatively) expensive call — the frontend caches the returned snapshot
    and reuses it for cheap bid/sell recomputation via the endpoints below,
    rather than calling this again on every keystroke (spec §32)."""
    guard=_require_auction_v3()
    if guard:return guard
    payload=request.get_json(silent=True) or {}
    identity=payload.get("identity") or {}
    identity_quality=float(payload.get("identity_quality") or 0.5)
    raw_comparables=payload.get("comparables") or []
    dealer_sample_prices=[p for p in (payload.get("dealer_sample_prices") or []) if isinstance(p,(int,float))]
    dealer_lowest=payload.get("dealer_lowest")
    dealer_second_lowest=payload.get("dealer_second_lowest")
    # If any comparable's currency differs from target_currency, convert it
    # using real ECB reference rates (Frankfurter API) before it enters the
    # statistics — a mixed-currency comparable list must never be averaged
    # as if every price were already in the same unit. Best-effort: a failed
    # FX lookup for one comparable never fails the whole valuation.
    target_currency=(payload.get("target_currency") or payload.get("currency") or "EUR").upper()

    cache_key=_auction_valuation_cache_key(identity,identity_quality,raw_comparables,
                                            dealer_sample_prices,dealer_lowest,dealer_second_lowest,target_currency)
    now=time.time()
    with _AUCTION_VALUATION_CACHE_LOCK:
        hit=_AUCTION_VALUATION_CACHE.get(cache_key)
        if hit and now-hit["at"]<_AUCTION_VALUATION_CACHE_TTL:
            resp=dict(hit["data"]);resp["cache"]="hit"
            return jsonify(resp)

    try:
        comps=[_comparable_from_dict(d) for d in raw_comparables]
        for c in comps:
            classify_comparable(identity,c)
        snapshot=auction_val.compute_valuation_snapshot(
            identity=identity,identity_quality=identity_quality,comparables=comps,
            dealer_sample_prices=dealer_sample_prices,
            dealer_lowest=dealer_lowest,dealer_second_lowest=dealer_second_lowest,
            target_currency=target_currency)
        snapshot["comparables_evidence"]=[c.to_dict() for c in comps]
        with _AUCTION_VALUATION_CACHE_LOCK:
            _AUCTION_VALUATION_CACHE[cache_key]={"at":now,"data":snapshot}
            if len(_AUCTION_VALUATION_CACHE)>500:
                stale=[k for k,v in _AUCTION_VALUATION_CACHE.items() if now-v["at"]>_AUCTION_VALUATION_CACHE_TTL]
                for k in stale:_AUCTION_VALUATION_CACHE.pop(k,None)
        return jsonify(snapshot)
    except Exception as e:
        print(f"[auction-v3] valuation EXCEPTION: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.post("/api/auction/bid-advice")
def auction_bid_advice_endpoint():
    """Cheap, local recompute from an already-built snapshot — safe to call
    on every keystroke in the 'Current bid' field (spec §32/§62). Buy mode
    requires a real, valid positive current_hammer; an empty/invalid/
    negative/zero bid returns recommendation:null rather than guessing."""
    guard=_require_auction_v3()
    if guard:return guard
    payload=request.get_json(silent=True) or {}
    snapshot=payload.get("snapshot") or {}
    try:
        advice=auction_bid.bid_advice(
            snapshot,
            current_hammer_raw=str(payload.get("current_hammer") if payload.get("current_hammer") is not None else ""),
            buyer_premium_pct=float(payload.get("buyer_premium_pct") or 0),
            shipping=float(payload.get("shipping") or 0),
            fixed_fees=float(payload.get("fixed_fees") or 0),
            taxes=float(payload.get("taxes") or 0),
        )
        return jsonify(advice)
    except Exception as e:
        print(f"[auction-v3] bid-advice EXCEPTION: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

@app.post("/api/auction/sell-advice")
def auction_sell_advice_endpoint():
    """Sell-side guidance (consignment estimate, reserve, starting bid, net
    proceeds after commission/fees) from the same Valuation Snapshot used on
    the buy side — always internally consistent with the buy guidance."""
    guard=_require_auction_v3()
    if guard:return guard
    payload=request.get_json(silent=True) or {}
    snapshot=payload.get("snapshot") or {}
    try:
        advice=auction_sell.sell_advice(
            snapshot,
            seller_commission_pct=float(payload.get("seller_commission_pct") or 0),
            insurance_pct=float(payload.get("insurance_pct") or 0),
            photography_fee=float(payload.get("photography_fee") or 0),
            listing_fee=float(payload.get("listing_fee") or 0),
            other_fixed_fees=float(payload.get("other_fixed_fees") or 0),
        )
        return jsonify(advice)
    except Exception as e:
        print(f"[auction-v3] sell-advice EXCEPTION: {type(e).__name__}: {e}")
        return jsonify({"error":str(e)}),200

# =============================================================================
# NEW RELEASES — RSS aggregation from independent numismatic news sources.
# ("Which mints have new releases coming" — most major mints today only
# offer email newsletters, not RSS, per real research; the genuinely
# practical answer was a curated set of numismatic news sites that already
# track releases across many mints and DO publish real RSS feeds. Every URL
# below was verified against a live fetch of its actual XML before being
# added here — none of these are guessed.)
#
# Coverage is honestly still English-language and US/UK-leaning — that's the
# real state of what's available as free, public RSS in this space, not a
# gap in the search. Two sources with genuine international/European
# coverage were added on top of the original US-heavy set: Numismatic News
# (has an active "World Coins" section — Europe, South Africa, Canada, etc.)
# and Change Checker (UK-focused).
#
# Investigated and deliberately NOT added:
# - Bank of Greece (bankofgreece.gr) DOES publish real RSS feeds
#   (Announcements/Publications/Press Releases), confirmed via a live fetch
#   of https://www.bankofgreece.gr/xristika/rss — but requesting the actual
#   feed URL returns a ROBOTS_DISALLOWED response (its robots.txt blocks the
#   /_layouts/ path the feed is served from). Respecting that, the same way
#   any other automated fetch should, rather than quietly working around it.
# - Coin & Mint News (coinmintnews.com) — genuinely global trade-press
#   coverage, but appears to be a paid subscription publication with no
#   confirmed free public RSS feed.
# - Numista API — investigated specifically for a "recently added" / "new
#   releases" endpoint or sort parameter; none exists in the documented API
#   (it's an identification catalog, not a news/announcement service).
#
# CoinsWeekly (new.coinsweekly.com/feed/) — genuinely ~170-country European/
# international numismatic coverage (Swissmint, Spain, Germany, Austria and
# more). Its RSS feed's XML could not be fetched directly from this
# sandboxed environment (a general tooling limitation here: RSS/XML endpoints
# generally aren't indexed by web search, so this sandbox's web-fetch
# permission model — which only allows fetching URLs already surfaced by a
# search — can't reach them even when they're real and working). The user
# confirmed by checking the URL directly in their own browser that it
# returns real RSS XML, and that confirmation is what's included below.
# =============================================================================
NEW_RELEASES_FEEDS=[
    {"name":"CoinWeek","url":"https://coinweek.com/feed/"},
    {"name":"CoinNews.net","url":"https://feeds.feedburner.com/CoinNewsnet"},
    {"name":"World Coin News","url":"https://feeds.feedburner.com/WorldCoinNews"},
    {"name":"ANA Coin Press","url":"https://blog.money.org/coin-collecting/rss.xml"},
    {"name":"United States Mint","url":"https://www.usmint.gov/feed/usmint"},
    {"name":"Numismatic News","url":"https://www.numismaticnews.net/.rss/full"},
    {"name":"Change Checker (UK)","url":"https://blog.changechecker.org/feed"},
    {"name":"CoinsWeekly","url":"https://new.coinsweekly.com/feed/"},
]
_NEW_RELEASES_CACHE={"at":0,"data":None}
_NEW_RELEASES_CACHE_LOCK=threading.Lock()
_NEW_RELEASES_CACHE_TTL_SECONDS=30*60  # 30 minutes — news doesn't need to be fetched more often than that, and this keeps the endpoint fast and polite to the source sites.
_NEW_RELEASES_MAX_ITEMS=40

def _strip_html_to_text(s):
    if not s:return ""
    s=re.sub(r'<[^>]+>',' ',s)
    s=ihtml.unescape(s)
    s=re.sub(r'\s+',' ',s).strip()
    # Strip the common WordPress syndication trailer ("The post X appeared
    # first on Y.") — noise, not useful content for a reader here.
    s=re.sub(r'\s*The post .+ appeared first on .+\.\s*$','',s)
    if len(s)>220:
        s=s[:217].rsplit(' ',1)[0]+'...'
    return s

def _parse_feed_date(raw):
    if not raw:return None
    try:
        dt=email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    # Atom feeds commonly use ISO 8601 in <updated>/<published> instead of
    # RSS's RFC 822 pubDate — handle that format too.
    try:
        s=raw.strip().replace("Z","+00:00")
        from datetime import datetime as _dt
        dt=_dt.fromisoformat(s)
        if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _parse_one_feed(xml_text,source_name):
    """Parses RSS 2.0 (<rss><channel><item>) feeds, which is what every
    currently-curated source above actually uses (verified via live fetch).
    Includes best-effort Atom (<feed><entry>) support for robustness if a
    future source uses that format instead — that branch has NOT been tested
    against a real Atom feed, only written to the documented Atom schema."""
    items=[]
    try:
        root=ET.fromstring(xml_text)
    except ET.ParseError as e:
        return [],f"XML parse error: {e}"
    rss_items=root.findall('.//item')
    if rss_items:
        for it in rss_items:
            title=(it.findtext('title') or '').strip()
            link=(it.findtext('link') or '').strip()
            if not title or not link:continue
            pub_dt=_parse_feed_date(it.findtext('pubDate') or it.findtext('{http://purl.org/dc/elements/1.1/}date'))
            summary=_strip_html_to_text(it.findtext('description') or '')
            items.append({"title":title,"link":link,"source":source_name,
                          "published":pub_dt.isoformat() if pub_dt else None,
                          "published_display":pub_dt.strftime('%d %b %Y') if pub_dt else '',
                          "summary":summary})
        return items,None
    # Atom fallback (untested against a real feed — see docstring above).
    ns={"a":"http://www.w3.org/2005/Atom"}
    entries=root.findall('.//a:entry',ns)
    for en in entries:
        title=(en.findtext('a:title',default='',namespaces=ns) or '').strip()
        link_el=en.find('a:link',ns)
        link=link_el.get('href','') if link_el is not None else ''
        if not title or not link:continue
        pub_dt=_parse_feed_date(en.findtext('a:updated',default='',namespaces=ns) or en.findtext('a:published',default='',namespaces=ns))
        summary=_strip_html_to_text(en.findtext('a:summary',default='',namespaces=ns) or '')
        items.append({"title":title,"link":link,"source":source_name,
                      "published":pub_dt.isoformat() if pub_dt else None,
                      "published_display":pub_dt.strftime('%d %b %Y') if pub_dt else '',
                      "summary":summary})
    return items,None

def _fetch_new_releases():
    all_items=[];errors=[]
    for feed in NEW_RELEASES_FEEDS:
        try:
            r=requests.get(feed["url"],timeout=10,headers={"User-Agent":"CoinBidsNewReleasesBot/1.0 (+https://coinbids.eu)"})
            if r.status_code!=200:
                errors.append({"source":feed["name"],"error":f"HTTP {r.status_code}"});continue
            items,err=_parse_one_feed(r.text,feed["name"])
            if err:
                errors.append({"source":feed["name"],"error":err});continue
            all_items.extend(items)
        except Exception as e:
            errors.append({"source":feed["name"],"error":str(e)})
    # Newest first; items without a parseable date sort last rather than
    # being dropped (still shown, just not date-ordered against the rest).
    all_items.sort(key=lambda x:x["published"] or "",reverse=True)
    return {"items":all_items[:_NEW_RELEASES_MAX_ITEMS],"sources":[f["name"] for f in NEW_RELEASES_FEEDS],"errors":errors,"fetched_at":time.time()}

@app.get("/api/new-releases")
def new_releases():
    with _NEW_RELEASES_CACHE_LOCK:
        cached=_NEW_RELEASES_CACHE["data"]
        if cached and time.time()-_NEW_RELEASES_CACHE["at"]<_NEW_RELEASES_CACHE_TTL_SECONDS:
            resp=dict(cached);resp["cache"]="hit"
            return jsonify(resp)
    try:
        data=_fetch_new_releases()
        with _NEW_RELEASES_CACHE_LOCK:
            _NEW_RELEASES_CACHE["data"]=data;_NEW_RELEASES_CACHE["at"]=time.time()
        return jsonify(data)
    except Exception as e:
        print(f"[new-releases] EXCEPTION: {type(e).__name__}: {e}")
        return jsonify({"items":[],"sources":[],"errors":[{"source":"all","error":str(e)}],"fetched_at":time.time()}),200

@app.get("/api/metal-spot")
def metal_spot():
    """Return live XAU/XAG USD/oz plus USD->EUR using server-side requests.
    Browser-side public CORS proxies are intentionally avoided.
    """
    try:
        r=SESSION.get("https://data-asg.goldprice.org/dbXRates/USD",timeout=12)
        r.raise_for_status()
        data=r.json()
        item=(data.get("items") or [None])[0]
        if not item or item.get("xauPrice") is None or item.get("xagPrice") is None:
            raise ValueError("unexpected goldprice response")
        rates=fx_rates()
        eur=rates.get("EUR")
        if not eur:
            raise ValueError("EUR exchange rate unavailable")
        return jsonify({
            "gold_usd_oz":float(item["xauPrice"]),
            "silver_usd_oz":float(item["xagPrice"]),
            "usd_to_eur":float(eur),
            "source":"goldprice.org + CoinBids FX"
        })
    except Exception as e:
        print(f"[metal-spot] {type(e).__name__}: {e}",flush=True)
        return jsonify({"error":"live metal price unavailable"}),503

@app.get("/api/system-diagnostics")
def system_diagnostics():
    shipping_exists=os.path.isfile(MA_SHOPS_SHIPPING_CSV)
    return jsonify({
        "shipping_db":{"path":MA_SHOPS_SHIPPING_CSV,"exists":shipping_exists,
                       "rules":len(load_mashops_shipping_rows()) if shipping_exists else 0},
        "postgres":{"configured":bool(DATABASE_URL),
                    "disabled_reason":globals().get("_PG_DISABLED_REASON")},
        "numista":{"configured":bool(NUMISTA_API_KEY)},
    })

@app.get("/health")
def health():
    pg_status="not_configured"
    if DATABASE_URL:
        try:
            conn=_get_pg_connection()
            if conn is None:
                pg_status="connection_failed"
            else:
                try:
                    with conn.cursor() as cur:
                        cur.execute("select count(*) from coin_specs")
                        row_count=cur.fetchone()[0]
                    pg_status=f"connected ({row_count} coin_specs rows)"
                finally:
                    _release_pg_connection(conn)
        except Exception as e:
            pg_status=f"error: {type(e).__name__}: {e}"
    return jsonify({"ok":True,"service":"CoinBids backend (MA-Shops + Numista)","numista_configured":bool(NUMISTA_API_KEY),"resolver_available":RESOLVER_AVAILABLE,"auction_intelligence_v3_available":AUCTION_INTELLIGENCE_V3_AVAILABLE,"corrections_available":CORRECTIONS_AVAILABLE,"corrections_write_enabled":bool(CORRECTIONS_SECRET),"coin_specs_postgres":pg_status})

_SEARCH_CACHE={}
_SEARCH_CACHE_LOCK=threading.Lock()
_SEARCH_CACHE_TTL=900  # 15 minutes — MA-Shops listings/prices don't meaningfully
                        # change minute-to-minute; this avoids re-scraping the
                        # exact same normalized query repeatedly under load.

def _search_cache_key(payload):
    coin=payload.get("coin") or {}
    # Include the raw free-text query / canonical resolved identity, not only
    # the structured coin fields — otherwise two semantically different raw
    # queries with an empty or identical structured "coin" object (e.g. both
    # relying entirely on server-side resolver inference) would collide on
    # the same cache key and one would silently serve the other's cached
    # results for up to _SEARCH_CACHE_TTL seconds.
    raw_query=str(payload.get("raw_query") or coin.get("raw") or "").strip().lower()
    parts=[raw_query]
    parts+=[str(coin.get(k) or "").strip().lower() for k in
           ("country","denom","denomination","year","variant","grade")]
    parts+=[str(payload.get("include_shipping")),str(payload.get("currency") or "EUR").upper(),
            str(payload.get("ship_to") or ""),
            str(payload.get("scan_limit") or ""),str(payload.get("sample_limit") or "")]
    return "|".join(parts)

@app.post("/api/coin-search")
def coin_search():
    payload=request.get_json(silent=True) or {}
    # Diagnostics counters: these enrichment paths are currently disabled in
    # the resource-safe/local-shipping flow, but the response schema still
    # exposes the counters. Initialize them so diagnostics can never crash the
    # entire Price Research request.
    direct_checked=0
    geo_checked=0
    cache_key=_search_cache_key(payload)
    now=time.time()
    with _SEARCH_CACHE_LOCK:
        hit=_SEARCH_CACHE.get(cache_key)
        if hit and now-hit["at"]<_SEARCH_CACHE_TTL:
            resp=dict(hit["data"]);resp["cache"]="hit"
            return jsonify(resp)
    # Coin Intelligence Core: resolve a single target identity once, used to
    # attach an explainable 0-100 identity_match_score to each listing in the
    # evidence panel. This is purely additive metadata — it never changes
    # which listings pass passes_hard_filter or how they're ranked/selected.
    target_identity=None
    if RESOLVER_AVAILABLE:
        _coin_for_target=payload.get("coin") or {}
        _raw_for_target=(payload.get("raw_query") or _coin_for_target.get("raw") or "").strip()
        if not _raw_for_target:
            _raw_for_target=" ".join(str(x or "") for x in [
                _coin_for_target.get("country"),
                _coin_for_target.get("denom") or _coin_for_target.get("denomination"),
                _coin_for_target.get("year")]).strip()
        if _raw_for_target:
            try:
                target_identity=resolve_coin_identity(_raw_for_target).get("best")
            except Exception as e:
                target_identity=None
                print(f"[resolver] coin-search target identity failed: {type(e).__name__}: {e}")

    queries=make_queries(payload);all_offers=[];errors=[];used=[]
    # Bounded concurrency (max 3 at once) instead of one-at-a-time: this
    # previously ran fully sequentially — up to 8 queries x 2 URLs each (see
    # fetch_search) meant up to 16 real, sequential HTTP round-trips with a
    # 20s timeout apiece, which could genuinely take a minute or more
    # end-to-end and feel like the app had hung. Capped at 3 concurrent
    # requests (not unbounded) to stay polite to MA-Shops and avoid
    # tripping its anti-bot/CAPTCHA protection, which a large burst of
    # simultaneous requests risks doing far more than a moderate, bounded
    # level of concurrency does.
    MAX_CONCURRENT_MA_SHOPS_REQUESTS=3
    executor=ThreadPoolExecutor(max_workers=MAX_CONCURRENT_MA_SHOPS_REQUESTS)
    future_to_query={executor.submit(fetch_search,q,payload):q for q in queries}
    try:
        for future in as_completed(future_to_query):
            q=future_to_query[future]
            try:
                ma_offers,ma_url,ma_err=future.result()
            except Exception as e:
                ma_offers,ma_url,ma_err=[],None,str(e)
            if ma_url:used.append({"query":q,"source":"MA-Shops","url":ma_url})
            if ma_err:errors.append({"query":q,"source":"MA-Shops","error":ma_err})
            all_offers.extend(ma_offers)
            if len(all_offers)>=80:
                break
    finally:
        # Any query still queued (not yet started, since only 3 run at once)
        # is cancelled once we have enough offers — already-in-progress
        # requests are simply left to finish in the background rather than
        # forcibly killed mid-request.
        executor.shutdown(wait=False,cancel_futures=True)
    raw_count=len(all_offers)
    searched_cheapest_first=any("sortby=preis_eur" in str(u.get("url","")) for u in used)
    # Deduplicate by canonical URL.
    by={}
    for o in all_offers:
        key=o.get("url") or (o.get("title"),o.get("price"))
        if key not in by or o.get("_score",0)>by[key].get("_score",0):by[key]=o
    offers=list(by.values())
    unique_count=len(offers)
    rejected={"asset":0,"scope":0,"identity":0}
    valid=[]
    for o in offers:
        # MA-Shops splits identity fields across columns. Validate against the
        # complete row when available; other sources fall back to title.
        match_text=o.get("_match_text") or o.get("title","")
        asset,conf=classify_asset(match_text);o["asset_type"]=asset;o["asset_confidence"]=conf
        if asset=="BANKNOTE":rejected["asset"]+=1;continue
        if product_scope(match_text)!="SINGLE_COIN":rejected["scope"]+=1;continue
        if not passes_hard_filter(match_text,payload):rejected["identity"]+=1;continue
        if target_identity:
            try:
                ms=get_resolver().listing_match_score(target_identity,match_text)
                o["identity_match_score"]=ms.get("score")
                o["identity_match_reasons"]=ms.get("reasons")
            except Exception:
                pass
        valid.append(o)
    print(f"[coin-search] candidates raw={raw_count} unique={unique_count} valid={len(valid)} rejected={rejected}", flush=True)
    valid_count=len(valid)

    # ------------------------------------------------------------------
    # RESOURCE-SAFE PRICE / SHIPPING ENRICHMENT
    # ------------------------------------------------------------------
    # The old implementation could open 30-40 MA-Shops item pages
    # sequentially before ranking. On a small Render instance this can kill
    # the worker (SIGKILL / HTTP 500). For top-2 all-in ranking we do not need
    # to inspect every validated listing.
    #
    # Shipping is never negative:
    #   total = item price + shipping >= item price
    #
    # Therefore item price is a hard lower bound. Walk candidates from
    # cheapest item price upward, and stop as soon as the next item's price is
    # already above the current second-best confirmed total.
    ship_to_country=(payload.get("ship_to") or "").strip()
    include_shipping=bool(payload.get("include_shipping"))
    if include_shipping and not ship_to_country:
        return jsonify({"error":"shipping destination is required","offers":[]}),400
    by_price=sorted(
        valid,
        key=lambda o:(
            o.get("price") is None,
            o.get("price") if o.get("price") is not None else float("inf"),
            -o.get("_score",0)
        )
    )

    # Shipping now comes from the local MA-Shops shipping database.
    # No ScrapingBee and no per-offer shipping-page fetches are required.
    item_weight_g=(
        payload.get("weight_g") or payload.get("coin_weight_g") or
        payload.get("physical_weight_g")
    )
    shipping_weight_source="request" if item_weight_g else None

    # Automatic MA-Shops/coin-spec weight bridge:
    # shipping tiers often depend on grams. If the frontend did not send a
    # weight, reuse a cached validated MA-Shops physical spec; if none exists,
    # resolve the exact coin specs once and feed that verified weight directly
    # into the local shipping database. No shipping-page request is made.
    if include_shipping and not item_weight_g:
        _coin_for_specs=payload.get("coin") or {}
        _cached_spec=cached_mashops_spec(_coin_for_specs)
        if _cached_spec and _cached_spec.get("weight_g"):
            item_weight_g=_cached_spec.get("weight_g")
            shipping_weight_source="ma_shops_cached_spec"
        else:
            try:
                _spec=mashops_spec_fallback(_coin_for_specs,(payload.get("raw_query") or "").strip())
                if _spec and _spec.get("weight_g"):
                    item_weight_g=_spec.get("weight_g")
                    shipping_weight_source="ma_shops_validated_spec"
            except Exception as _e:
                print(f"[shipping-db] automatic weight resolution failed: {type(_e).__name__}: {_e}",flush=True)

    if item_weight_g is not None:
        try:item_weight_g=float(item_weight_g)
        except Exception:item_weight_g=None

    def finalize(o):
        target_currency=(payload.get("currency") or "EUR").upper()
        if o.get("shipping_status")=="unverified":
            o["shipping"]=None
        item_eur=to_eur(o.get("price"),o.get("currency"))
        ship_eur=to_eur(o.get("shipping"),o.get("currency")) if o.get("shipping") is not None else None
        if target_currency=="EUR":
            o["price"]=round(item_eur,2) if item_eur is not None else o.get("price")
            if ship_eur is not None:o["shipping"]=round(ship_eur,2)
            if o.get("original_price") is not None:
                _old_eur=to_eur(o.get("original_price"),o.get("discount_currency") or o.get("currency"))
                if _old_eur is not None:o["original_price"]=round(_old_eur,2)
            o["currency"]="EUR"
        elif target_currency in ("USD","GBP","CHF") and o.get("currency")!=target_currency:
            rates=fx_rates();rate=rates.get(target_currency)
            if item_eur is not None and rate:
                o["price"]=round(item_eur*rate,2)
                if ship_eur is not None:o["shipping"]=round(ship_eur*rate,2)
                o["currency"]=target_currency
        if include_shipping:
            o["total"]=round(float(o["price"])+float(o["shipping"]),2) if o.get("shipping") is not None else None
        else:
            o["total"]=round(float(o["price"]),2)
        if not o.get("dealer"):o["dealer"]="MA-Shops"


    # Normalize item prices, then resolve destination shipping locally.
    for o in valid:
        finalize(o)
        if include_shipping:
            if lookup_mashops_shipping(o,ship_to_country,item_weight_g):
                o["shipping_weight_g"]=item_weight_g
                o["shipping_weight_source"]=shipping_weight_source
                finalize(o)
            else:
                # Search-result shipping is not destination-safe. If the DB
                # cannot select a tier (often because weight is unknown), do
                # not guess a total.
                o["shipping"]=None
                o["shipping_status"]="unknown_db_no_match"
                o["total"]=None

    valid.sort(
        key=lambda o:(
            o.get("total") is None,
            o.get("total") if o.get("total") is not None else float("inf"),
            o.get("price") if o.get("price") is not None else float("inf"),
            -o.get("_score",0)
        )
    )

    top=valid[:max(1,min(int(payload.get("limit") or 2),2))]
    db_matched=sum(1 for o in valid if o.get("shipping_status")=="known_target_db")
    print(
        f"[coin-search] shipping-db matched={db_matched}/{len(valid)} "
        f"ship_to={ship_to_country} item_weight_g={item_weight_g} weight_source={shipping_weight_source}",
        flush=True
    )



    # Dealer market sample = strongest identity matches (relevance-ranked),
    # NOT simply the cheapest ones. This is used for market-value anchoring
    # in Auction Intelligence; it deliberately avoids valuing a coin from
    # only the two lowest asks, which structurally biases Fair Value downward
    # (two cheap "buy opportunities" are not a representative market sample).
    sample_limit=max(5,min(int(payload.get("sample_limit") or 10),15))
    market_sample=[]
    seen_sample=set()
    for o in sorted(valid,key=lambda x:-x.get("_score",0)):
        k=o.get("url") or (o.get("title"),o.get("price"))
        if k in seen_sample:continue
        seen_sample.add(k)
        if o.get("total") is not None:
            market_sample.append(o)
        if len(market_sample)>=sample_limit:break

    def public_offer(o):
        d=dict(o)
        d.pop("_score",None)
        d.pop("dealer_source",None)
        d.pop("_match_text",None)
        return d
    top_public=[public_offer(o) for o in top]
    sample_public=[public_offer(o) for o in market_sample]

    diagnostics={
        "raw_candidates_found":raw_count,
        "unique_candidates":unique_count,
        "validated_matching_coins":valid_count,
        "detail_pages_checked":sum(1 for o in valid if o.get("detail_page_checked")),
        "direct_detail_pages_checked":direct_checked,
        "geo_detail_pages_checked":geo_checked,
        "known_comparable_totals":sum(1 for o in valid if o.get("total") is not None),
        "discounted_valid_offers":sum(1 for o in valid if o.get("is_discounted")),
        "displayed_candidates":len(top_public),
        "valuation_sample_size":len(sample_public),
        "searched_cheapest_first_pages":searched_cheapest_first,
    }

    result={
        "source":"MA-Shops","queries":queries,"used_search_pages":used,"offers":top_public,
        "market_sample":sample_public,
        "best_offer":top_public[0] if top_public else None,"count":len(top_public),"raw_count":raw_count,
        "unique_count":unique_count,"valid_count":valid_count,"diagnostics":diagnostics,"rejected":rejected,
        "sources_ok":["MA-Shops"] if used else [],
        "sources_failed":["MA-Shops"] if errors and not used else [],"errors":errors[-6:],
        "note":"The two cheapest validated matching COIN listings found after scanning both normal and cheapest-first MA-Shops search results are shown as purchase anchors. Dealer market value (Auction Intelligence) uses a separate, broader relevance-ranked sample — not only those two lowest asks. Unknown shipping is never treated as free.",
        "shipping_note":f"shipping=null means unknown. shipping_status distinguishes confidence: known_target (confirmed for your chosen destination, {ship_to_country}), known_other_destination (a specific other destination was found — see shipping_destination), known_unconfirmed_destination (a flat rate was found with no destination stated), free (confirmed free), unknown (nothing reliable found).",
        "ship_to_country":ship_to_country,
        "cache":"miss"
    }
    # Only cache genuinely successful lookups — never cache a transient failure
    # so a temporary MA-Shops block doesn't get "frozen" as the answer for 15 minutes.
    if top_public:
        with _SEARCH_CACHE_LOCK:
            _SEARCH_CACHE[cache_key]={"at":time.time(),"data":result}
    return jsonify(result)

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
    print("CoinBids backend")
    print(f"  On this PC:        http://127.0.0.1:{port}")
    if lan_ip and port == 8765:
        print(f"  From phone/tablet:  http://{lan_ip}:{port}   (same WiFi required)")
        print(f"  -> In the app's Research Settings, set the endpoint to:")
        print(f"     http://{lan_ip}:{port}/api/coin-search")
    print(f"  Health check: http://127.0.0.1:{port}/health")
    print("  If Windows Firewall prompts you, click 'Allow access'.")
    app.run(host="0.0.0.0",port=port,debug=False)
