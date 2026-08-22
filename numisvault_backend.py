from flask import Flask, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
import requests, re, html as ihtml, urllib.parse, time, os, math, threading, json
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import xml.etree.ElementTree as ET
import email.utils
from datetime import timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import secrets
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
MA_SHOPS_SHIPPING_CSV = os.environ.get(
    "MA_SHOPS_SHIPPING_CSV",
    os.path.join(APP_DIR, "ma_shops_shipping.csv")
)
_MA_SHOPS_SHIPPING_ROWS = None
_MA_SHOPS_SHIPPING_MTIME = None

def _fnum(v):
    try:
        if v is None or str(v).strip()=="" or str(v).lower()=="nan":
            return None
        return float(v)
    except Exception:
        return None

def load_mashops_shipping_rows():
    global _MA_SHOPS_SHIPPING_ROWS, _MA_SHOPS_SHIPPING_MTIME
    try:
        mtime=os.path.getmtime(MA_SHOPS_SHIPPING_CSV)
    except OSError:
        print(f"[shipping-db] missing file: {MA_SHOPS_SHIPPING_CSV}", flush=True)
        return []
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
BANKNOTE_TERMS=(
    "banknote","bank note","paper money","billet","banknoten","pick #","pick p",
    "watermark","serial number","silver certificate","gold certificate",
    "federal reserve note","legal tender note","treasury note","national currency",
    "fractional currency","large size note","small size note","banconota","billete",
    "papiergeld","notgeld","notgeldschein","notgeldscheine","emergency money",
    "geldschein","geldscheine","currency note","currency notes"
)
BANKNOTE_STRONG_TERMS=(
    "banknote","bank note","paper money","silver certificate","gold certificate",
    "federal reserve note","legal tender note","treasury note","national currency",
    "fractional currency","large size note","small size note","papiergeld",
    "banconota","billete","notgeld","notgeldschein","notgeldscheine",
    "emergency money","geldschein","geldscheine","currency note","currency notes"
)
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

_DENOM_NUMBER_WORDS={
    "one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7",
    "eight":"8","nine":"9","ten":"10","half":"0.5","quarter":"0.25"
}

def _canonicalize_denom_text(s):
    """Normalize number words and common fraction forms only for denomination parsing."""
    a=norm(s).replace(",",".")
    for word,numtxt in _DENOM_NUMBER_WORDS.items():
        a=re.sub(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])",numtxt,a)
    # Common numismatic fraction forms.
    a=re.sub(r"(?<!\d)1\s*/\s*2(?!\d)","0.5",a)
    a=re.sub(r"(?<!\d)1\s*/\s*4(?!\d)","0.25",a)
    a=re.sub(r"(?<!\d)3\s*/\s*4(?!\d)","0.75",a)
    return a

def parse_denomination(s):
    a=_canonicalize_denom_text(s)
    # Do not begin inside a fraction such as 1/2 Dollar.
    m=re.search(r"(?<![\d/])(\d+(?:\.\d+)?)\s*(" + DENOM_UNIT_ALT + r")(?![a-z])",a,re.I)
    if not m:return None
    val=float(m.group(1));unit=_normalize_denom_unit(m.group(2))
    return val,unit

def extract_denominations(s):
    """Return every explicit denomination in text in canonical numeric/unit form."""
    a=_canonicalize_denom_text(s)
    out=[]
    pat=r"(?<![\d/])(\d+(?:\.\d+)?)\s*(" + DENOM_UNIT_ALT + r")(?![a-z])"
    for v,u in re.findall(pat,a,re.I):
        unit=_normalize_denom_unit(u)
        try: val=float(v)
        except Exception: continue
        item=(val,unit)
        if item not in out:out.append(item)
    return out

def denomination_matches(target, title):
    td=parse_denomination(target)
    if not td:return True
    for d in extract_denominations(title):
        if abs(d[0]-td[0])<1e-9 and d[1]==td[1]:
            return True
    return False

def denomination_conflicts(target, title):
    """Reject an explicitly different denomination in the listing title.

    MA-Shops search rows can contain denomination metadata outside the clickable
    item title.  The full row remains useful when a title omits denomination,
    but it must never rescue a title that explicitly says a different value
    (e.g. target 1 Dollar, title Quarter Dollar / 0.25 Dollar).

    Conservative rule: no explicit denomination in the title => no conflict.
    If the title does state one or more denominations, at least one must exactly
    equal the requested value+unit.
    """
    td=parse_denomination(target)
    if not td:return False
    stated=extract_denominations(title)
    if not stated:return False
    return not any(abs(d[0]-td[0])<1e-9 and d[1]==td[1] for d in stated)

def _ma_shops_banknote_evidence(*parts):
    """Hard reject explicit paper-money evidence."""
    a=norm(" ".join(str(x or "") for x in parts))
    terms=(
        "banknotes","banknote","emergency money","notgeld","notgeldschein",
        "notgeldscheine","paper money","papiergeld","geldschein","geldscheine",
        "silver certificate","gold certificate","federal reserve note",
        "legal tender note","treasury note","national currency",
        "fractional currency","currency note","currency notes"
    )
    return any(term in a for term in terms)

def classify_asset(title):
    a=norm(title)
    if _ma_shops_banknote_evidence(a):
        return "BANKNOTE",.995

    # High-confidence paper-money phrases are decisive on their own.
    # This is important for listings such as "USA 1 Dollar 1923 Silver
    # Certificate": previously "silver certificate" was not recognized and
    # the listing could fall through as UNKNOWN/COIN.
    strong_hits=sum(1 for x in BANKNOTE_STRONG_TERMS if x in a)
    if strong_hits:
        return "BANKNOTE",min(.995,.90+.02*strong_hits)

    bank=sum(1 for x in BANKNOTE_TERMS if x in a)
    coin=sum(1 for x in COIN_TERMS if x in a)

    # Pick catalogue references are paper-money evidence.
    if re.search(r"\bp[- ]?\d{1,5}[a-z]?\b",a,re.I):
        bank+=3

    if bank>=2 and bank>coin:
        return "BANKNOTE",min(.99,.65+.08*bank)
    if coin>=1:
        return "COIN",min(.98,.70+.06*coin)
    return "UNKNOWN",.45

def product_scope(title):
    a=norm(title)

    # Explicit set/lot language. Boundaries matter: "set" inside another word
    # must not be enough on its own.
    set_patterns=(
        r"\bkms\b", r"\bkursm[üu]nzensatz\b", r"\bmin(?:t|ze)\s*set\b",
        r"\bcoin\s*set\b", r"\bcoins\s*set\b", r"\bannual\s*set\b",
        r"\byear\s*set\b", r"\bcomplete\s*set\b", r"\bproof\s*set\b",
        r"\bunc(?:irculated)?\s*set\b", r"\bcollector(?:s)?\s*set\b",
        r"\bset\s+of\s+\d+\b", r"\blot\s+of\b", r"\broll\b", r"\brouleau\b",
        r"\b\d+\s+(?:werte|coins|m[üu]nzen|pieces|pcs)\b",
        r"\bsatz\b"
    )
    if any(re.search(p,a,re.I) for p in set_patterns):
        return "SET"
    if any(x in a for x in SET_TERMS):
        return "SET"
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
    # Canonical denomination variant: "one dollar", "$1", "1 dollar"
    # all converge to the same structured denomination search.
    _pd=parse_denomination(denom or raw)
    canonical_denom=(f"{_pd[0]:g} {_pd[1]}" if _pd else denom)
    core = " ".join(x for x in [country, canonical_denom, year] if x)
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
               "_score":(score_title(match_text,payload)+_coin_type_rank_bonus(match_text,payload))}
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


