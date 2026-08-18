from flask import Flask, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
import requests, re, html as ihtml, urllib.parse, time, os, math, threading, json
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
    return send_from_directory(os.path.join(APP_DIR,"assets"),"favicon.ico",mimetype="image/vnd.microsoft.icon")

@app.get("/assets/<path:filename>")
def public_assets(filename):
    # Favicons and the Open Graph share image referenced by the public pages
    # above — all real files derived from the existing CoinBids logo, not
    # newly designed artwork.
    return send_from_directory(os.path.join(APP_DIR,"assets"),filename)

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

# --- Optional geo-targeted fetching (ScrapingBee) -----------------------
# When SCRAPINGBEE_API_KEY is set, item-detail-page fetches for shipping
# verification are routed through a proxy in the buyer's chosen country, so
# MA-Shops shows THAT country's shipping instead of whatever it defaults to
# for a plain request (usually the seller's own country). Without a key
# configured, everything falls back to the original direct-request behavior
# — this is additive, never a hard dependency, and never silently pretends
# geo-targeting happened when it didn't.
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY","")
SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"

def fetch_geo_targeted(url, iso_code):
    """Best-effort geo-targeted fetch. Tries ScrapingBee (classic proxy,
    no JS rendering — MA-Shops item pages are plain server-rendered PHP, so
    JS rendering credits would be wasted) when a key is configured and a
    valid ISO country code is available. On ANY failure — missing key,
    quota exceeded, network error, non-2xx response — falls back to a
    plain direct request, exactly as before this feature existed.
    Every outcome is logged so it's visible in Render's Logs tab — silently
    swallowing a ScrapingBee failure made "it didn't work" indistinguishable
    from "it wasn't even tried", which made this impossible to debug."""
    if not SCRAPINGBEE_API_KEY:
        print(f"[geo-fetch] SCRAPINGBEE_API_KEY not set — skipping geo-targeting for {url}")
    elif not iso_code:
        print(f"[geo-fetch] no ISO country code resolved — skipping geo-targeting for {url}")
    else:
        try:
            resp = SESSION.get(SCRAPINGBEE_ENDPOINT, params={
                "api_key": SCRAPINGBEE_API_KEY,
                "url": url,
                "country_code": iso_code.lower(),
                "render_js": "false",
            }, timeout=25)
            if resp.ok and resp.text:
                print(f"[geo-fetch] ScrapingBee OK ({iso_code}) for {url} — {len(resp.text)} chars, "
                      f"credits used this call: {resp.headers.get('Spb-cost','?')}")
                return resp, True
            else:
                print(f"[geo-fetch] ScrapingBee returned HTTP {resp.status_code} for {url}: "
                      f"{resp.text[:300]!r}")
        except Exception as e:
            print(f"[geo-fetch] ScrapingBee call raised {type(e).__name__}: {e} for {url}")
    try:
        return SESSION.get(url, timeout=12, allow_redirects=True), False
    except Exception:
        return None, False

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
    "greece":["greece","greek","hellas","ellada","ελλαδα"],
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
    qs.extend(resolver_queries)
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
        text = price_cell_text if price_cell_text else smart_join(container.stripped_strings)
        if looks_unavailable(text) or looks_unavailable(title): continue
        result=extract_prices_with_shipping(text,title)
        if not result: continue
        price, shipping, currency, shipping_status = result
        if price<=0 or price>200000: continue
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
        offers.append({"title":title,"url":g["url"],"price":price,"shipping":shipping,"shipping_status":shipping_status,
                       "currency":currency,"dealer":"","grade":"","availability":"",
                       "asset_type":classify_asset(title)[0],"product_scope":product_scope(title),
                       "_score":score_title(title,payload)})
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

def fetch_search(query, payload):
    """Inspect BOTH the normal MA-Shops search page and the explicit
    cheapest-first page (sortby=preis_eur). A successful normal-page fetch
    must NOT short-circuit the cheapest-first one — the old behavior here
    returned as soon as the first URL produced offers, which meant a
    low-priced listing that only showed up on the cheapest-first ordering
    could be missed entirely. Results from both are merged; deduplication
    happens later in coin_search()."""
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

