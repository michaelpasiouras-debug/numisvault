from flask import Flask, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
import requests, re, html as ihtml, urllib.parse, time, os, math, threading, json
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import email.utils
from datetime import timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
try:
    from coin_identity_resolver import resolve_coin_identity, get_resolver, transliterate_greek
    RESOLVER_AVAILABLE=True
except Exception as _resolver_import_err:
    RESOLVER_AVAILABLE=False
    transliterate_greek=lambda s:s or ""
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
from multilingual_country_aliases import normalize_country_aliases_in_text

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

@app.get("/coinbids-logo.png")
def coinbids_logo_png():
    # Critical public brand asset used by header, hero and footer.
    return send_from_directory(APP_DIR,"coinbids-logo.png",mimetype="image/png")

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
    "coinbids-logo.png":"image/png",
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
    s = re.sub(r"\s+"," ",s).strip()
    return normalize_country_aliases_in_text(s)

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
    "greece":["greece","greek","hellas","ellada","griechenland","griekenland","grèce","grece","grecia","grécia","grecja","recko","řecko","grecko","grécko","grcka","grčka","gorogorszag","görögország","yunanistan","graekenland","grækenland","grekland","kreikka","ελλαδα"],
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
# Explicitly non-numismatic products that can appear in broad MA-Shops searches.
# These are only used as a NEGATIVE signal; UNKNOWN titles remain eligible so
# ordinary coin listings do not need to contain the literal word "coin".
NON_COIN_PRODUCT_TERMS=(
    "postcard","post card","postal card","postkarte","grußkarte","grusskarte",
    "carte postale","cartolina","postkarte gebraucht","briefmarke","stamp",
    "philately","philatelic"
)
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
    "euro":["euro","euros","eur","evro","ευρω","ευρώ"],
    "dollar":["dollar","dollars","usd","dolar","dollaro","δολαριο","δολαρια"],
    "pound":["pound","pounds","gbp","sterling"],
    "franc":["franc","francs","franken","frank","frs"],
    "yen":["yen"],
    "yuan":["yuan","renminbi"],
    # Historical / pre-euro European currencies — a coin listing rarely says
    # "Greece" in a Greek-numismatic term the same way a search query does,
    # so recognizing every common spelling variant (English/German/French/
    # native-plural) is what actually prevents a real, correct match from
    # being silently rejected as "wrong denomination".
    "drachma":["drachma","drachmas","drachmai","drachmae","drachme","drachmen","drachmi","drakhma","drakhmai","drachm","δραχμη","δραχμες","δραχμαι"],
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

def parse_target_denomination(s):
    """Parse the REQUESTED denomination, including resolver display labels.

    The resolver deliberately returns human-readable currency names such as
    "Greek drachma" or "Deutsche Mark".  Those are excellent UI labels but the
    old strict parser expected the denomination unit immediately after the
    number, so "5 Greek drachma" parsed as None.  denomination_matches() then
    treated a missing target parse as "no denomination constraint" and could
    accept 5 Pfennig, postcards, etc.

    Listing-side parsing stays strict.  Only the TARGET gets this tolerant
    fallback: number + a known currency/denomination alias anywhere in the
    short remainder of the target label.
    """
    direct=parse_denomination(s)
    if direct:return direct
    a=norm(s).replace(",",".")
    m=re.search(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)",a)
    if not m:return None
    try: val=float(m.group(1))
    except Exception:return None
    tail=a[m.end():].strip()
    if not tail:return None

    hits=[]
    for alias,canon in _ALT_TO_CANON.items():
        # aliases are already normalized lower-case strings; use conservative
        # token boundaries so e.g. "mark" cannot match inside "market".
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])",tail,re.I):
            hits.append((len(alias),canon))
    # Symbols are handled by parse_denomination() above; this fallback is for
    # human-readable resolver labels only.
    if not hits:return None
    hits.sort(reverse=True)
    return val,hits[0][1]