def _mashops_spec_blocks(html_text):
    """Return compact visible MA-Shops blocks containing explicit spec labels."""
    if not html_text:
        return []
    soup=BeautifulSoup(html_text,"html.parser")
    for bad in soup(["script","style","noscript","template","svg"]):
        bad.decompose()

    labels=re.compile(
        r"\b(?:material|composition|metal|legierung|materiale|m[ée]tal|"
        r"weight|gewicht|poids|peso|fineness|feinheit|titre|ley|"
        r"diameter|durchmesser|diam[eè]tre|diametro)\b|[Øø]",re.I)
    stop=re.compile(
        r"(?:recently viewed|similar items|you may also like|recommend(?:ed|ations?)|"
        r"weitere artikel|ähnliche artikel|newsletter|customer support)",re.I)

    out=[];seen=set()
    for node in soup.find_all(["table","tr","td","div","section","p","li"]):
        txt=smart_join(node.stripped_strings)
        if not txt or len(txt)>650 or not labels.search(txt) or stop.search(txt):
            continue
        # Require at least two different physical-spec labels.
        flags=[
            bool(re.search(r"\b(?:material|composition|metal|legierung|materiale|m[ée]tal)\b",txt,re.I)),
            bool(re.search(r"\b(?:weight|gewicht|poids|peso)\b",txt,re.I)),
            bool(re.search(r"\b(?:fineness|feinheit|titre|ley)\b",txt,re.I)),
            bool(re.search(r"\b(?:diameter|durchmesser|diam[eè]tre|diametro)\b|[Øø]",txt,re.I)),
        ]
        if sum(flags)<2:
            continue
        if txt not in seen:
            seen.add(txt);out.append(txt)
    return out


def extract_mashops_explicit_specs(html_text, source_url=None):
    """Extract only label-bound physical facts from coherent MA-Shops blocks.

    Critical rule: a bare number such as 188 can NEVER become fineness.
    """
    metal_aliases={
        "silver":"Silver","silber":"Silver","argent":"Silver","argento":"Silver","plata":"Silver",
        "gold":"Gold","or":"Gold","oro":"Gold",
        "platinum":"Platinum","platin":"Platinum","platine":"Platinum","platino":"Platinum",
        "palladium":"Palladium",
        "copper":"Copper","kupfer":"Copper","cuivre":"Copper","cobre":"Copper",
        "bronze":"Bronze","brass":"Brass","messing":"Brass",
        "nickel":"Nickel","steel":"Steel","stahl":"Steel",
    }

    candidates=[]
    for evidence in _mashops_spec_blocks(html_text):
        rec={"source_url":source_url,"evidence":evidence}

        wm=re.search(
            r"(?:weight|gewicht|poids|peso)\s*[:\-]?\s*"
            r"([0-9]+(?:[.,][0-9]+)?)\s*(?:g|gr|gram|grams|gramm)\b",
            evidence,re.I)
        if wm:
            v=num(wm.group(1))
            if v is not None and 0.05<=v<=2000: rec["weight_g"]=float(v)

        dm=re.search(
            r"(?:diameter|durchmesser|diam[eè]tre|diametro)\s*[:\-]?\s*"
            r"([0-9]+(?:[.,][0-9]+)?)\s*mm\b",evidence,re.I)
        if not dm:
            dm=re.search(r"(?:Ø|ø)\s*([0-9]+(?:[.,][0-9]+)?)\s*mm\b",evidence)
        if dm:
            v=num(dm.group(1))
            if v is not None and 3<=v<=200: rec["diameter_mm"]=float(v)

        cm=re.search(
            r"(?:material|composition|metal|legierung|materiale|m[ée]tal)\s*[:\-]?\s*"
            r"(.{1,100}?)(?=\s+(?:weight|gewicht|poids|peso|fineness|feinheit|titre|ley|"
            r"diameter|durchmesser|diam[eè]tre|diametro|catalog|katalog|mint|mintage)\b|$)",
            evidence,re.I)
        comp=cm.group(1).strip(" .;,-") if cm else ""
        if comp:
            rec["composition_text"]=comp
            for raw,canon in metal_aliases.items():
                if re.search(rf"\b{re.escape(raw)}\b",comp,re.I):
                    rec["primary_metal"]=canon;break

        # LABEL-BOUND fineness only.
        fm=re.search(
            r"(?:fineness|feinheit|titre|ley)\s*[:\-]?\s*"
            r"(?:0[.,])?([0-9]{3})(?:\s*(?:‰|/ ?1000))?",
            evidence,re.I)
        if fm:
            v=int(fm.group(1))
            if 100<=v<=999: rec["fineness_per_mille"]=v
        elif comp:
            # Explicit ".900" inside the captured Material/Composition value is allowed.
            fm=re.search(r"(?:^|\s)(?:0[.,]|\.)([0-9]{3})(?:\b|$)",comp)
            if fm:
                v=int(fm.group(1))
                if 100<=v<=999: rec["fineness_per_mille"]=v

        if sum(k in rec for k in ("primary_metal","fineness_per_mille","weight_g","diameter_mm"))>=2:
            candidates.append(rec)
    return candidates


def extract_mashops_physical_specs_from_html(html_text, source_url=None):
    """Backward-compatible strict single-page extractor."""
    candidates=extract_mashops_explicit_specs(html_text,source_url)
    if not candidates:return None
    # Prefer the most complete coherent block.
    rec=max(candidates,key=lambda x:sum(k in x for k in ("primary_metal","fineness_per_mille","weight_g","diameter_mm")))
    metal=rec.get("primary_metal");fine=rec.get("fineness_per_mille");weight=rec.get("weight_g")
    if not (metal and fine and weight is not None):
        return None
    return {
        "composition":f"{metal} (.{fine:03d})","primary_metal":metal,
        "fineness_per_mille":fine,"weight_g":weight,
        "diameter_mm":rec.get("diameter_mm"),
        "fine_metal_g":weight*fine/1000.0,
        "spec_source":"MA-Shops exact labelled item specification",
        "data_provider":"MA-Shops","source_url":source_url,
        "source_evidence_text":rec.get("evidence"),
    }


_MA_SPEC_CACHE = {}

def _coin_identity_key(coin):
    return "|".join(str(coin.get(k) or "").strip().lower() for k in ("countryEN","country","denom","year","variant"))

def cache_mashops_spec(coin, spec):
    # Cache only multi-source consensus or a complete strict record.
    if spec and spec.get("weight_g") and spec.get("primary_metal") and spec.get("fineness_per_mille"):
        _MA_SPEC_CACHE[_coin_identity_key(coin)] = dict(spec)

