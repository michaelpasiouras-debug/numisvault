from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests, re, html as ihtml, urllib.parse, time, os, math, threading
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

app = Flask(__name__)
ALLOWED_ORIGINS=os.environ.get("COINBIDS_CORS_ORIGINS","*").split(",")
CORS(app, resources={r"/api/*":{"origins":ALLOWED_ORIGINS}}, supports_credentials=False)


APP_DIR=os.path.dirname(os.path.abspath(__file__))

@app.get("/")
def frontend():
    return send_from_directory(APP_DIR,"index.html")

@app.get("/index.html")
def frontend_named():
    return send_from_directory(APP_DIR,"index.html")

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
}
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

# ---- Numista catalogue lookup: composition, weight, diameter, images. ----
# Official, documented, keyed REST API (not scraping) — https://en.numista.com/api/doc/index.php
# Auth: HTTP header "Numista-API-Key: <key>". Key is read ONLY from the
# NUMISTA_API_KEY environment variable — it must never be hardcoded here.
NUMISTA_API_KEY = os.environ.get("NUMISTA_API_KEY", "")
NUMISTA_BASE = "https://api.numista.com/v3"

def numista_search(query, category="coin", count=12, year=None):
    if not NUMISTA_API_KEY:return None,"NUMISTA_API_KEY is not configured on the server."
    try:
        params={"q":query,"count":count,"lang":"en"}
        # Official v3 search accepts year/date; category remains as a compatibility hint.
        if year:params["year"]=year
        if category:params["category"]=category
        r=requests.get(f"{NUMISTA_BASE}/types",params=params,headers={"Numista-API-Key":NUMISTA_API_KEY},timeout=15)
        if r.status_code!=200:return None,f"HTTP {r.status_code}: {r.text[:200]}"
        data=r.json();types=data.get("types")
        if types is None and isinstance(data.get("data"),dict):types=data["data"].get("types")
        return types or [],None
    except Exception as e:return None,str(e)