def denomination_matches(target, title):
    td=parse_target_denomination(target)
    # A non-empty target that cannot be parsed must NEVER disable the hard
    # denomination gate. Fail closed instead of accepting every listing.
    if not td:return False if str(target or "").strip() else True
    candidates=re.findall(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(" + DENOM_UNIT_ALT + r")(?![a-z])",norm(title),re.I)
    for v,u in candidates:
        d=parse_denomination(f"{v} {u}")
        if d and abs(d[0]-td[0])<1e-9:
            if d[1]==td[1]:
                return True
            # Marketplace shorthand: modern Greek "drachma" listings are
            # sometimes titled "drachm". Treat only this pair as equivalent
            # at comparison time, without globally collapsing the distinct
            # ancient "drachm" denomination in the alias table.
            if {d[1], td[1]} == {"drachma", "drachm"}:
                return True
    return False

def classify_asset(title):
    a=norm(title)
    bank=sum(1 for x in BANKNOTE_TERMS if x in a)
    coin=sum(1 for x in COIN_TERMS if x in a)
    other=sum(1 for x in NON_COIN_PRODUCT_TERMS if x in a)
    if re.search(r"\bp[- ]?\d{1,5}[a-z]?\b",a,re.I): bank+=3
    if bank>=2 and bank>coin:return "BANKNOTE",min(.99,.65+.08*bank)
    if other>=1 and coin==0:return "OTHER",min(.99,.82+.04*other)
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
            if not country and b.get("country"):
                country=b["country"]; coin["country"]=country
            if not year and b.get("year"):
                year=str(b["year"]); coin["year"]=year
            if not denom and b.get("denomination_value") is not None:
                unit=b.get("currency") or b.get("currency_code") or ""
                denom=f'{b["denomination_value"]:g} {unit}'.strip()
                coin["denom"]=denom
            resolver_queries=b.get("search_variants") or []

    # Raw-only API callers do not have the frontend parser's separate theme
    # field. Derive only the descriptive residue here so known same-year/same-
    # denomination issues (e.g. Antikythera vs Lord Byron) remain strictly
    # separated. This mirrors parseCoinText() without guessing a theme when the
    # raw text contains only structural identity fields.
    if raw and not str(coin.get("theme") or "").strip():
        theme_text=norm(raw)
        if country:
            cn=norm(country)
            if cn:
                theme_text=re.sub(rf"(?<![a-z0-9]){re.escape(cn)}(?![a-z0-9])"," ",theme_text,count=1)
        if year:
            theme_text=re.sub(rf"(?<!\d){re.escape(str(year))}(?!\d)"," ",theme_text)
        # Remove denomination numbers and every known unit/currency spelling;
        # leave all other words exactly as normalized so multilingual issue
        # matching can still use them.
        theme_text=re.sub(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)"," ",theme_text)
        for _aliases in CURRENCY_UNIT_ALIASES.values():
            for _al in sorted(set(_aliases),key=len,reverse=True):
                _an=norm(_al)
                if _an:
                    theme_text=re.sub(rf"(?<![a-z0-9]){re.escape(_an)}(?![a-z0-9])"," ",theme_text)
        theme_text=re.sub(r"(?<![a-z0-9])(?:coin|coins|proof|pp|unc|uncirculated|bu|fdc|silver|gold|argento|argent|silber|zilver|ασημι|ασημένιο)(?![a-z0-9])"," ",theme_text,re.I)
        theme_text=re.sub(r"\s+"," ",theme_text).strip(" -")
        if theme_text:
            coin["theme"]=theme_text

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

# ============================================================================
# GENERIC MULTILINGUAL THEME-WORD MATCHING
# ============================================================================
# Two complementary, purely LOCAL/offline techniques — no translation API,
# no network call, no new failure mode in the search hot path:
#
# 1. THEME_WORD_TRANSLATIONS: a small, curated dictionary of common
#    commemorative/numismatic THEME vocabulary (anniversary, independence,
#    battle, olympics, coronation, ...) with known translations in the
#    languages MA-Shops dealers most commonly use (EN/DE/FR/IT/ES/GR). Any
#    word the user types that appears in this dictionary is recognized in
#    ALL its listed language forms — this is what generalizes the original
#    Antikythera-Mechanism fix to "anniversary"/"Jubiläum"/"anniversaire",
#    "battle"/"Schlacht"/"bataille", etc. without hand-seeding every coin.
#
# 2. _theme_fuzzy_hit(): a conservative fuzzy/transliteration check for
#    PROPER NOUNS that no dictionary can cover (place/person names like
#    "Antikythera"/"Anticythère"/"Anticitera"/"Αντικύθηρα") — these mostly
#    just get spelled slightly differently per language rather than
#    translated outright, so similarity matching (reusing the same
#    SequenceMatcher + Greek transliteration approach already used in
#    coin_identity_resolver.py) catches them without an explicit alias.
#    Threshold is deliberately conservative (0.82) and only applied to
#    words of at least 5 characters, to avoid short/common-word false
#    positives.
# ============================================================================
THEME_WORD_TRANSLATIONS=[
    {"anniversary","jubiläum","jubilaeum","anniversaire","anniversario","aniversario","επετειος","επέτειος"},
    {"independence","unabhängigkeit","unabhaengigkeit","indépendance","independance","indipendenza","independencia","ανεξαρτησια","ανεξαρτησία"},
    {"battle","schlacht","bataille","battaglia","batalla","μαχη","μάχη"},
    {"war","krieg","guerre","guerra","πολεμος","πόλεμος"},
    {"victory","sieg","victoire","vittoria","victoria","νικη","νίκη"},
    {"peace","frieden","paix","pace","paz","ειρηνη","ειρήνη"},
    {"treaty","vertrag","traite","traité","trattato","tratado","συνθηκη","συνθήκη"},
    {"coronation","kronung","krönung","couronnement","incoronazione","coronacion","coronación","στεψη","στέψη"},
    {"wedding","hochzeit","mariage","matrimonio","boda","γαμος","γάμος"},
    {"birth","geburt","naissance","nascita","nacimiento","γεννηση","γέννηση"},
    {"death","tod","mort","morte","muerte","θανατος","θάνατος"},
    {"olympic","olympics","olympiade","olympisch","jeux olympiques","olimpiadi","olimpiada","ολυμπιακοι","ολυμπιακοί"},
    {"games","spiele","jeux","giochi","juegos","αγωνες","αγώνες"},
    {"unity","einheit","unite","unité","unita","unità","unidad","ενοτητα","ενότητα"},
    {"freedom","freiheit","liberte","liberté","liberta","libertà","libertad","ελευθερια","ελευθερία"},
    {"constitution","verfassung","constitution","costituzione","constitucion","constitución","συνταγμα","σύνταγμα"},
    {"republic","republik","republique","république","repubblica","republica","república","δημοκρατια","δημοκρατία"},
    {"kingdom","königreich","koenigreich","royaume","regno","reino","βασιλειο","βασίλειο"},
    {"empire","reich","empire","impero","imperio","αυτοκρατορια","αυτοκρατορία"},
    {"saint","heilige","heiliger","saint","sainte","santo","santa","αγιος","άγιος","αγια","αγία"},
    {"cathedral","dom","kathedrale","cathedrale","cathédrale","cattedrale","catedral","καθεδρικος","καθεδρικός"},
    {"castle","schloss","burg","chateau","château","castello","castillo","καστρο","κάστρο"},
    {"bridge","brucke","brücke","pont","ponte","puente","γεφυρα","γέφυρα"},
    {"ship","schiff","navire","bateau","nave","barco","πλοιο","πλοίο"},
    {"discovery","entdeckung","decouverte","découverte","scoperta","descubrimiento","ανακαλυψη","ανακάλυψη"},
    {"mint","münze","muenze","monnaie","zecca","casa de moneda","νομισματοκοπειο","νομισματοκοπείο"},
    {"mechanism","mechanismus","mecanisme","mécanisme","meccanismo","mecanismo","μηχανισμος","μηχανισμός"},
    {"queen","königin","koenigin","reine","regina","reina","βασιλισσα","βασίλισσα"},
    {"king","könig","koenig","roi","re","rey","βασιλιας","βασιλιάς"},
    {"president","präsident","praesident","president","presidente","προεδρος","πρόεδρος"},
    {"museum","museum","musee","musée","museo","museo","μουσειο","μουσείο"},
    {"temple","tempel","temple","tempio","templo","ναος","ναός"},
]

def _theme_expand(word):
    """Returns the full multilingual synonym group for `word` if it's in
    THEME_WORD_TRANSLATIONS, else just {word} unchanged (graceful — an
    unlisted word simply isn't translated, it still works exactly like
    literal matching did before this feature existed)."""
    w=norm(word)
    for group in THEME_WORD_TRANSLATIONS:
        if w in group:
            return group
    return {w}

def _theme_fuzzy_hit(word, title_tokens, threshold=0.82):
    """Conservative fuzzy/transliterated match for proper nouns not covered
    by any dictionary (place/person names spelled differently per
    language, e.g. Antikythera/Anticythère/Anticitera/Αντικύθηρα). Only
    applied to words of >=5 characters to avoid short-word false
    positives. A Greek-script word is transliterated to Latin first (reusing
    coin_identity_resolver's own transliteration table) before comparing —
    plain character-similarity between a Greek-script and a Latin-script
    word would otherwise score near zero even for the same underlying name,
    since they use entirely different alphabets."""
    w=norm(word)
    if len(w)<5:
        return w in title_tokens
    w_variants={w}
    translit=norm(transliterate_greek(word))
    if translit and translit!=w:
        w_variants.add(translit)
    for tok in title_tokens:
        if len(tok)<5:continue
        tok_variants={tok}
        tok_translit=norm(transliterate_greek(tok))
        if tok_translit and tok_translit!=tok:
            tok_variants.add(tok_translit)
        if any(SequenceMatcher(None,wv,tv).ratio()>=threshold for wv in w_variants for tv in tok_variants):
            return True
    return False

def theme_word_matches_title(theme_raw, title):
    """Soft, GENERIC multilingual match: True if the free-text theme
    (any word(s) the user typed, e.g. "mechanism", "anniversary",
    "Antikythera") is recognized in the listing title via (in order):
    literal substring, a known dictionary translation
    (THEME_WORD_TRANSLATIONS), or conservative fuzzy/transliteration
    matching for proper nouns. Works for ANY coin, not just ones with a
    seeded coin_issue_database.json alias list — see score_title() for
    where this is used as a soft ranking signal, and _theme_issue_gate()
    for the separate, curated hard gate used only for specifically seeded
    issues."""
    theme_raw=(theme_raw or "").strip()
    if not theme_raw:return True
    t_norm=norm(title)
    t_tokens=set(t_norm.split())
    words=[w for w in norm(theme_raw).split() if len(w)>=3]
    if not words:return True
    hits=0
    for w in words:
        group=_theme_expand(w)
        if any(g and g in t_norm for g in group):
            hits+=1;continue
        if _theme_fuzzy_hit(w,t_tokens):
            hits+=1
    return hits>=max(1,len(words))  # ALL leftover theme words must be recognized (in some language)

def theme_match_score(theme_raw, title):
    """0.0-1.0 SOFT ranking bonus (never a hard filter) — fraction of the
    theme's words recognized in the title via theme_word_matches_title's
    same dictionary+fuzzy logic. Used by score_title() so that among
    several otherwise-valid candidates, ones whose title actually matches
    the requested theme (in whatever language) rank higher — without ever
    excluding a candidate purely for not matching, which is exactly the
    over-strict behavior this whole feature was built to avoid."""
    theme_raw=(theme_raw or "").strip()
    if not theme_raw:return 0.0
    t_norm=norm(title)
    t_tokens=set(t_norm.split())
    words=[w for w in norm(theme_raw).split() if len(w)>=3]
    if not words:return 0.0
    hits=0
    for w in words:
        group=_theme_expand(w)
        if any(g and g in t_norm for g in group):
            hits+=1;continue
        if _theme_fuzzy_hit(w,t_tokens):
            hits+=1
    return hits/len(words)

# Minimal, bounded country-name -> issue-database country_code lookup, scoped
# ONLY to _theme_issue_gate() below. Deliberately NOT the general-purpose
# COUNTRY_CANON/canonical_country() machinery used everywhere else (country/
# denomination/year hard filters are UNCHANGED by this feature) — this is
# just enough to look up coin_issue_database.json's "issues" entries, which
# currently only use a handful of ISO-style country_code values. Extend this
# alongside any new issue record that needs it.
_ISSUE_COUNTRY_NAME_TO_CODE={"greece":"GR","ελλαδα":"GR","ελλαs":"GR","hellas":"GR","hellenic republic":"GR"}

def _theme_issue_gate(coin, title):
    """Multilingual ISSUE/THEME identity gate — separate from, and does not
    modify, variant_matches() (which stays literal/English and strict on
    purpose for controlled condition/type categories like proof/UNC/
    commemorative). This gate answers a different question: when a query's
    leftover descriptive text (e.g. "mechanism" from "Greece 10 euros 2022
    mechanism") clearly identifies ONE SPECIFIC known coin issue among
    possibly several sharing the same country+denomination+year (looked up
    in the shared coin_issue_database.json seed via the resolver), a
    listing must be recognized (via theme_word_matches_title's dictionary+
    fuzzy matching, not just literal substring) as matching THAT issue's
    own canonical_title/aliases — in any of several languages/spellings,
    not just English — to pass. This is what lets "Antikythera-Mechanismus"
    (German), "Mécanisme d'Anticythère" (French) or a bare "Antikythera"
    listing all correctly match a query written in English, while a
    genuinely different Greek 2022 10-euro issue is correctly excluded.

    Returns True (no gate — falls through to existing country/denom/year
    behavior) whenever:
      - there is no leftover theme text at all, or
      - the resolver/issue database is unavailable, or
      - no issue record for this country+denomination+year has an
        "aliases" list at all (most issues don't — this is opt-in per
        issue record, never a blanket new requirement), or
      - the theme text doesn't clearly pick out one specific such issue
        (ambiguous or no match against any candidate issue's own
        canonical_title/aliases) — in which case this gate stays out of
        the way rather than guessing.
    Only once a SPECIFIC issue has been identified from the theme text does
    this function start requiring a recognized match against one of that
    issue's own aliases in the listing title."""
    theme_raw=(coin.get("theme") or "").strip()
    if not theme_raw or not RESOLVER_AVAILABLE:
        return True
    try:
        issues=(get_resolver().issue_db or {}).get("issues") or []
    except Exception:
        return True
    if not issues:
        return True
    country_n=norm(coin.get("country") or "")
    code=next((c for name,c in _ISSUE_COUNTRY_NAME_TO_CODE.items() if name in country_n),None)
    if not code:
        return True
    m=re.search(r"(\d+(?:\.\d+)?)",str(coin.get("denom") or coin.get("denomination") or ""))
    denom_val=float(m.group(1)) if m else None
    try:
        year_val=int(coin.get("year") or 0) or None
    except Exception:
        year_val=None
    candidates=[iss for iss in issues if iss.get("country_code")==code
                and (denom_val is None or iss.get("denomination_value")==denom_val)
                and (year_val is None or iss.get("year")==year_val)
                and iss.get("aliases")]
    if not candidates:
        return True
    theme_n=norm(theme_raw)
    matched=None
    for iss in candidates:
        pool=[iss.get("canonical_title","")]+list(iss.get("aliases") or [])
        if any(p and (theme_n in norm(p) or norm(p) in theme_n) for p in pool):
            matched=iss;break
    if not matched:
        return True
    return theme_word_matches_title(theme_raw,title) or any(
        norm(al) in norm(title) for al in (matched.get("aliases") or []) if al)

def _country_explicit_in_raw(country, raw_query):
    """True only if the raw text the USER actually typed literally names
    this country (via any of its known aliases) — as opposed to a country
    the identity resolver merely INFERRED and auto-filled into the
    displayed "Country" field. Restores the "raw intent is the search
    contract" principle: denomination and year the user actually typed
    remain hard constraints, but a resolver-inferred country must not
    silently narrow/reject otherwise-correct results.

    Concrete regression this fixes: a user searching "5 drachma 1901"
    (no country mentioned at all) has the Country field auto-filled with
    "Greece" by the resolver — correct and helpful to show, but it must
    NOT then hard-reject a genuine "Kreta / Crete 5 Drachmai 1901"
    listing (the Cretan State, a distinct historical issuing authority)
    just because its title doesn't literally say "Greece". If the user
    instead explicitly typed "Greece 5 drachma 1901", country stays a
    real, enforced constraint as normal."""
    if not country or not raw_query:
        return False
    target=canonical_country(country)
    aliases=COUNTRY_CANON.get(target,[target])
    rq=norm(raw_query)
    return any(norm(al) in rq for al in aliases)

def passes_hard_filter(title, payload):
    coin = payload.get("coin") or {}
    a = norm(title)
    if not a: return False

    asset, conf = classify_asset(title)
    if asset in ("BANKNOTE", "OTHER"): return False
    if product_scope(title) != "SINGLE_COIN": return False

    year = str(coin.get("year") or "").strip()
    if year and not re.search(rf"(?<!\d){re.escape(year)}(?!\d)", a): return False

    denom = str(coin.get("denom") or coin.get("denomination") or "").strip()
    if denom and not denomination_matches(denom, title): return False

    country = str(coin.get("country") or "").strip()
    raw_query = str(payload.get("raw_query") or coin.get("raw") or "")

    if country and _country_explicit_in_raw(country, raw_query):
        numismatic_exceptions = ["drachma", "drachmai", "drachmas", "lepta", "george i", "georgios"]
        # The terminology exception is evidence for a Greek coin only when the
        # title does not explicitly name a conflicting historical issuer. In
        # particular, a user who typed Greece must not receive Cretan State /
        # Kreta / Crete issues merely because their title also says Drachmai.
        conflicting_greek_authority = (
            canonical_country(country) == "greece"
            and any(term in a for term in ("kreta", "crete", "cretan state", "cretan"))
        )
        has_exception = any(ex in a for ex in numismatic_exceptions) and not conflicting_greek_authority

        if not country_in_title(country, a) and not has_exception:
            return False

    variant = str(coin.get("variant") or "").strip()
    if variant and not variant_matches(variant, title): return False

    grade = str(coin.get("grade") or "").strip()
    if grade and grade_conflicts(grade, title): return False

    if not _theme_issue_gate(coin, title): return False

    if RESOLVER_AVAILABLE:
        try:
            if get_resolver()._negative_flags(title): return False
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
    # GENERIC multilingual theme ranking bonus (soft only — see
    # theme_match_score docstring). Any coin's free-text theme, in any
    # supported language/spelling, nudges matching listings higher without
    # ever excluding non-matching ones from being valid candidates.
    theme=coin.get("theme") or ""
    if theme:score+=.12*theme_match_score(theme,title)
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
        # Handles text such as "Tax included + 9,00 EUR shipping".
        rf"(?:tax included|tax)\s*\+?\s*{money_seg}\s*{ship_word}",
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
            requested_dest=(payload.get("ship_to") or "").strip()
            if requested_dest:
                dest_pattern,_,dest_display=destination_pattern_and_iso(requested_dest)
                # Only trust the compact search-row amount when the SAME row
                # explicitly names the requested destination (e.g.
                # "+ 5,50 EUR shipping (to Greece)").
                if re.search(dest_pattern,text,re.I):
                    shipping_status="known_target_search"
                else:
                    shipping_status="unverified"
            else:
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


def extract_mashops_physical_specs_from_html(html_text, source_url=None):
    """Extract explicit physical specs from an identity-validated MA-Shops item page.

    Conservative by design: only values printed on the page are returned.
    Handles current MA-Shops labels such as:
        Material: Silver
        Weight: 25.00 g
        Fineness: 900 ‰ (22.50 g fine)
        Diameter: 37.00 mm
    """
    if not html_text:
        return None
    soup=BeautifulSoup(html_text,"html.parser")
    text=smart_join(soup.stripped_strings)
    if not text:
        return None

    def first_number(patterns, lo=None, hi=None):
        for pat in patterns:
            m=re.search(pat,text,re.I)
            if not m:
                continue
            v=num(m.group(1))
            if v is None:
                continue
            if lo is not None and v < lo:
                continue
            if hi is not None and v > hi:
                continue
            return v
        return None

    weight=first_number([
        r"(?:weight|gewicht|poids|peso)\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:g|gram|grams|gramm)\b",
        r"([0-9]+(?:[.,][0-9]+)?)\s*(?:g|gram|grams|gramm)\s*(?:weight|gewicht|poids|peso)\b",
    ],0.1,1000)

    diameter=first_number([
        r"(?:diameter|durchmesser|diam[eè]tre|diametro|di[aá]metro)\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)\s*mm\b",
    ],1,200)

    metal_aliases={
        "silver":"Silver","silber":"Silver","argent":"Silver","argento":"Silver","plata":"Silver",
        "gold":"Gold","golden":"Gold","or":"Gold","oro":"Gold",
        "platinum":"Platinum","platin":"Platinum","platine":"Platinum","platino":"Platinum",
        "palladium":"Palladium",
    }

    metal=None
    mm=re.search(r"(?:material|metal|metall|mati[eè]re|materiale)\s*[:\-]?\s*([A-Za-zÀ-ÿ]+)",text,re.I)
    if mm:
        metal=metal_aliases.get(norm(mm.group(1)))
    if not metal:
        for raw,canon in metal_aliases.items():
            if re.search(rf"\b{re.escape(raw)}\b",text,re.I):
                metal=canon
                break

    fineness=None
    for pat in [
        r"(?:fineness|feinheit|titre|ley)\s*[:\-]?\s*(?:0[.,])?([0-9]{3,4})\s*(?:/\s*1000|‰)?",
        r"\b([0-9]{3,4})\s*(?:/\s*1000|‰)\b",
    ]:
        m=re.search(pat,text,re.I)
        if not m:
            continue
        try:
            v=float(m.group(1))
            if 100 <= v <= 1000:
                fineness=v
                break
        except Exception:
            pass

    printed_fine_g=first_number([
        r"\(\s*([0-9]+(?:[.,][0-9]+)?)\s*g\s*(?:fine|fein)\s*\)",
    ],0.01,1000)

    if weight is None and diameter is None and metal is None and fineness is None:
        return None

    if fineness is not None and float(fineness).is_integer():
        fineness=int(fineness)

    composition=None
    if metal and fineness is not None:
        if isinstance(fineness,int):
            composition=f"{metal} (.{fineness:03d})"
        else:
            composition=f"{metal} ({fineness/1000:.4f})"
    elif metal:
        composition=metal

    fine_g=(weight*float(fineness)/1000.0) if weight is not None and fineness is not None else printed_fine_g

    return {
        "composition":composition,
        "primary_metal":metal,
        "fineness_per_mille":fineness,
        "weight_g":weight,
        "diameter_mm":diameter,
        "fine_metal_g":fine_g,
        "printed_fine_metal_g":printed_fine_g,
        "spec_source":"MA-Shops validated item page",
        "data_provider":"MA-Shops",
        "source_url":source_url,
    }


_MA_SPEC_CACHE = {}

def _coin_identity_key(coin):
    return "|".join(str(coin.get(k) or "").strip().lower() for k in ("countryEN","country","denom","year","variant"))

def cache_mashops_spec(coin, spec):
    if spec and any(spec.get(k) is not None for k in ("weight_g","fineness_per_mille","primary_metal","diameter_mm")):
        _MA_SPEC_CACHE[_coin_identity_key(coin)] = dict(spec)

def cached_mashops_spec(coin):
    return _MA_SPEC_CACHE.get(_coin_identity_key(coin))

def mashops_spec_fallback(coin, raw_query=""):
    """Resolve missing physical specs from identity-validated MA-Shops listings."""
    cached=cached_mashops_spec(coin)
    if cached:
        return cached

    country=str(coin.get("countryEN") or coin.get("country") or "").strip()
    denom=str(coin.get("denom") or coin.get("denomination") or "").strip()
    year=str(coin.get("year") or "").strip()
    variant=str(coin.get("variant") or "").strip()
    base_query=(raw_query or " ".join(x for x in [country,denom,year,variant] if x)).strip()
    if not base_query:
        return None

    payload={
        "coin":{"country":country,"countryEN":country,"denom":denom,"year":year,"variant":variant},
        "raw_query":raw_query or base_query,
        "asset_type":"COIN",
    }

    queries=make_queries(payload)[:3] or [base_query]
    valid_by_url={}

    for query in queries:
        offers,_,err=fetch_search(query,payload)
        if err and not offers:
            continue
        for o in offers:
            mt=o.get("_match_text") or o.get("title","")
            asset=classify_asset(mt)[0]
            if asset in ("BANKNOTE","OTHER"):
                continue
            if product_scope(mt)!="SINGLE_COIN":
                continue
            if not passes_hard_filter(mt,payload):
                continue
            url=o.get("url")
            if url and url not in valid_by_url:
                valid_by_url[url]=o

    valid=sorted(
        valid_by_url.values(),
        key=lambda o:(o.get("price") is None,
                      o.get("price") if o.get("price") is not None else float("inf"),
                      -o.get("_score",0))
    )[:6]

    if not valid:
        print(f"[MA-Shops specs] no validated listing candidates for {base_query!r}",flush=True)
        return None

    def fetch_one(o):
        url=o.get("url")
        if not url:
            return None
        try:
            r=SESSION.get(url,timeout=12,allow_redirects=True)
            if not r.ok:
                return None
            specs=extract_mashops_physical_specs_from_html(r.text,r.url)
            if not specs:
                return None
            specs.update({
                "id":None,
                "title":o.get("title") or base_query,
                "issuer":"",
                "obverse_image":None,
                "reverse_image":None,
                "url":r.url,
                "match_class":"MA_SHOPS_VALIDATED_SPEC",
                "confidence":0.88,
                "spec_source_title":o.get("title") or "",
                "spec_source_dealer":o.get("dealer") or "",
            })
            return specs
        except Exception as e:
            print(f"[MA-Shops specs] item fetch failed: {type(e).__name__}: {e}",flush=True)
            return None

    with ThreadPoolExecutor(max_workers=min(3,len(valid))) as pool:
        futures=[pool.submit(fetch_one,o) for o in valid]
        for fut in as_completed(futures):
            try:
                specs=fut.result()
            except Exception:
                specs=None
            if specs:
                cache_mashops_spec(coin,specs)
                for other in futures:
                    if other is not fut:
                        other.cancel()
                print(
                    f"[MA-Shops specs] resolved {base_query!r}: "
                    f"composition={specs.get('composition')} weight_g={specs.get('weight_g')} "
                    f"fineness={specs.get('fineness_per_mille')} diameter_mm={specs.get('diameter_mm')}",
                    flush=True
                )
                return specs

    print(f"[MA-Shops specs] validated listings found but no explicit physical specs for {base_query!r}",flush=True)
    return None


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
    if not NUMISTA_API_KEY:
        return None, "NUMISTA_API_KEY is not configured on the server."

    year_match = re.search(r'\b(1\d{3}|20\d{2})\b', query)
    clean_query = query
    params = {"count": count, "lang": "en"}

    if year_match:
        detected_year = year_match.group(1)
        params["year"] = detected_year
        clean_query = query.replace(detected_year, "").strip()
    elif year:
        params["year"] = year

    params["q"] = clean_query
    if category:
        params["category"] = category

    url = f"{NUMISTA_BASE}/items"
    r, transport_err = _numista_get_with_backoff(url, params=params, timeout=15)
    if transport_err:
        return None, transport_err
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"

    try:
        data = r.json()
        items = data.get("items") or data.get("types")
        return items or [], None
    except Exception as e:
        return None, str(e)

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
            if re.search(rf"(?<!\d){re.escape(wanted_year)}(?!\d)",flatten_text(issue)):
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

@app.get("/api/metal-spot")
def metal_spot():
    """Return live XAU/XAG USD/oz plus USD->EUR.

    Primary provider: gold-api.com (free real-time endpoint, no API key).
    Fallback: goldprice.org. Never fabricates prices: if both providers fail,
    the endpoint returns HTTP 503 and the frontend keeps showing unavailable.
    """
    gold_usd_oz=None
    silver_usd_oz=None
    source=None
    errors=[]

    # Primary: gold-api.com. Fetch XAU and XAG independently so a malformed
    # response for one metal cannot silently become the other's price.
    try:
        print("[metal-spot][gold-api] REQUEST XAU + XAG",flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_gold=pool.submit(requests.get,"https://api.gold-api.com/price/XAU",timeout=10)
            fut_silver=pool.submit(requests.get,"https://api.gold-api.com/price/XAG",timeout=10)
            rg=fut_gold.result()
            rs=fut_silver.result()
        print(f"[metal-spot][gold-api] HTTP XAU={rg.status_code} XAG={rs.status_code}",flush=True)
        rg.raise_for_status(); rs.raise_for_status()
        dg=rg.json(); ds=rs.json()
        gold_usd_oz=float(dg["price"])
        silver_usd_oz=float(ds["price"])
        if gold_usd_oz<=0 or silver_usd_oz<=0:
            raise ValueError("non-positive metal price")
        source="gold-api.com"
        print("[metal-spot][gold-api] SUCCESS: XAU/XAG received",flush=True)
    except Exception as e:
        errors.append(f"gold-api: {type(e).__name__}: {e}")
        print(f"[metal-spot][gold-api] FAILED: {type(e).__name__}: {e}",flush=True)

    # Fallback retained for resilience. It is currently known to return 403
    # from Render, but keeping it costs nothing unless the primary fails and
    # allows automatic recovery if that provider later permits Render traffic.
    if gold_usd_oz is None or silver_usd_oz is None:
        try:
            print("[metal-spot][goldprice] REQUEST https://data-asg.goldprice.org/dbXRates/USD",flush=True)
            r=SESSION.get("https://data-asg.goldprice.org/dbXRates/USD",timeout=12)
            print(f"[metal-spot][goldprice] HTTP {r.status_code}",flush=True)
            r.raise_for_status()
            data=r.json()
            item=(data.get("items") or [None])[0]
            if not item or item.get("xauPrice") is None or item.get("xagPrice") is None:
                raise ValueError("unexpected goldprice response")
            gold_usd_oz=float(item["xauPrice"])
            silver_usd_oz=float(item["xagPrice"])
            if gold_usd_oz<=0 or silver_usd_oz<=0:
                raise ValueError("non-positive metal price")
            source="goldprice.org"
            print("[metal-spot][goldprice] SUCCESS: XAU/XAG received",flush=True)
        except Exception as e:
            errors.append(f"goldprice: {type(e).__name__}: {e}")
            print(f"[metal-spot][goldprice] FAILED: {type(e).__name__}: {e}",flush=True)

    if gold_usd_oz is None or silver_usd_oz is None:
        return jsonify({"error":"live metal price unavailable","providers_failed":errors}),503

    # fx_rates() is EUR-based (1 EUR = rates["USD"] USD), therefore USD->EUR
    # is its reciprocal. The previous code used rates["EUR"] (=1.0), which
    # incorrectly treated USD and EUR as equal in Metal Value calculations.
    rates=fx_rates()
    usd_per_eur=rates.get("USD")
    if not usd_per_eur:
        print("[metal-spot][fx] FAILED: USD exchange rate unavailable",flush=True)
        return jsonify({"error":"live FX rate unavailable"}),503
    usd_to_eur=1.0/float(usd_per_eur)

    return jsonify({
        "gold_usd_oz":gold_usd_oz,
        "silver_usd_oz":silver_usd_oz,
        "usd_to_eur":usd_to_eur,
        "source":source+" + CoinBids FX"
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
    """Return a cache key for the *exact response contract* of coin-search.

    Search-result caching must separate both market identity inputs and response
    projection inputs.  A normal UI request (top 2) must never satisfy a QA
    request (full evidence), and a QA response must never leak back into the UI.
    Likewise, shipping weight can change dealer shipping tiers and therefore the
    delivered-price ordering, so it is part of the market identity.
    """
    coin=payload.get("coin") or {}
    raw_query=str(payload.get("raw_query") or coin.get("raw") or "").strip().lower()
    parts=["raw="+raw_query]
    for k in ("country","countryEN","denom","denomination","year","variant","grade","theme","currency"):
        parts.append(f"coin.{k}="+str(coin.get(k) or "").strip().lower())
    weight=(payload.get("weight_g") if payload.get("weight_g") is not None else
            payload.get("coin_weight_g") if payload.get("coin_weight_g") is not None else
            payload.get("physical_weight_g"))
    parts += [
        "include_shipping="+str(bool(payload.get("include_shipping"))),
        "currency="+str(payload.get("currency") or "EUR").upper(),
        "ship_to="+str(payload.get("ship_to") or "").strip().lower(),
        "weight_g="+str(weight if weight is not None else ""),
        "limit="+str(int(payload.get("limit") or 2)),
        "sample_limit="+str(int(payload.get("sample_limit") or 10)),
        "qa_full_evidence="+str(bool(payload.get("qa_full_evidence"))),
    ]
    return "|".join(parts)

def _why_rejected(title, payload):
    """READ-ONLY diagnostic helper — reuses the exact same check functions
    passes_hard_filter() calls (never reimplements their logic), just
    stops at and names the FIRST one that fails, for troubleshooting only.
    Never called from the actual filtering decision (rejected[...] +=1 /
    valid.append), so it cannot change which listings pass or fail."""
    coin=payload.get("coin") or {}
    a=norm(title)
    if not a:return "empty_title"
    asset,_=classify_asset(title)
    if asset=="BANKNOTE":return "BANKNOTE"
    if product_scope(title)!="SINGLE_COIN":return "NOT_SINGLE_COIN"
    year=str(coin.get("year") or "").strip()
    if year and not re.search(rf"(?<!\d){re.escape(year)}(?!\d)",a):return f"YEAR_MISMATCH(want={year})"
    denom=str(coin.get("denom") or coin.get("denomination") or "").strip()
    if denom and not denomination_matches(denom,title):return f"DENOM_MISMATCH(want={denom})"
    country=str(coin.get("country") or "").strip()
    raw_query=str(payload.get("raw_query") or coin.get("raw") or "")
    if country and _country_explicit_in_raw(country,raw_query) and not country_in_title(country,a):
        return f"COUNTRY_MISMATCH(want={country})"
    variant=str(coin.get("variant") or "").strip()
    if variant and not variant_matches(variant,title):return f"VARIANT_MISMATCH(want={variant})"
    grade=str(coin.get("grade") or "").strip()
    if grade and grade_conflicts(grade,title):return f"GRADE_CONFLICT(want={grade})"
    if not _theme_issue_gate(coin,title):return f"THEME_ISSUE_MISMATCH(theme={coin.get('theme')!r})"
    if RESOLVER_AVAILABLE:
        try:
            neg=get_resolver()._negative_flags(title)
            if neg:return f"NEGATIVE_FLAG({neg})"
        except Exception:
            pass
    return "unknown"

@app.post("/api/coin-search")
def coin_search():
    payload=request.get_json(silent=True) or {}
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
    _reject_log_budget=[15]  # bounded: at most 15 per-title diagnostic lines per search, so this stays a lightweight troubleshooting aid rather than flooding the log on a 100+-candidate search.
    for o in offers:
        # MA-Shops splits identity fields across columns. Validate against the
        # complete row when available; other sources fall back to title.
        match_text=o.get("_match_text") or o.get("title","")
        asset,conf=classify_asset(match_text);o["asset_type"]=asset;o["asset_confidence"]=conf
        if asset in ("BANKNOTE","OTHER"):rejected["asset"]+=1;continue
        if product_scope(match_text)!="SINGLE_COIN":rejected["scope"]+=1;continue
        if not passes_hard_filter(match_text,payload):
            rejected["identity"]+=1
            if _reject_log_budget[0]>0:
                _reject_log_budget[0]-=1
                print(f"[coin-search] reject identity reason={_why_rejected(match_text,payload)} title={match_text!r}",flush=True)
            continue
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
    # Keep a bounded list of the cheapest DB misses for ONE final direct
    # item-page check.  This closes the "shipping unknown although MA-Shops
    # explicitly shows +X EUR shipping (to Greece)" gap without returning to
    # the old unbounded 30-40 detail-page crawl that exhausted Render workers.
    detail_fallback=[]
    for o in valid:
        finalize(o)
        if include_shipping:
            if o.get("shipping_status")=="known_target_search" and o.get("shipping") is not None:
                # Search row explicitly named the chosen destination.
                finalize(o)
            elif lookup_mashops_shipping(o,ship_to_country,item_weight_g):
                o["shipping_weight_g"]=item_weight_g
                o["shipping_weight_source"]=shipping_weight_source
                finalize(o)
            else:
                o["shipping"]=None
                o["shipping_status"]="unknown_db_no_match"
                o["total"]=None
                detail_fallback.append(o)

    # At most the two cheapest unresolved offers, in parallel.  Direct GET only
    # (use_geo_proxy=False): no paid proxy and no undefined fetch_geo_targeted
    # path. If the returned page names another destination, it remains unknown
    # for the user's destination rather than being guessed.
    if include_shipping and detail_fallback:
        candidates_for_detail=sorted(
            detail_fallback,
            key=lambda o:(o.get("price") is None,o.get("price") if o.get("price") is not None else float("inf"))
        )[:2]
        with ThreadPoolExecutor(max_workers=min(2,len(candidates_for_detail))) as _detail_pool:
            _future_map={_detail_pool.submit(enrich_offer_from_item_page,o,ship_to_country,False):o for o in candidates_for_detail}
            for _f in as_completed(_future_map):
                _o=_future_map[_f]
                try:
                    _f.result()
                except Exception as _e:
                    print(f"[shipping-detail] fallback failed: {type(_e).__name__}: {_e}",flush=True)
                # Only a target-confirmed figure is allowed into delivered total.
                if _o.get("shipping_status") in ("known_target","free") and _o.get("shipping") is not None:
                    _o["shipping_source"]="MA-Shops item page"
                    finalize(_o)
                else:
                    _o["shipping"]=None
                    _o["shipping_status"]="unknown"
                    _o["total"]=None

    valid.sort(
        key=lambda o:(
            o.get("total") is None,
            o.get("total") if o.get("total") is not None else float("inf"),
            o.get("price") if o.get("price") is not None else float("inf"),
            -o.get("_score",0)
        )
    )

    # Normal UI remains top-2; QA can explicitly request all validated
    # evidence so the global cheapest delivered offer is independently provable.
    _requested_limit=int(payload.get("limit") or 2)
    _max_public_limit=200 if payload.get("qa_full_evidence") else 2
    top=valid[:max(1,min(_requested_limit,_max_public_limit))]
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

    # These two counters have no producing logic anywhere in coin_search() —
    # nothing here currently calls enrich_offer_from_item_page() to populate
    # them. Left undefined, referencing them below throws NameError on every
    # single /api/coin-search request (HTTP 500, confirmed in production
    # Render logs 2026-08-23). Initializing to 0 is the safe minimal fix:
    # it reports "0 direct/geo detail-page checks" (accurate, since none
    # currently happen in this function) instead of crashing the endpoint.
    direct_checked=sum(1 for o in valid if o.get("detail_page_checked"))
    geo_checked=sum(1 for o in valid if o.get("shipping_geo_verified"))

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
        "shipping_note":f"shipping=null means unknown. shipping_status distinguishes confidence: known_target (item page confirmed your chosen destination, {ship_to_country}), known_target_search (MA-Shops search row explicitly named your chosen destination), known_target_db (matched dealer/destination tier in the local shipping database), known_other_destination (a specific other destination was found — see shipping_destination), known_unconfirmed_destination (a flat rate was found with no destination stated), free (confirmed free), unknown (nothing reliable found).",
        "ship_to_country":ship_to_country,
        "cheapest_known_delivered":public_offer(next((o for o in valid if o.get("total") is not None), valid[0])) if valid else None,
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