def cached_mashops_spec(coin):
    return _MA_SPEC_CACHE.get(_coin_identity_key(coin))


def _independent_by_dealer(rows):
    out=[];seen=set()
    for r in rows:
        key=(r.get("dealer") or r.get("url") or "").lower()
        if not key or key in seen:continue
        seen.add(key);out.append(r)
    return out


def _exact_consensus(rows, field):
    rows=_independent_by_dealer([r for r in rows if r.get(field) not in (None,"")])
    groups={}
    for r in rows:
        val=r[field]
        key=str(val).strip().lower()
        groups.setdefault(key,[]).append(r)
    if not groups:return None,[]
    _,grp=max(groups.items(),key=lambda kv:len(kv[1]))
    return grp[0][field],grp


def _numeric_consensus(rows, field, abs_tol, rel_tol):
    rows=_independent_by_dealer([r for r in rows if r.get(field) not in (None,"")])
    best=[]
    for a in rows:
        av=float(a[field]);cluster=[]
        for r in rows:
            rv=float(r[field]);tol=max(abs_tol,abs(av)*rel_tol)
            if abs(rv-av)<=tol:cluster.append(r)
        if len(cluster)>len(best):best=cluster
    if not best:return None,[]
    vals=sorted(float(r[field]) for r in best)
    mid=vals[len(vals)//2] if len(vals)%2 else (vals[len(vals)//2-1]+vals[len(vals)//2])/2
    return mid,best


def mashops_spec_fallback(coin, raw_query=""):
    """Resolve physical specs from multiple identity-validated MA-Shops listings.

    Price ranking and spec discovery are separate. Up to 20 exact item pages are
    inspected; a field is published only when >=2 independent dealers agree.
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

    valid=[]
    for o in offers:
        mt=o.get("_match_text") or o.get("title","")
        if classify_asset(mt)[0]=="BANKNOTE":continue
        if product_scope(mt)!="SINGLE_COIN":continue
        if not passes_hard_filter(mt,payload):continue
        valid.append(o)
    valid.sort(key=lambda o:(o.get("price") is None,o.get("price") or float("inf")))

    evidence=[];checked=0
    print(f"[MA-Shops specs] candidates offers={len(offers)} valid={len(valid)} query={query!r}",flush=True)
    for o in valid[:20]:
        try:
            # Page-level asset/category validation first.
            asset=inspect_mashops_item_asset(o.get("url"),timeout=7)
            if asset.get("asset_type")=="BANKNOTE":
                continue

            r=SESSION.get(o.get("url"),timeout=8,allow_redirects=True)
            checked+=1
            if not r.ok:continue
            soup=BeautifulSoup(r.text,"html.parser")
            h1=soup.find("h1")
            page_title=h1.get_text(" ",strip=True) if h1 else (o.get("title") or "")
            identity_text=" ".join(x for x in [page_title,o.get("_match_text"),o.get("title")] if x)
            if not passes_hard_filter(identity_text,payload):continue
            if score_title(identity_text,payload)<0.72:continue

            dealer=_dealer_slug_from_offer({"url":r.url})
            for rec in extract_mashops_explicit_specs(r.text,r.url):
                rec.update({"dealer":dealer,"url":r.url,"title":page_title})
                evidence.append(rec)

            metal,mg=_exact_consensus(evidence,"primary_metal")
            fine,fg=_exact_consensus(evidence,"fineness_per_mille")
            weight,wg=_numeric_consensus(evidence,"weight_g",0.08,0.004)
            diam,dg=_numeric_consensus(evidence,"diameter_mm",0.18,0.003)
            if all(len(g)>=2 for g in (mg,fg,wg,dg)):
                break
        except Exception as e:
            print(f"[MA-Shops specs] item fetch failed: {type(e).__name__}: {e}",flush=True)

    metal,mg=_exact_consensus(evidence,"primary_metal")
    fine,fg=_exact_consensus(evidence,"fineness_per_mille")
    weight,wg=_numeric_consensus(evidence,"weight_g",0.08,0.004)
    diam,dg=_numeric_consensus(evidence,"diameter_mm",0.18,0.003)

    accepted={}
    groups={"primary_metal":mg,"fineness_per_mille":fg,"weight_g":wg,"diameter_mm":dg}
    if metal is not None and len(mg)>=2:accepted["primary_metal"]=metal
    if fine is not None and len(fg)>=2:accepted["fineness_per_mille"]=int(fine)
    if weight is not None and len(wg)>=2:accepted["weight_g"]=round(float(weight),4)
    if diam is not None and len(dg)>=2:accepted["diameter_mm"]=round(float(diam),3)

    # Precious-metal fineness without consensus weight is suppressed.
    if accepted.get("primary_metal") in ("Silver","Gold","Platinum","Palladium"):
        if accepted.get("fineness_per_mille") is not None and accepted.get("weight_g") is None:
            accepted.pop("fineness_per_mille",None)

    if not accepted:
        print(f"[MA-Shops specs] checked={checked} no 2-dealer consensus",flush=True)
        return None

    if accepted.get("primary_metal") and accepted.get("fineness_per_mille"):
        accepted["composition"]=f"{accepted['primary_metal']} (.{accepted['fineness_per_mille']:03d})"
    elif accepted.get("primary_metal"):
        accepted["composition"]=accepted["primary_metal"]
    if accepted.get("weight_g") is not None and accepted.get("fineness_per_mille") is not None:
        accepted["fine_metal_g"]=accepted["weight_g"]*accepted["fineness_per_mille"]/1000.0

    counts={k:len(v) for k,v in groups.items() if v}
    urls=[]
    field_evidence={}
    for field,grp in groups.items():
        if not grp:continue
        field_evidence[field]=[]
        for x in grp:
            field_evidence[field].append({
                "dealer":x.get("dealer"),"url":x.get("url"),"title":x.get("title"),
                "value":x.get(field),"evidence":x.get("evidence")
            })
            if x.get("url") and x["url"] not in urls:urls.append(x["url"])

    result={
        **accepted,
        "id":None,"title":query,"issuer":"","obverse_image":None,"reverse_image":None,
        "url":urls[0] if urls else None,
        "match_class":"MA_SHOPS_MULTI_LISTING_CONSENSUS","confidence":0.92,
        "spec_source":"MA-Shops multi-listing consensus",
        "data_provider":"MA-Shops",
        "spec_evidence_counts":counts,
        "spec_evidence_by_field":field_evidence,
        "spec_source_urls":urls,
        "listings_checked":checked,
    }
    print(f"[MA-Shops specs] checked={checked} accepted={accepted} counts={counts}",flush=True)
    cache_mashops_spec(coin,result)
    return result


def inspect_mashops_item_asset(url, timeout=7):
    """Inspect an MA-Shops item page and classify it using breadcrumb/category + page text.

    Returns a dict with:
      asset_type: COIN / BANKNOTE / UNKNOWN
      confidence
      breadcrumb
      title
    This is intentionally conservative: explicit MA-Shops Banknotes/Emergency-money
    breadcrumb evidence is a hard reject for CoinBids coin Price Research.
    """
    if not url or "ma-shops.com/" not in str(url):
        return {"asset_type":"UNKNOWN","confidence":0.0,"breadcrumb":"","title":""}
    try:
        r=SESSION.get(url,timeout=timeout,allow_redirects=True)
        if not r.ok:
            return {"asset_type":"UNKNOWN","confidence":0.0,"breadcrumb":"","title":""}
        soup=BeautifulSoup(r.text,"html.parser")
        h1=soup.find("h1")
        title=h1.get_text(" ",strip=True) if h1 else ""

        # MA-Shops shows a breadcrumb line near the top. Collect short links/text
        # near the first part of the document rather than the whole page.
        crumb_parts=[]
        for node in soup.find_all(["a","div","span","td"],limit=250):
            txt=" ".join(node.stripped_strings)
            if not txt or len(txt)>180:
                continue
            nt=norm(txt)
            if any(k in nt for k in (
                "banknotes","banknote","emergency money","notgeld",
                "coins banknotes","coins + banknotes","world coins","european coins",
                "coins","münzen","munzen","monnaies"
            )):
                crumb_parts.append(txt)
        breadcrumb=" > ".join(dict.fromkeys(crumb_parts[:12]))

        combined=" ".join([title,breadcrumb])

        # IMPORTANT: MA-Shops has a generic top-level breadcrumb/category such as
        # "Coins + Banknotes" on BOTH coin and banknote pages.  Treating the word
        # "Banknotes" in that generic navigation label as item-level evidence
        # caused every genuine coin detail page to be rejected before it was
        # counted, producing: [MA-Shops specs] checked=0.
        # Strip ONLY the generic mixed-category label before applying the hard
        # banknote test.  Real banknote evidence (Banknotes > Emergency money,
        # Notgeld, paper money, certificates, etc.) remains untouched/rejected.
        banknote_check_text=re.sub(r"\bcoins\s*(?:\+|&|and)?\s*banknotes\b", " ", combined, flags=re.I)
        banknote_check_text=re.sub(r"\bbanknotes\s*(?:\+|&|and)?\s*coins\b", " ", banknote_check_text, flags=re.I)
        if _ma_shops_banknote_evidence(banknote_check_text):
            return {"asset_type":"BANKNOTE","confidence":0.999,
                    "breadcrumb":breadcrumb,"title":title}

        # Explicit coin-category evidence can positively validate an otherwise
        # ambiguous title such as "USA Peace Dollar 1923 UNC-".
        c=norm(combined)
        coin_category_terms=("world coins","european coins","coins","münzen","munzen","monnaies")
        if any(term in c for term in coin_category_terms):
            return {"asset_type":"COIN","confidence":0.94,
                    "breadcrumb":breadcrumb,"title":title}

        cls,conf=classify_asset(combined)
        return {"asset_type":cls,"confidence":conf,
                "breadcrumb":breadcrumb,"title":title}
    except Exception as e:
        print(f"[MA-Shops asset-inspect] failed {url}: {type(e).__name__}: {e}",flush=True)
        return {"asset_type":"UNKNOWN","confidence":0.0,"breadcrumb":"","title":""}


def validate_ambiguous_mashops_assets(offers, payload, max_checks=18):
    """Second-stage asset validation for Price Research.

    Cheap/ambiguous listings are inspected on their actual MA-Shops item page.
    Banknotes are removed before lowest-price calculation. We only spend detail
    requests on offers whose title/category metadata is not already decisive.
    """
    if not offers:
        return offers,0,0

    checked=0
    rejected=0

    # Validate cheapest ambiguous offers first because those are the ones that can
    # corrupt "Lowest price found". Also inspect a small number of top results.
    ranked=sorted(
        list(offers),
        key=lambda o:(o.get("price") is None, o.get("price") if o.get("price") is not None else float("inf"))
    )

    keep_ids=set()
    for o in ranked:
        keep_ids.add(id(o))

    for o in ranked:
        if checked>=max_checks:
            break
        title=o.get("_match_text") or o.get("title","")
        cls,_=classify_asset(title)

        # Already explicit banknote -> no need to fetch; outer filter normally
        # removes these, but keep the rule defensive.
        if cls=="BANKNOTE":
            o["_asset_page_validation"]="BANKNOTE"
            rejected+=1
            continue

        # If title itself contains strong positive coin language, we still inspect
        # a few cheap results only when it is generic/ambiguous; "Peace Dollar"
        # gets a positive bonus below and is less likely to need rejection.
        a=norm(title)
        strong_coin_title=bool(re.search(r"\b(?:peace dollar|morgan dollar|trade dollar|"
                                          r"walking liberty|liberty dollar|eagle|half dollar|quarter dollar)\b",a))
        ambiguous = (cls=="UNKNOWN") or not strong_coin_title
        if not ambiguous:
            continue

        info=inspect_mashops_item_asset(o.get("url"))
        checked+=1
        o["_asset_page_validation"]=info.get("asset_type")
        o["_asset_breadcrumb"]=info.get("breadcrumb")
        o["_asset_page_title"]=info.get("title")
        if info.get("asset_type")=="BANKNOTE":
            rejected+=1
            continue

        # The detail-page H1 is stronger identity evidence than a noisy search
        # result row.  A listing that looked like "1 Dollar" in the result table
        # but opens as "Quarter Dollar" must never influence Lowest price.
        page_title=(info.get("title") or "").strip()
        if page_title and not passes_hard_filter(page_title,payload):
            o["_identity_page_validation"]="REJECT"
            o["_identity_page_reject_title"]=page_title
            rejected+=1
            continue
        if page_title:
            o["_identity_page_validation"]="ACCEPT"

    filtered=[o for o in offers
              if o.get("_asset_page_validation")!="BANKNOTE"
              and o.get("_identity_page_validation")!="REJECT"]
    return filtered,checked,rejected


def _coin_type_rank_bonus(text, payload):
    """Positive ranking signal for known coin-type wording; never a hard identity rule."""
    a=norm(text)
    coin=payload.get("coin") or {}
    country=canonical_country(coin.get("countryEN") or coin.get("country") or "")
    denom=norm(coin.get("denom") or "")
    year=str(coin.get("year") or "")

    bonus=0.0
    if country=="united states" and "dollar" in denom:
        # In 1921-1935 the standard US silver dollar type is Peace Dollar except
        # 1921 also has Morgan. This is ONLY a ranking bonus; the hard filters still
        # enforce denomination/year/country and asset type.
        if re.search(r"\bpeace dollar\b",a):
            bonus+=0.18
        if re.search(r"\bmorgan dollar\b",a):
            bonus+=0.10
        if "silver dollar" in a:
            bonus+=0.08
    return bonus

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

def _get_pg_connection():
    global _PG_POOL
    if not DATABASE_URL:return None
    if _PG_POOL is None:
        try:
            import psycopg2.pool
            _PG_POOL=psycopg2.pool.SimpleConnectionPool(1,5,DATABASE_URL)
        except Exception as e:
            print(f"[coin-specs-pg] pool init failed: {type(e).__name__}: {e}")
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

def local_coin_spec_match(coin):
    country=str(coin.get("countryEN") or coin.get("country") or "").strip().lower()
    year_txt=str(coin.get("year") or "").strip()
    try: year=int(year_txt) if year_txt else None
    except: year=None
    denom=_spec_denom_value(coin.get("denom"))
    if not country or denom is None:return None
    for r in _COIN_SPECS:
        countries=[str(x).lower() for x in r.get("countries",[])]
        if country not in countries:continue
        if abs(float(r.get("denomination",-999))-denom)>1e-6:continue
        yf=r.get("year_from"); yt=r.get("year_to")
        if year is not None and ((yf is not None and year<int(yf)) or (yt is not None and year>int(yt))):continue
        return {
            "id":None,"title":f"{coin.get('denom','')} {coin.get('countryEN') or coin.get('country','')} {coin.get('year','')}".strip(),
            "issuer":"","composition":r.get("composition"),"weight_g":r.get("weight_g"),
            "diameter_mm":r.get("diameter_mm"),"obverse_image":None,"reverse_image":None,
            "url":None,"match_class":"LOCAL_SPEC","confidence":r.get("confidence",1.0),
            "spec_source":r.get("source","CoinBids curated specifications"),"data_provider":"CoinBids local specifications"
        }
    return None

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

@app.post("/api/coin-lookup")
def coin_lookup():
    payload=request.get_json(silent=True) or {};coin=payload.get("coin") or {}
    query=" ".join(str(x) for x in [coin.get("countryEN") or coin.get("country"),coin.get("denom"),coin.get("year"),coin.get("variant")] if x).strip()
    if not query:query=(payload.get("raw_query") or "").strip()
    if not query:return jsonify({"error":"empty query"}),400
    # Physical-spec priority: identity-validated MA-Shops first.
    # This prevents an older/local catalogue record from overriding explicit
    # weight/fineness shown on the matching MA-Shops item page.
    ma_spec=mashops_spec_fallback(coin,(payload.get("raw_query") or "").strip())
    if ma_spec:
        return jsonify({"match":ma_spec,"provider":"ma_shops",
                        "note":"Physical specifications extracted from an identity-validated MA-Shops item page."})

    # Zero-quota local fallback when MA-Shops does not expose reliable specs.
    local_match=pg_coin_spec_match(coin)
    provider="local_pg"
    if not local_match:
        local_match=local_coin_spec_match(coin)
        provider="local"
    if local_match:
        return jsonify({"match":local_match,"provider":provider,
                        "note":"Physical specifications resolved from the local catalogue because no validated MA-Shops physical specification was available."})

    results,err=numista_search(query,category="coin",count=12,year=coin.get("year"))
    if err:return jsonify({"match":None,"error":err}),200
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
        return jsonify({"match":None,"note":"No reliable Numista match satisfied country + denomination + year.","rejected":rejected[:8]})
    # Multiple validated candidates with no explicit variant = ambiguous, do not choose arbitrarily.
    if len(survivors)>1 and not (coin.get("variant") or "").strip():
        candidates=[]
        for conf,detail,cand,_ in survivors[:5]:
            tid=numista_pick(detail,"id") or numista_pick(cand,"id")
            candidates.append({"id":tid,"title":numista_pick(detail,"title") or numista_pick(cand,"title"),"url":f"https://en.numista.com/catalogue/pieces{tid}.html","confidence":conf})
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
    return jsonify({"provider":"numista","match":{
        "id":tid,"title":numista_pick(detail,"title") or numista_pick(best,"title"),
        "issuer":numista_pick(detail,"issuer.name") or flatten_text(numista_pick(detail,"issuer")),
        "composition":composition,"weight_g":weight,
        "diameter_mm":diameter,
        "obverse_image":numista_pick(detail,"obverse.picture","obverse_picture","obverse.thumbnail"),
        "reverse_image":numista_pick(detail,"reverse.picture","reverse_picture","reverse.thumbnail"),
        "url":f"https://en.numista.com/catalogue/pieces{tid}.html","match_class":"EXACT" if conf>=.75 else "STRONG","confidence":conf
    }})

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

_METAL_SPOT_CACHE={"at":0.0,"data":None}
_METAL_SPOT_LOCK=threading.Lock()
_METAL_SPOT_TTL_SECONDS=60

def _ecb_usd_per_eur():
    """ECB reference rate: USD quoted per EUR. Cached by the metal endpoint."""
    r=SESSION.get(
        "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        timeout=5
    )
    r.raise_for_status()
    m=re.search(r"currency=['\"]USD['\"]\s+rate=['\"]([0-9.]+)['\"]",r.text,re.I)
    if not m:
        raise ValueError("USD rate missing from ECB reference-rate XML")
    return float(m.group(1))

@app.get("/api/metal-spot")
def metal_spot():
    """Current precious-metal spot prices and EUR/gram conversion inputs.

    Metal feed: gold-api.com XAU/XAG/XPT/XPD real-time USD/troy-ounce prices.
    FX: ECB EUR/USD reference rate, with the existing FX service only as fallback.
    This endpoint is cached for 60 seconds to avoid unnecessary external calls.
    """
    now=time.time()
    with _METAL_SPOT_LOCK:
        cached=_METAL_SPOT_CACHE.get("data")
        if cached and now-_METAL_SPOT_CACHE.get("at",0)<_METAL_SPOT_TTL_SECONDS:
            out=dict(cached); out["cache"]="hit"
            return jsonify(out)

    symbols={"gold":"XAU","silver":"XAG","platinum":"XPT","palladium":"XPD"}
    prices={}; errors=[]; metal_sources=[]
    for name,symbol in symbols.items():
        try:
            r=SESSION.get(f"https://api.gold-api.com/price/{symbol}",timeout=5)
            r.raise_for_status()
            d=r.json()
            val=d.get("price")
            if val is None: val=d.get("ask") or d.get("bid")
            val=float(val)
            if val<=0: raise ValueError(f"invalid {symbol} price")
            prices[name]=val
            if "Gold API" not in metal_sources: metal_sources.append("Gold API")
        except Exception as e:
            errors.append(f"{symbol}: {type(e).__name__}: {e}")

    # Existing independent fallback for the two principal coin metals.
    if "gold" not in prices or "silver" not in prices:
        try:
            r=SESSION.get("https://data-asg.goldprice.org/dbXRates/USD",timeout=5)
            r.raise_for_status()
            item=(r.json().get("items") or [None])[0]
            if item:
                if "gold" not in prices and item.get("xauPrice") is not None:
                    prices["gold"]=float(item["xauPrice"])
                if "silver" not in prices and item.get("xagPrice") is not None:
                    prices["silver"]=float(item["xagPrice"])
                if "goldprice.org fallback" not in metal_sources:
                    metal_sources.append("goldprice.org fallback")
        except Exception as e:
            errors.append(f"metal fallback: {type(e).__name__}: {e}")

    if not prices:
        return jsonify({"error":"live metal price unavailable","details":errors}),503

    fx_source="ECB"
    try:
        usd_per_eur=_ecb_usd_per_eur()
    except Exception as e:
        errors.append(f"ECB FX: {type(e).__name__}: {e}")
        rates=fx_rates()
        usd_per_eur=rates.get("USD")
        fx_source="FX fallback"
        if not usd_per_eur:
            return jsonify({"error":"EUR exchange rate unavailable","details":errors}),503
        usd_per_eur=float(usd_per_eur)

    # Since ECB quotes USD per EUR: EUR = USD / (USD per EUR).
    usd_to_eur=1.0/usd_per_eur
    oz_g=31.1034768
    out={
        "source":" + ".join(metal_sources),
        "fx_source":fx_source,
        "unit":"USD/troy oz",
        "troy_ounce_g":oz_g,
        "usd_per_eur":usd_per_eur,
        "usd_to_eur":usd_to_eur,
        "fetched_at":now
    }
    for name,val in prices.items():
        out[f"{name}_usd_oz"]=val
        out[f"{name}_eur_g"]=(val/oz_g)*usd_to_eur
    if errors: out["warnings"]=errors

    with _METAL_SPOT_LOCK:
        _METAL_SPOT_CACHE.update(at=now,data=out)
    return jsonify(out)

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

    # These diagnostics remain in the public response schema even when the
    # corresponding detail-page enrichment paths are disabled/skipped.
    # Initialize them before ANY branch so diagnostics can never crash an
    # otherwise successful Price Research request.
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
        # A slow MA-Shops variant must never consume the whole synchronous
        # Gunicorn request lifetime. Keep every query that finishes within the
        # bounded search window and ignore/cancel laggards.
        done,not_done=wait(set(future_to_query.keys()),timeout=12)
        for future in done:
            q=future_to_query[future]
            try:
                ma_offers,ma_url,ma_err=future.result()
            except Exception as e:
                ma_offers,ma_url,ma_err=[],None,str(e)
            if ma_url:used.append({"query":q,"source":"MA-Shops","url":ma_url})
            if ma_err:errors.append({"query":q,"source":"MA-Shops","error":ma_err})
            all_offers.extend(ma_offers)
        if not_done:
            print(f"[coin-search] search deadline: completed={len(done)}/{len(future_to_query)}; ignoring {len(not_done)} slow variant(s)",flush=True)
            for future in not_done:
                future.cancel()
    finally:
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
        asset_text=" ".join([match_text]+[
            str(o.get(k,"") or "") for k in (
                "category","categories","breadcrumb","breadcrumbs",
                "path","section","description"
            )
        ])
        asset,conf=classify_asset(asset_text);o["asset_type"]=asset;o["asset_confidence"]=conf
        if asset=="BANKNOTE":rejected["asset"]+=1;continue
        if product_scope(match_text)!="SINGLE_COIN":rejected["scope"]+=1;continue
        # Title-level denomination veto: the noisy MA-Shops row may contain the
        # requested denomination in a separate metadata cell.  It may fill in a
        # missing title denomination, but it cannot override an explicitly
        # different denomination stated by the product title itself.
        _coin_obj=(payload.get("coin") or {})
        _target_denom=str(_coin_obj.get("denom") or _coin_obj.get("denomination") or "").strip()
        # Resolver labels can be non-numeric ("United States dollar"). The raw
        # user query is authoritative for face value when it explicitly states it.
        _raw_target=str(payload.get("raw_query") or "").strip()
        if not parse_denomination(_target_denom):
            _raw_parsed=parse_denomination(_raw_target)
            if _raw_parsed:
                _target_denom=f"{_raw_parsed[0]:g} {_raw_parsed[1]}"
        _explicit_title_denoms=extract_denominations(o.get("title",""))
        if _target_denom and _explicit_title_denoms and denomination_conflicts(_target_denom,o.get("title","")):
            print(f"[coin-search] reject explicit denomination: target={_target_denom!r} title={o.get('title','')!r} denoms={_explicit_title_denoms}",flush=True)
            rejected["identity"]+=1;continue
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
    # Second-stage MA-Shops asset validation: ambiguous cheap listings are checked
    # against the actual MA-Shops item page/breadcrumb before they can affect price.
    # Verify a wider cheapest window against the actual item-page title. This
    # uses the same hard identity contract as the specs pipeline, preventing
    # search-row false positives (notably 1 Dollar -> Quarter Dollar).
    # TIMEOUT-SAFE: explicit denomination conflicts have already been hard-rejected above.
    # Keep real item-page validation, but cap it to the cheapest 4 surviving candidates.
    # This preserves asset/identity protection while avoiding 12 sequential MA-Shops
    # network waits that can exceed the Gunicorn worker timeout.
    valid,asset_detail_checked,asset_detail_rejected=validate_ambiguous_mashops_assets(
        valid,payload,max_checks=4
    )
    if asset_detail_rejected:
        rejected["asset"]+=asset_detail_rejected
    print(f"[coin-search] detail-identity checked={asset_detail_checked} rejected_asset_or_identity={asset_detail_rejected} valid_after={len(valid)}",flush=True)

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

    # Resource-safe spec/weight bridge:
    # /api/coin-search NEVER launches MA-Shops detail-page specification scraping.
    # That work belongs to /api/coin-lookup. Price Research can reuse a previously
    # cached validated weight, but it must not block or kill the Gunicorn worker.
    if include_shipping and not item_weight_g:
        _coin_for_specs=payload.get("coin") or {}
        _cached_spec=cached_mashops_spec(_coin_for_specs)
        if _cached_spec and _cached_spec.get("weight_g"):
            item_weight_g=_cached_spec.get("weight_g")
            shipping_weight_source="ma_shops_cached_spec"
        else:
            print("[shipping-db] no cached coin weight; skipping live spec scrape inside coin-search",flush=True)

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

    # FINAL PRICE ORDERING: only after every asset/scope/identity gate.
    # Lowest price means the cheapest VALID requested single coin, never the
    # cheapest raw MA-Shops text hit.
    valid.sort(
        key=lambda o:(
            o.get("total") is None,
            o.get("total") if o.get("total") is not None else float("inf"),
            o.get("price") if o.get("price") is not None else float("inf"),
            -(o.get("identity_match_score") or 0),
            -o.get("_score",0)
        )
    )
    print("[coin-search] cheapest-valid="+str([
        {"title":x.get("title"),"price":x.get("price"),"shipping":x.get("shipping"),
         "total":x.get("total"),"scope":product_scope(x.get("_match_text") or x.get("title","")),
         "denoms":extract_denominations(x.get("title",""))}
        for x in valid[:5]
    ]),flush=True)

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
            "asset_detail_pages_checked":asset_detail_checked,
            "asset_detail_banknotes_rejected":asset_detail_rejected,
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


# ============================================================
# TEMPORARY CATALOGUE CONSENSUS DIAGNOSTIC V4 — DRY RUN ONLY
# Added 2026-08-22.
#
# V4 changes:
# - Early-stop cross-year fallback.
# - Hard overall time budget so Gunicorn is never allowed to
#   kill the request just because fallback keeps probing years.
# - Graceful TIME_BUDGET_EXCEEDED / NO_CONSENSUS result.
# - No database writes.
# - Existing production routes untouched.
# ============================================================

CATALOG_DIAGNOSTIC_KEY = os.environ.get("CATALOG_DIAGNOSTIC_KEY", "").strip()

_CATALOG_DIAGNOSTIC_TEST_COINS = [
    {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1922","variant":"Peace Dollar"},
    {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1923","variant":"Peace Dollar"},
    {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1924","variant":"Peace Dollar"},
    {"country":"United States","countryEN":"United States","denom":"1 Dollar","year":"1925","variant":"Peace Dollar"},
    {"country":"United States","countryEN":"United States","denom":"1/2 Dollar","year":"1964","variant":"Kennedy Half Dollar"},
]

def _catalog_unique_dealer_rows(rows):
    out=[]; seen=set()
    for row in rows or []:
        dealer=(row.get("dealer") or "").strip().lower()
        url=(row.get("url") or "").strip().lower()
        key=dealer or url
        if not key or key in seen:
            continue
        seen.add(key); out.append(row)
    return out

def _catalog_exact_text_consensus(rows, min_dealers=2):
    rows=_catalog_unique_dealer_rows(rows)
    groups={}
    for row in rows:
        value=row.get("value")
        if value in (None,""): continue
        groups.setdefault(str(value).strip().lower(),[]).append(row)
    if not groups: return None,[],0
    _,grp=max(groups.items(),key=lambda kv:len(kv[1]))
    if len(grp)<min_dealers: return None,grp,len(grp)
    return grp[0].get("value"),grp,len(grp)

def _catalog_exact_numeric_consensus(rows, decimals=2, min_dealers=2):
    rows=_catalog_unique_dealer_rows(rows)
    groups={}
    for row in rows:
        try: value=float(row.get("value"))
        except Exception: continue
        key=round(value,decimals)
        groups.setdefault(key,[]).append(row)
    if not groups: return None,[],0
    key,grp=max(groups.items(),key=lambda kv:len(kv[1]))
    if len(grp)<min_dealers: return None,grp,len(grp)
    return float(key),grp,len(grp)

def _catalog_cluster_summary(rows):
    rows=_catalog_unique_dealer_rows(rows)
    vals=[]
    for row in rows:
        try: vals.append(float(row.get("value")))
        except Exception: pass
    if not vals: return None
    vals=sorted(vals); n=len(vals)
    med=vals[n//2] if n%2 else (vals[n//2-1]+vals[n//2])/2.0
    return {
        "dealer_count":len(vals),
        "min":round(min(vals),5),
        "max":round(max(vals),5),
        "spread":round(max(vals)-min(vals),5),
        "median":round(med,5),
        "values":[round(v,5) for v in vals],
    }

def _catalogue_current_year_consensus(spec):
    if not spec:
        return {
            "catalogue_status":"NO_CONSENSUS",
            "canonical":{},
            "observed":{},
            "field_confidence":{},
            "notes":["No accepted MA-Shops evidence for this identity."],
        }

    ev=spec.get("spec_evidence_by_field") or {}
    metal_rows=_catalog_unique_dealer_rows(ev.get("primary_metal") or [])
    fine_rows=_catalog_unique_dealer_rows(ev.get("fineness_per_mille") or [])
    weight_rows=_catalog_unique_dealer_rows(ev.get("weight_g") or [])
    diam_rows=_catalog_unique_dealer_rows(ev.get("diameter_mm") or [])

    metal,_,metal_n=_catalog_exact_text_consensus(metal_rows,2)
    fine,_,fine_n=_catalog_exact_numeric_consensus(fine_rows,0,2)
    weight,_,weight_n=_catalog_exact_numeric_consensus(weight_rows,2,2)
    diam,_,diam_n=_catalog_exact_numeric_consensus(diam_rows,1,2)

    canonical={}
    if metal is not None: canonical["primary_metal"]=metal
    if fine is not None: canonical["fineness_per_mille"]=int(round(float(fine)))
    if weight is not None: canonical["weight_g"]=round(float(weight),2)
    if diam is not None: canonical["diameter_mm"]=round(float(diam),1)

    if canonical.get("primary_metal") and canonical.get("fineness_per_mille") is not None:
        canonical["composition"]=f"{canonical['primary_metal']} (.{canonical['fineness_per_mille']:03d})"
    if canonical.get("weight_g") is not None and canonical.get("fineness_per_mille") is not None:
        canonical["fine_metal_g"]=round(
            canonical["weight_g"]*canonical["fineness_per_mille"]/1000.0,6
        )

    return {
        "catalogue_status":(
            "VERIFIED_COMPLETE" if all(k in canonical for k in ("primary_metal","fineness_per_mille","weight_g","diameter_mm"))
            else "VERIFIED_METAL_SPECS" if all(k in canonical for k in ("primary_metal","fineness_per_mille","weight_g"))
            else "PARTIAL_NEEDS_CANONICAL_WEIGHT" if all(k in canonical for k in ("primary_metal","fineness_per_mille"))
            else "PARTIAL_UNVERIFIED"
        ),
        "canonical":canonical,
        "observed":{
            "weight_g":_catalog_cluster_summary(weight_rows),
            "diameter_mm":_catalog_cluster_summary(diam_rows),
            "metal_dealers":len(metal_rows),
            "fineness_dealers":len(fine_rows),
        },
        "field_confidence":{
            "primary_metal":{"status":"VERIFIED" if metal is not None else "UNVERIFIED","agreeing_dealers":metal_n,"required_dealers":2},
            "fineness_per_mille":{"status":"VERIFIED" if fine is not None else "UNVERIFIED","agreeing_dealers":fine_n,"required_dealers":2},
            "weight_g":{"status":"VERIFIED_CURRENT_YEAR" if weight is not None else "OBSERVATIONS_ONLY","agreeing_dealers":weight_n,"required_dealers":2},
            "diameter_mm":{"status":"VERIFIED_CURRENT_YEAR" if diam is not None else "OBSERVATIONS_ONLY","agreeing_dealers":diam_n,"required_dealers":2},
        },
        "notes":[],
        "source":{
            "provider":"MA-Shops",
            "listings_checked":spec.get("listings_checked"),
            "source_urls":spec.get("spec_source_urls") or [],
        },
    }

def _catalog_series_year_candidates(coin, radius=4):
    try: y=int(str(coin.get("year") or "").strip())
    except Exception: return []
    years=[]
    for d in range(1,radius+1):
        years.extend([y-d,y+d])
    return [yy for yy in years if yy>0]

def _catalog_series_weight_fallback(coin, target_canonical, time_budget_seconds=12.0, max_years=4):
    variant=str(coin.get("variant") or "").strip()
    if not variant:
        return {"accepted":False,"reason":"variant_required_for_cross_year_fallback"}

    target_metal=target_canonical.get("primary_metal")
    target_fine=target_canonical.get("fineness_per_mille")
    if not target_metal or target_fine is None:
        return {"accepted":False,"reason":"verified_target_metal_and_fineness_required"}

    started=time.time()
    weight_groups={}
    year_reports=[]
    years_checked=0

    for yy in _catalog_series_year_candidates(coin):
        if years_checked>=max_years:
            break
        if time.time()-started>=time_budget_seconds:
            return {
                "accepted":False,
                "reason":"TIME_BUDGET_EXCEEDED",
                "years_checked":years_checked,
                "elapsed_seconds":round(time.time()-started,3),
                "year_reports":year_reports,
            }

        probe=dict(coin); probe["year"]=str(yy)
        query=" ".join(
            str(probe.get(k) or "").strip()
            for k in ("countryEN","denom","year","variant")
            if probe.get(k)
        ).strip()

        try:
            spec=mashops_spec_fallback(probe,query)
        except BaseException as e:
            # SystemExit can be injected by Gunicorn only if we already exceeded
            # its timeout; ordinary source/parser errors should be reported and skipped.
            if isinstance(e, SystemExit):
                raise
            year_reports.append({"year":yy,"status":"ERROR","error":type(e).__name__})
            years_checked+=1
            continue

        years_checked+=1
        cur=_catalogue_current_year_consensus(spec)
        c=cur.get("canonical") or {}

        if c.get("primary_metal")!=target_metal or c.get("fineness_per_mille")!=target_fine:
            year_reports.append({"year":yy,"status":"SKIPPED_COMPOSITION_MISMATCH","canonical":c})
            continue

        ev=(spec or {}).get("spec_evidence_by_field") or {}
        rows=_catalog_unique_dealer_rows(ev.get("weight_g") or [])
        accepted_rows=0
        for row in rows:
            try: w=round(float(row.get("value")),2)
            except Exception: continue
            dealer=(row.get("dealer") or row.get("url") or "").strip().lower()
            if not dealer: continue
            weight_groups.setdefault(w,[]).append({
                "year":yy,"dealer":dealer,"url":row.get("url"),"value":w
            })
            accepted_rows+=1

        year_reports.append({"year":yy,"status":"USED","weight_observations":accepted_rows})

        # EARLY STOP: as soon as any weight reaches the acceptance threshold,
        # return immediately rather than probing more years.
        candidates=[]
        for weight,rows2 in weight_groups.items():
            uniq={(r["year"],r["dealer"]):r for r in rows2}
            rr=list(uniq.values())
            years=sorted(set(r["year"] for r in rr))
            dealers=sorted(set(r["dealer"] for r in rr))
            if len(years)>=2 and len(rr)>=3:
                candidates.append({
                    "weight_g":weight,
                    "supporting_years":years,
                    "observation_count":len(rr),
                    "dealer_count":len(dealers),
                    "evidence":rr,
                })

        if candidates:
            candidates.sort(
                key=lambda x:(x["observation_count"],len(x["supporting_years"]),x["dealer_count"]),
                reverse=True
            )
            best=candidates[0]
            return {
                "accepted":True,
                "weight_g":best["weight_g"],
                "supporting_years":best["supporting_years"],
                "observation_count":best["observation_count"],
                "dealer_count":best["dealer_count"],
                "evidence":best["evidence"],
                "years_checked":years_checked,
                "elapsed_seconds":round(time.time()-started,3),
                "method":"MA_SHOPS_SAME_SERIES_CROSS_YEAR_EARLY_STOP",
                "year_reports":year_reports,
            }

    return {
        "accepted":False,
        "reason":"no_cross_year_weight_met_threshold",
        "years_checked":years_checked,
        "elapsed_seconds":round(time.time()-started,3),
        "year_reports":year_reports,
    }

@app.get("/api/admin/catalog-builder-test")
def catalog_builder_test():
    if not CATALOG_DIAGNOSTIC_KEY:
        return jsonify({"ok":False,"error":"catalog_diagnostic_disabled"}),503

    supplied=(request.args.get("key") or "").strip()
    if not supplied or not secrets.compare_digest(supplied,CATALOG_DIAGNOSTIC_KEY):
        return jsonify({"ok":False,"error":"unauthorized"}),401

    try: idx=int(request.args.get("coin","0"))
    except Exception:
        return jsonify({"ok":False,"error":"coin must be an integer from 0 to 4"}),400
    if idx<0 or idx>=len(_CATALOG_DIAGNOSTIC_TEST_COINS):
        return jsonify({"ok":False,"error":"coin_out_of_range"}),400

    coin=dict(_CATALOG_DIAGNOSTIC_TEST_COINS[idx])
    query=" ".join(
        str(coin.get(k) or "").strip()
        for k in ("countryEN","denom","year","variant")
        if coin.get(k)
    ).strip()

    started=time.time()
    try:
        raw_spec=mashops_spec_fallback(coin,query)
        catalogue=_catalogue_current_year_consensus(raw_spec)
        canonical=catalogue.get("canonical") or {}

        fallback=None
        # Keep total request budget comfortably below common 30s Gunicorn timeout.
        total_budget=18.0
        elapsed=time.time()-started
        remaining=max(0.0,total_budget-elapsed)

        if (
            "weight_g" not in canonical
            and canonical.get("primary_metal")
            and canonical.get("fineness_per_mille") is not None
            and remaining>2.0
        ):
            fallback=_catalog_series_weight_fallback(
                coin,
                canonical,
                time_budget_seconds=min(10.0,max(2.0,remaining-2.0)),
                max_years=3,
            )
            if fallback.get("accepted"):
                canonical["weight_g"]=round(float(fallback["weight_g"]),2)
                canonical["fine_metal_g"]=round(
                    canonical["weight_g"]*canonical["fineness_per_mille"]/1000.0,6
                )
                catalogue["field_confidence"]["weight_g"]={
                    "status":"VERIFIED_SERIES_FALLBACK",
                    "method":fallback.get("method"),
                    "supporting_years":fallback.get("supporting_years"),
                    "observation_count":fallback.get("observation_count"),
                    "dealer_count":fallback.get("dealer_count"),
                }
                catalogue["catalogue_status"]=(
                    "VERIFIED_COMPLETE" if "diameter_mm" in canonical else "VERIFIED_METAL_SPECS"
                )
                catalogue["notes"].append(
                    "Canonical weight filled from same-series cross-year MA-Shops evidence with early stop."
                )
        elif "weight_g" not in canonical and remaining<=2.0:
            fallback={
                "accepted":False,
                "reason":"TIME_BUDGET_EXCEEDED_BEFORE_FALLBACK",
                "remaining_seconds":round(remaining,3),
            }

        catalogue["series_weight_fallback"]=fallback
        catalogue["metal_specs_status"]=(
            "VERIFIED" if all(k in canonical for k in ("primary_metal","fineness_per_mille","weight_g"))
            else "UNVERIFIED"
        )
        catalogue["dimensions_status"]="VERIFIED" if "diameter_mm" in canonical else "UNVERIFIED"

        return jsonify({
            "ok":True,
            "mode":"DRY_RUN_V4",
            "database_writes":False,
            "coin_index":idx,
            "coin":coin,
            "query":query,
            "elapsed_seconds":round(time.time()-started,3),
            "catalogue":catalogue,
            "message":"Catalogue V4 evaluated; database unchanged.",
        })
    except Exception as e:
        app.logger.exception("catalog-builder diagnostic V4 failed")
        return jsonify({
            "ok":False,
            "mode":"DRY_RUN_V4",
            "database_writes":False,
            "coin_index":idx,
            "coin":coin,
            "query":query,
            "elapsed_seconds":round(time.time()-started,3),
            "error":type(e).__name__,
            "message":str(e)[:500],
        }),500

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