# Server-side cache for Numista type details/issues — shared across ALL
# CoinBids users and requests (unlike the frontend's per-browser cache),
# since composition/weight/diameter/issue-year data for a catalogued type
# doesn't change on any timescale that matters here. In-memory only (not
# persisted across a process restart on Render's free tier — an honest
# limitation, not a durable cross-deploy cache), but still meaningfully cuts
# repeat Numista API calls within a running process's lifetime, including
# across different users looking up the same popular coin.
_NUMISTA_DETAIL_CACHE={}   # type_id -> (data, cached_at)
_NUMISTA_ISSUES_CACHE={}   # type_id -> (issues, cached_at)
_NUMISTA_CACHE_LOCK=threading.Lock()
_NUMISTA_CACHE_TTL_SECONDS=24*60*60  # 24 hours
_NUMISTA_CACHE_MAX_ENTRIES=2000

def _numista_cache_get(cache,key):
    with _NUMISTA_CACHE_LOCK:
        hit=cache.get(key)
        if hit and time.time()-hit[1]<_NUMISTA_CACHE_TTL_SECONDS:
            return hit[0]
        return None

def _numista_cache_set(cache,key,value):
    with _NUMISTA_CACHE_LOCK:
        cache[key]=(value,time.time())
        if len(cache)>_NUMISTA_CACHE_MAX_ENTRIES:
            # Bounded growth: drop the oldest half rather than let this grow
            # forever on a long-running process.
            oldest=sorted(cache.items(),key=lambda kv:kv[1][1])[:len(cache)//2]
            for k,_ in oldest:cache.pop(k,None)

# ---- Request deduplication ("single-flight") ----
# If several Flask requests concurrently need the SAME not-yet-cached
# Numista type/issues (e.g. two people looking up the same popular coin at
# the same moment), only the first one actually calls Numista — the rest
# wait for that result instead of each firing their own duplicate HTTP
# request. This is orthogonal to the TTL cache above: the cache prevents
# repeat calls across TIME, this prevents repeat calls across CONCURRENT
# requests for data that isn't cached yet. No race on the shared cache
# itself (that's already lock-protected by _numista_cache_get/_set above);
# this only coordinates *waiting*, so a stuck/slow leader can never corrupt
# the cache — it can only make followers fall back to fetching themselves
# after a bounded wait.
_NUMISTA_INFLIGHT={}  # key -> threading.Event
_NUMISTA_INFLIGHT_LOCK=threading.Lock()
_NUMISTA_INFLIGHT_WAIT_SECONDS=20

def _numista_single_flight(cache,key,fetch_fn):
    cached=_numista_cache_get(cache,key)
    if cached is not None:return cached,None
    is_leader=False
    with _NUMISTA_INFLIGHT_LOCK:
        cached=_numista_cache_get(cache,key)  # re-check under lock (double-checked locking)
        if cached is not None:return cached,None
        event=_NUMISTA_INFLIGHT.get(key)
        if event is None:
            event=threading.Event()
            _NUMISTA_INFLIGHT[key]=event
            is_leader=True
    if not is_leader:
        event.wait(timeout=_NUMISTA_INFLIGHT_WAIT_SECONDS)
        cached=_numista_cache_get(cache,key)
        if cached is not None:return cached,None
        # The leader's fetch failed, or didn't finish within the wait
        # window — fall through and fetch ourselves rather than permanently
        # fail every follower because of one leader's bad luck.
    data,err=fetch_fn()  # fetch_fn is responsible for writing to `cache` itself on success
    if is_leader:
        with _NUMISTA_INFLIGHT_LOCK:
            _NUMISTA_INFLIGHT.pop(key,None)
        event.set()
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
        _numista_cache_set(_NUMISTA_DETAIL_CACHE,type_id,data)
        return data,None
    return _numista_single_flight(_NUMISTA_DETAIL_CACHE,type_id,_fetch)

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
        _numista_cache_set(_NUMISTA_ISSUES_CACHE,type_id,issues)
        return issues,None
    return _numista_single_flight(_NUMISTA_ISSUES_CACHE,type_id,_fetch)

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
    return jsonify({"match":{
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

@app.get("/health")
def health():
    return jsonify({"ok":True,"service":"CoinBids backend (MA-Shops + Numista)","numista_configured":bool(NUMISTA_API_KEY),"resolver_available":RESOLVER_AVAILABLE,"auction_intelligence_v3_available":AUCTION_INTELLIGENCE_V3_AVAILABLE,"corrections_available":CORRECTIONS_AVAILABLE,"corrections_write_enabled":bool(CORRECTIONS_SECRET)})

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
    for q in queries:
        ma_offers,ma_url,ma_err=fetch_search(q,payload)
        if ma_url:used.append({"query":q,"source":"MA-Shops","url":ma_url})
        if ma_err:errors.append({"query":q,"source":"MA-Shops","error":ma_err})
        all_offers.extend(ma_offers)
        if len(all_offers)>=120:break
        time.sleep(.20)
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
        asset,conf=classify_asset(o.get("title",""));o["asset_type"]=asset;o["asset_confidence"]=conf
        if asset=="BANKNOTE":rejected["asset"]+=1;continue
        if product_scope(o.get("title",""))!="SINGLE_COIN":rejected["scope"]+=1;continue
        if not passes_hard_filter(o.get("title",""),payload):rejected["identity"]+=1;continue
        if target_identity:
            try:
                ms=get_resolver().listing_match_score(target_identity,o.get("title",""))
                o["identity_match_score"]=ms.get("score")
                o["identity_match_reasons"]=ms.get("reasons")
            except Exception:
                pass
        valid.append(o)
    valid_count=len(valid)
    valid.sort(key=lambda o:-o.get("_score",0))
    # Fetch item pages to verify shipping for candidates that could plausibly
    # end up in the final cheapest-2. Selecting only by relevance score was
    # the actual bug behind "wrong shipping shown": the final ranking below
    # sorts by TOTAL PRICE, not relevance score, so a low-price/lower-score
    # listing could win a top-2 slot while still carrying its unverified,
    # possibly-wrong Stage-1 shipping guess because it was never fetched.
    # Union: top by relevance score (identity-quality coverage) + top by
    # item price alone (price is reliable pre-enrichment) — capped by
    # scan_limit (default wider than before — was a fixed ~20, now
    # configurable up to 40) so request volume stays bounded but the search
    # is transparent about how many candidates it actually inspected.
    scan_limit=max(10,min(int(payload.get("scan_limit") or 30),40))
    by_price=sorted(valid,key=lambda o:o.get("price") if o.get("price") is not None else float("inf"))
    score_share=max(6,scan_limit//3)
    to_enrich={id(o):o for o in valid[:score_share]}
    for o in by_price[:scan_limit]:to_enrich[id(o)]=o
    ship_to_country=payload.get("ship_to") or "Greece"
    print(f"[coin-search] valid={len(valid)} to_enrich={len(to_enrich)} scan_limit={scan_limit} ship_to={ship_to_country} "
          f"SCRAPINGBEE_API_KEY_set={bool(SCRAPINGBEE_API_KEY)}")
    # PHASE 1 (free): direct-fetch enrichment for real price + honestly-labeled
    # shipping across all plausible candidates. No ScrapingBee here — this is
    # exactly what ran before ScrapingBee existed, at zero extra cost.
    for o in to_enrich.values():enrich_offer_from_item_page(o,ship_to_country,use_geo_proxy=False)

    def finalize(o):
        include_shipping=bool(payload.get("include_shipping"))
        target_currency=(payload.get("currency") or "EUR").upper()
        if o.get("shipping_status")=="unverified":
            o["shipping"]=None
        item_eur=to_eur(o.get("price"),o.get("currency"))
        ship_eur=to_eur(o.get("shipping"),o.get("currency")) if o.get("shipping") is not None else None
        if target_currency=="EUR":
            o["price"]=round(item_eur,2) if item_eur is not None else o.get("price")
            if ship_eur is not None:o["shipping"]=round(ship_eur,2)
            o["currency"]="EUR"
        elif target_currency in ("USD","GBP","CHF") and o.get("currency")!=target_currency:
            rates=fx_rates();rate=rates.get(target_currency)
            if item_eur is not None and rate:
                o["price"]=round(item_eur*rate,2)
                if ship_eur is not None:o["shipping"]=round(ship_eur*rate,2)
                o["currency"]=target_currency
        if include_shipping:
            o["total"]=round(float(o["price"])+float(o["shipping"]),2) if o.get("shipping") is not None else None
        else:o["total"]=round(float(o["price"]),2)
        if not o.get("dealer"):o["dealer"]="MA-Shops"

    for o in valid: finalize(o)
    # Confirmed totals first; unknown shipping cannot masquerade as free.
    valid.sort(key=lambda o:(o.get("total") is None,o.get("total") if o.get("total") is not None else float("inf"),-o.get("_score",0)))

    include_shipping=bool(payload.get("include_shipping"))
    # PHASE 1.5 (only spends ScrapingBee credits here, if configured, and only
    # when shipping is actually part of the ranking): BEFORE the final top-2
    # cut, geo-verify unknown-shipping candidates that still have a realistic
    # chance of entering the top two. An item's PRICE ALONE is a lower bound
    # on its eventual all-in total (shipping is never negative), so any
    # unknown-shipping candidate priced at or below the current second-best
    # CONFIRMED total deserves verification before being excluded — otherwise
    # a genuinely cheaper listing can be dropped from the results purely
    # because Phase 1 never checked its real shipping cost. This fixes a
    # selection bias where the displayed "two cheapest" were only the two
    # cheapest AMONG candidates that happened to get checked, not necessarily
    # the two cheapest all-in across the full scanned set.
    if include_shipping and SCRAPINGBEE_API_KEY:
        confirmed=[o for o in valid if o.get("total") is not None]
        unknown=[o for o in valid if o.get("total") is None]
        unknown_by_price=sorted(unknown,key=lambda o:o.get("price") if o.get("price") is not None else float("inf"))
        margin=float(payload.get("shipping_check_margin") or 0)
        contenders=[]
        if len(confirmed)>=2:
            second_best_total=confirmed[1]["total"]
            for o in unknown_by_price:
                ip=o.get("price")
                if ip is None:continue
                # Item price is a hard lower bound on total (shipping >= 0),
                # so if it already exceeds the current #2 confirmed total,
                # no possible shipping figure can move it into the top two.
                if ip<=second_best_total+margin:
                    contenders.append(o)
                if len(contenders)>=8:break  # bound ScrapingBee credit usage
        else:
            # Fewer than two confirmed totals exist yet — verify the cheapest
            # unknown-shipping candidates to try to establish real top results.
            contenders=unknown_by_price[:8]
        for o in contenders:
            if o.get("shipping_status")!="known_target":
                enrich_offer_from_item_page(o,ship_to_country,use_geo_proxy=True)
                finalize(o)
        if contenders:
            valid.sort(key=lambda o:(o.get("total") is None,o.get("total") if o.get("total") is not None else float("inf"),-o.get("_score",0)))

    top=valid[:max(1,min(int(payload.get("limit") or 2),2))]

    # PHASE 2 (safety net, only spends ScrapingBee credits here if configured):
    # for the 1-2 offers that will ACTUALLY be shown, make sure shipping is
    # geo-verified for their chosen destination. Most top-2 members will
    # already be "known_target" from Phase 1.5 above (skipped here via that
    # check) — this remaining pass only catches edge cases such as very few
    # confirmed totals existing overall.
    if SCRAPINGBEE_API_KEY:
        for o in top:
            if o.get("shipping_status")!="known_target":
                enrich_offer_from_item_page(o,ship_to_country,use_geo_proxy=True)
                finalize(o)
        top.sort(key=lambda o:(o.get("total") is None,o.get("total") if o.get("total") is not None else float("inf"),-o.get("_score",0)))

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
        d=dict(o);d.pop("_score",None);d.pop("dealer_source",None);return d
    top_public=[public_offer(o) for o in top]
    sample_public=[public_offer(o) for o in market_sample]

    diagnostics={
        "raw_candidates_found":raw_count,
        "unique_candidates":unique_count,
        "validated_matching_coins":valid_count,
        "detail_pages_checked":sum(1 for o in valid if o.get("detail_page_checked")),
        "known_comparable_totals":sum(1 for o in valid if o.get("total") is not None),
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