def numista_get_type(type_id):
    try:
        r=requests.get(f"{NUMISTA_BASE}/types/{type_id}",headers={"Numista-API-Key":NUMISTA_API_KEY},timeout=15)
        return (r.json(),None) if r.status_code==200 else (None,f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:return None,str(e)

def numista_get_issues(type_id):
    try:
        r=requests.get(f"{NUMISTA_BASE}/types/{type_id}/issues",headers={"Numista-API-Key":NUMISTA_API_KEY},timeout=15)
        if r.status_code!=200:return [],f"HTTP {r.status_code}"
        d=r.json()
        if isinstance(d,list):return d,None
        return d.get("issues") or (d.get("data",{}).get("issues") if isinstance(d.get("data"),dict) else []) or [],None
    except Exception as e:return [],str(e)

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
    for cand in results[:12]:
        tid=numista_pick(cand,"id")
        if tid is None:continue
        detail,derr=numista_get_type(tid)
        if derr or not detail:continue
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

@app.get("/health")
def health():
    return jsonify({"ok":True,"service":"CoinBids backend (MA-Shops + Numista)","numista_configured":bool(NUMISTA_API_KEY)})

_SEARCH_CACHE={}
_SEARCH_CACHE_LOCK=threading.Lock()
_SEARCH_CACHE_TTL=900  # 15 minutes — MA-Shops listings/prices don't meaningfully
                        # change minute-to-minute; this avoids re-scraping the
                        # exact same normalized query repeatedly under load.

def _search_cache_key(payload):
    coin=payload.get("coin") or {}
    parts=[str(coin.get(k) or "").strip().lower() for k in
           ("country","denom","denomination","year","variant","grade")]
    parts+=[str(payload.get("include_shipping")),str(payload.get("currency") or "EUR").upper(),
            str(payload.get("ship_to") or "")]
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
    queries=make_queries(payload);all_offers=[];errors=[];used=[]
    for q in queries:
        ma_offers,ma_url,ma_err=fetch_search(q,payload)
        if ma_url:used.append({"query":q,"source":"MA-Shops","url":ma_url})
        if ma_err:errors.append({"query":q,"source":"MA-Shops","error":ma_err})
        all_offers.extend(ma_offers)
        if len(all_offers)>=60:break
        time.sleep(.20)
    # Deduplicate by canonical URL.
    by={}
    for o in all_offers:
        key=o.get("url") or (o.get("title"),o.get("price"))
        if key not in by or o.get("_score",0)>by[key].get("_score",0):by[key]=o
    offers=list(by.values())
    rejected={"asset":0,"scope":0,"identity":0}
    valid=[]
    for o in offers:
        asset,conf=classify_asset(o.get("title",""));o["asset_type"]=asset;o["asset_confidence"]=conf
        if asset=="BANKNOTE":rejected["asset"]+=1;continue
        if product_scope(o.get("title",""))!="SINGLE_COIN":rejected["scope"]+=1;continue
        if not passes_hard_filter(o.get("title",""),payload):rejected["identity"]+=1;continue
        valid.append(o)
    valid.sort(key=lambda o:-o.get("_score",0))
    # Fetch item pages to verify shipping for candidates that could plausibly
    # end up in the final cheapest-2. Selecting only by relevance score was
    # the actual bug behind "wrong shipping shown": the final ranking below
    # sorts by TOTAL PRICE, not relevance score, so a low-price/lower-score
    # listing could win a top-2 slot while still carrying its unverified,
    # possibly-wrong Stage-1 shipping guess because it was never fetched.
    # Union: top by relevance score (identity-quality coverage) + top by
    # item price alone (price is reliable pre-enrichment) — capped so request
    # volume stays bounded.
    by_price=sorted(valid,key=lambda o:o.get("price") if o.get("price") is not None else float("inf"))
    to_enrich={id(o):o for o in valid[:8]}
    for o in by_price[:12]:to_enrich[id(o)]=o
    ship_to_country=payload.get("ship_to") or "Greece"
    print(f"[coin-search] valid={len(valid)} to_enrich={len(to_enrich)} ship_to={ship_to_country} "
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
    top=valid[:max(1,min(int(payload.get("limit") or 2),2))]

    # PHASE 2 (only spends ScrapingBee credits here, if configured): for the
    # 1-2 offers that will ACTUALLY be shown to the person, try to upgrade
    # shipping to a verified figure for their chosen destination — instead
    # of burning a geo-targeted request on every one of the ~12 pre-ranking
    # candidates, only the offers that made the final cut get one.
    if SCRAPINGBEE_API_KEY:
        for o in top:
            if o.get("shipping_status")!="known_target":
                enrich_offer_from_item_page(o,ship_to_country,use_geo_proxy=True)
                finalize(o)
        top.sort(key=lambda o:(o.get("total") is None,o.get("total") if o.get("total") is not None else float("inf"),-o.get("_score",0)))

    for o in top:
        o.pop("_score",None);o.pop("dealer_source",None)
    result={
        "source":"MA-Shops","queries":queries,"used_search_pages":used,"offers":top,
        "best_offer":top[0] if top else None,"count":len(top),"raw_count":len(all_offers),
        "valid_count":len(valid),"rejected":rejected,"sources_ok":["MA-Shops"] if used else [],
        "sources_failed":["MA-Shops"] if errors and not used else [],"errors":errors[-6:],
        "note":"Only the two cheapest validated matching COIN listings are returned. Unknown shipping is never treated as free.",
        "shipping_note":f"shipping=null means unknown. shipping_status distinguishes confidence: known_target (confirmed for your chosen destination, {ship_to_country}), known_other_destination (a specific other destination was found — see shipping_destination), known_unconfirmed_destination (a flat rate was found with no destination stated), free (confirmed free), unknown (nothing reliable found).",
        "ship_to_country":ship_to_country,
        "cache":"miss"
    }
    # Only cache genuinely successful lookups — never cache a transient failure
    # so a temporary MA-Shops block doesn't get "frozen" as the answer for 15 minutes.
    if top:
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
