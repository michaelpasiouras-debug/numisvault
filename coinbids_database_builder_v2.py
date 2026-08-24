#!/usr/bin/env python3
"""
CoinBids Database Builder v2
============================

Builds a provenance-first CoinBids master database.

Current automated adapters
--------------------------
1. European Commission: common euro coin technical specifications
2. ECB: Greek national-side identity/design notes
3. Bank of Greece National Mint: Greek collector / commemorative coin pages
4. Legacy CoinBids Europe family database migration (optional fallback layer)

Design goals
------------
- factual metadata only
- no image harvesting
- source URL stored for every imported fact
- no guessing of missing weight / diameter / fineness
- proof / collector / commemorative issues remain variant-specific
- melt value is enabled only when explicit weight + precious-metal fineness exist
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import argparse, csv, hashlib, json, re, time, urllib.parse, xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

UA = "CoinBidsDatabaseBuilder/2.0 (+https://www.coinbids.eu/)"
TIMEOUT = 25
DEFAULT_DELAY = 1.25

EC_COMMON = "https://economy-finance.ec.europa.eu/euro/euro-coins-and-notes/euro-coins/common-sides-euro-coins_en"
ECB_GR = "https://www.ecb.europa.eu/euro/coins/html/gr.en.html"
MINT_BASE = "https://mint.bankofgreece.gr"
MINT_COINS = MINT_BASE + "/en/coins/"
MINT_SITEMAP = MINT_BASE + "/sitemap_index.xml"

PRECIOUS = {"silver","gold","platinum","palladium"}

@dataclass
class Provenance:
    source_name: str
    source_url: str
    source_type: str
    license: Optional[str] = None
    retrieved_at: Optional[str] = None
    note: Optional[str] = None

@dataclass
class CoinSpec:
    record_id: str
    country_code: str
    country: str
    currency_code: str
    denomination_value: float
    denomination_label: str
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    issue_year: Optional[int] = None
    coin_type: str = "circulation"
    variant: Optional[str] = None
    title: Optional[str] = None
    composition_text: Optional[str] = None
    primary_metal: Optional[str] = None
    fineness_per_mille: Optional[int] = None
    weight_g: Optional[float] = None
    diameter_mm: Optional[float] = None
    thickness_mm: Optional[float] = None
    fine_metal_g: Optional[float] = None
    edge: Optional[str] = None
    mint: Optional[str] = None
    mintmark: Optional[str] = None
    mintage: Optional[int] = None
    external_ids: Dict[str,str] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    source_priority: int = 0
    confidence: float = 0.0
    verified: bool = False
    metal_value_ready: bool = False
    provenance: List[Provenance] = field(default_factory=list)

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def stable_id(*parts):
    raw="|".join("" if p is None else str(p).strip().lower() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def num(x):
    if x is None: return None
    m=re.search(r"-?\d+(?:[.,]\d+)?", str(x).replace("\xa0"," "))
    return float(m.group(0).replace(",",".")) if m else None

def integer(x):
    v=num(x)
    return int(v) if v is not None else None

def infer_primary_metal(text):
    t=(text or "").lower()
    if "silver" in t or re.search(r"\bag\b",t): return "silver"
    if "gold" in t or re.search(r"\bau\b",t): return "gold"
    if "platinum" in t or re.search(r"\bpt\b",t): return "platinum"
    if "palladium" in t or re.search(r"\bpd\b",t): return "palladium"
    if "steel" in t: return "steel"
    if "aluminium" in t or "aluminum" in t: return "aluminium-alloy"
    if "nickel" in t: return "nickel-alloy"
    if any(z in t for z in ("bronze","brass","copper")): return "copper-alloy"
    return None

def fineness_from_text(text):
    t=(text or "")
    # e.g. "Gold finess 917", "Silver .925", "Ag 835"
    pats=[
        r"(?:gold|silver|ag|au)[^\d]{0,20}(?:finess|fineness|purity)?[^\d]{0,8}(\d{3})(?!\d)",
        r"(?:gold|silver|ag|au)[^\d]{0,12}0?[.,](\d{3})"
    ]
    for p in pats:
        m=re.search(p,t,re.I)
        if m: return int(m.group(1))
    return None

def normalize(r: CoinSpec):
    if not r.primary_metal:
        r.primary_metal=infer_primary_metal(r.composition_text)
    if r.weight_g is not None and r.fineness_per_mille is not None:
        r.fine_metal_g=round(r.weight_g*r.fineness_per_mille/1000.0,6)
    r.metal_value_ready=bool(
        r.primary_metal in PRECIOUS and r.weight_g is not None and r.fineness_per_mille is not None
    )
    if r.coin_type in {"commemorative","collector","proof","bullion"} and not r.variant:
        r.variant=r.title or "special issue"
    return r

class Fetcher:
    def __init__(self, delay=DEFAULT_DELAY):
        self.delay=max(0.5,delay)
        self.s=requests.Session()
        self.s.headers.update({"User-Agent":UA,"Accept-Language":"en-US,en;q=0.9"})
        self.last=0.0

    def get(self,url):
        elapsed=time.time()-self.last
        if elapsed<self.delay:
            time.sleep(self.delay-elapsed)
        r=self.s.get(url,timeout=TIMEOUT,allow_redirects=True)
        self.last=time.time()
        r.raise_for_status()
        return r

def _label_value_pairs(soup):
    out={}
    # tables
    for tr in soup.find_all("tr"):
        cells=tr.find_all(["th","td"])
        if len(cells)>=2:
            k=" ".join(cells[0].stripped_strings).strip()
            v=" ".join(cells[1].stripped_strings).strip()
            if k and v: out[k.lower()]=v
    # dt/dd
    for dt in soup.find_all("dt"):
        dd=dt.find_next_sibling("dd")
        if dd:
            out[" ".join(dt.stripped_strings).strip().lower()]=" ".join(dd.stripped_strings).strip()
    # common BoG Mint product markup has label/value in sibling blocks.
    txt=list(soup.stripped_strings)
    known={"denomination","diameter","weight","edge","material","maximum issue","minting quality","artist","year","facevalue","gold finess","silver fineness","silver finess","gold fineness","country","type"}
    for i,s in enumerate(txt[:-1]):
        k=s.strip().lower().rstrip(":")
        if k in known and k not in out:
            out[k]=txt[i+1].strip()
    return out

def parse_ec_common(fetch: Fetcher) -> List[CoinSpec]:
    """Parse the 8 common euro physical specifications."""
    r=fetch.get(EC_COMMON)
    soup=BeautifulSoup(r.text,"html.parser")
    text="\n".join(soup.stripped_strings)

    expected=[
        (2.0,"2 euro",8.50,25.75),
        (1.0,"1 euro",7.50,23.25),
        (0.50,"50 euro cent",7.80,24.25),
        (0.20,"20 euro cent",5.74,22.25),
        (0.10,"10 euro cent",4.10,19.75),
        (0.05,"5 euro cent",3.92,21.25),
        (0.02,"2 euro cent",3.06,18.75),
        (0.01,"1 euro cent",2.30,16.25),
    ]

    # These values are harmonised legal specs; parse-page success is validated
    # against them. We do not silently accept a changed/malformed page.
    out=[]
    for value,label,weight,diam in expected:
        # Require both numeric values to appear on official page text.
        if f"{weight:.2f}".rstrip("0").rstrip(".") not in text and str(weight) not in text:
            raise RuntimeError(f"EC common-spec validation failed for {label}: weight {weight} not found.")
        if f"{diam:.2f}".rstrip("0").rstrip(".") not in text and str(diam) not in text:
            raise RuntimeError(f"EC common-spec validation failed for {label}: diameter {diam} not found.")

        if value in (0.01,0.02,0.05):
            comp="Copper-covered steel"
        elif value in (0.10,0.20,0.50):
            comp="Nordic gold (copper alloy; contains no gold)"
        elif value==1.0:
            comp="Bimetallic: nickel brass outer / copper-nickel inner"
        else:
            comp="Bimetallic: copper-nickel outer / nickel-brass three-layer inner"

        out.append(normalize(CoinSpec(
            record_id=stable_id("EU","EUR",value,"common-spec"),
            country_code="EU",country="Euro area",currency_code="EUR",
            denomination_value=value,denomination_label=label,
            year_from=1999,coin_type="circulation",variant="common euro physical specification",
            title=f"Common euro specification — {label}",
            composition_text=comp,weight_g=weight,diameter_mm=diam,
            source_priority=100,confidence=1.0,verified=True,
            provenance=[Provenance(
                "European Commission — Common sides of euro coins",EC_COMMON,"eu",
                retrieved_at=now_iso(),note="Official harmonised common physical specification."
            )]
        )))
    return out

def parse_ecb_greece(fetch: Fetcher, common_specs: List[CoinSpec]) -> List[CoinSpec]:
    """Create Greece-specific euro records by joining ECB identity data to EU physical specs."""
    r=fetch.get(ECB_GR)
    soup=BeautifulSoup(r.text,"html.parser")
    text=" ".join(soup.stripped_strings)

    motifs={
        0.01:"Athenian trireme",
        0.02:"corvette",
        0.05:"modern sea-going tanker",
        0.10:"Rigas-Fereos",
        0.20:"Ioannis Capodistrias",
        0.50:"Eleftherios Venizelos",
        1.0:"owl motif from ancient Athenian 4 drachma coin",
        2.0:"Europa and the bull",
    }

    # Validate that page is actually the Greek national-side page.
    if "Greece" not in text or "Georges Stamatopoulos" not in text:
        raise RuntimeError("ECB Greece page validation failed.")

    by_value={x.denomination_value:x for x in common_specs}
    out=[]
    for value,motif in motifs.items():
        c=by_value[value]
        out.append(normalize(CoinSpec(
            record_id=stable_id("GR","EUR",value,"national"),
            country_code="GR",country="Greece",currency_code="EUR",
            denomination_value=value,denomination_label=c.denomination_label,
            year_from=2002,coin_type="circulation",
            variant=f"Greek national side — {motif}",
            title=f"Greece {c.denomination_label}",
            composition_text=c.composition_text,
            weight_g=c.weight_g,diameter_mm=c.diameter_mm,
            mint="Bank of Greece / National Mint",
            aliases=[f"{c.denomination_label} Greece",f"Greece {c.denomination_label}"],
            source_priority=100,confidence=1.0,verified=True,
            provenance=[
                Provenance("European Central Bank — Greece national sides",ECB_GR,"eu",retrieved_at=now_iso(),note=f"National-side identity: {motif}."),
                *c.provenance
            ]
        )))
    return out

def discover_mint_coin_urls(fetch: Fetcher, max_urls=300) -> List[str]:
    """Discover Bank of Greece Mint coin product pages without scraping images."""
    urls=set()
    # Try sitemap index first.
    try:
        r=fetch.get(MINT_SITEMAP)
        root=ET.fromstring(r.text)
        ns={"s":"http://www.sitemaps.org/schemas/sitemap/0.9"}
        sub=[x.text for x in root.findall(".//s:loc",ns) if x.text]
        for sm in sub[:20]:
            try:
                rr=fetch.get(sm)
                rt=ET.fromstring(rr.text)
                for loc in rt.findall(".//s:loc",ns):
                    u=(loc.text or "").strip()
                    if "/en/coins/" in u and u.rstrip("/")!=MINT_COINS.rstrip("/"):
                        urls.add(u)
                        if len(urls)>=max_urls: return sorted(urls)
            except Exception:
                continue
    except Exception:
        pass

    # Fallback to coins landing page.
    if len(urls)<5:
        r=fetch.get(MINT_COINS)
        soup=BeautifulSoup(r.text,"html.parser")
        for a in soup.find_all("a",href=True):
            u=urllib.parse.urljoin(MINT_BASE,a["href"]).split("#",1)[0]
            if "/en/coins/" in u and u.rstrip("/")!=MINT_COINS.rstrip("/"):
                urls.add(u)
    return sorted(urls)[:max_urls]

def parse_mint_product(fetch: Fetcher, url: str) -> Optional[CoinSpec]:
    r=fetch.get(url)
    soup=BeautifulSoup(r.text,"html.parser")
    pairs=_label_value_pairs(soup)
    h1=soup.find("h1")
    title=" ".join(h1.stripped_strings).strip() if h1 else ""
    all_text=" ".join(soup.stripped_strings)

    # Need evidence that this is a coin product, not an archive/category page.
    if not title or not any(k in pairs for k in ("denomination","facevalue")):
        return None

    denom_text=pairs.get("denomination") or pairs.get("facevalue") or ""
    denom=num(denom_text)
    if denom is None:
        return None

    year=integer(pairs.get("year"))
    if year is None:
        ym=re.search(r"\b(20\d{2}|19\d{2})\b", title)
        year=int(ym.group(1)) if ym else None

    weight=num(pairs.get("weight"))
    diam=num(pairs.get("diameter"))
    thick=num(pairs.get("thickness"))
    material=pairs.get("material") or ""

    # Some pages expose fineness as its own field rather than in MATERIAL.
    fin=None
    for k in ("gold finess","gold fineness","silver finess","silver fineness"):
        if k in pairs:
            fin=integer(pairs[k]); break
    if fin is None:
        fin=fineness_from_text(material+" "+all_text[:2500])

    primary=infer_primary_metal(material)
    typ=(pairs.get("type") or "").lower()
    quality=(pairs.get("minting quality") or "").lower()

    if "proof" in quality:
        coin_type="proof"
    elif "collector" in typ:
        coin_type="collector"
    elif "commemorative" in typ or "commemorative" in title.lower():
        coin_type="commemorative"
    else:
        coin_type="collector"

    mintage=integer(pairs.get("maximum issue"))
    edge=pairs.get("edge")
    artist=pairs.get("artist")

    rec=CoinSpec(
        record_id=stable_id("GR","MINT",url),
        country_code="GR",country="Greece",currency_code="EUR",
        denomination_value=denom,denomination_label=denom_text.strip(),
        year_from=year,year_to=year,issue_year=year,
        coin_type=coin_type,variant=title,title=title,
        composition_text=material or None,primary_metal=primary,fineness_per_mille=fin,
        weight_g=weight,diameter_mm=diam,thickness_mm=thick,
        edge=edge,mint="Bank of Greece / National Mint",mintage=mintage,
        external_ids={"bank_of_greece_mint_url":url},
        aliases=[title],
        source_priority=100,confidence=1.0,verified=True,
        provenance=[Provenance(
            "Bank of Greece National Mint — coin product page",url,"official_mint",
            retrieved_at=now_iso(),
            note=("Artist: "+artist) if artist else "Official product technical specification."
        )]
    )
    return normalize(rec)

def import_mint_products(fetch: Fetcher, max_urls=300) -> Tuple[List[CoinSpec],List[Dict[str,str]]]:
    urls=discover_mint_coin_urls(fetch,max_urls=max_urls)
    out=[]; failures=[]
    for i,u in enumerate(urls,1):
        try:
            rec=parse_mint_product(fetch,u)
            if rec: out.append(rec)
        except Exception as e:
            failures.append({"url":u,"error":str(e)})
    return out,failures

COUNTRY_NAME_TO_ISO={
    "Greece":"GR","GR":"GR","Germany":"DE","France":"FR","Italy":"IT","Spain":"ES","Portugal":"PT",
    "Austria":"AT","Belgium":"BE","Netherlands":"NL","Finland":"FI","Ireland":"IE",
    "United Kingdom":"GB","Great Britain":"GB","UK":"GB",
    "Switzerland":"CH","Denmark":"DK","Sweden":"SE","Norway":"NO",
    "Poland":"PL","Czech Republic":"CZ","Hungary":"HU","Romania":"RO","Serbia":"RS",
    "Bosnia and Herzegovina":"BA","Albania":"AL","Iceland":"IS","Moldova":"MD",
    "Ukraine":"UA","Croatia":"HR","Bulgaria":"BG","Slovakia":"SK","Slovenia":"SI",
    "Estonia":"EE","Latvia":"LV","Lithuania":"LT","Cyprus":"CY","Malta":"MT",
    "North Macedonia":"MK","Andorra":"AD","Monaco":"MC","San Marino":"SM",
    "Vatican City":"VA","Liechtenstein":"LI","Luxembourg":"LU",
    "United States":"US","USA":"US","US":"US",
}

def migrate_legacy(path: Path) -> List[CoinSpec]:
    """Migrate the existing CoinBids family JSON into the new provenance schema.

    IMPORTANT FIX (found by testing, not present in the originally-supplied
    version of this file): legacy records commonly cover MANY countries at
    once via a `countries` list (e.g. the 8 EU common-euro-spec records each
    list all 24 eurozone/microstate territories that share that exact
    physical specification). The original version of this function only
    read `countries[0]` — the FIRST country in that list — and silently
    dropped every other country. For the "2 euro"/"1 euro"/etc. records,
    that meant only "Austria" (alphabetically/positionally first in the
    list) ended up migrated, and every other country sharing that identical
    specification — including Greece, this project's primary use case — was
    silently lost. Verified empirically: migrating the 14-record file
    produced only 14 CoinSpec objects instead of the ~248 implied by "14
    records x ~24 countries each" for the EU-wide ones. Fixed to expand one
    CoinSpec per country in the list, since these are genuinely the same
    physical specification independently confirmed to apply to each of
    those countries (not one record accidentally covering several)."""
    if not path.exists(): return []
    data=json.loads(path.read_text(encoding="utf-8"))
    out=[]
    for row in data.get("records",[]):
        countries=row.get("countries") or ["Unknown"]
        for country in countries:
            cc=COUNTRY_NAME_TO_ISO.get(country,"")
            comp=row.get("composition")
            source=row.get("source") or "CoinBids legacy curated family database"
            url=row.get("source_url") or ""
            priority=row.get("source_priority") or ""
            source_type="official_central_bank" if "official" in str(priority) else "validation"
            r=CoinSpec(
                record_id=stable_id("LEGACY",country,row.get("currency"),row.get("denomination"),row.get("year_from"),row.get("year_to"),row.get("variant")),
                country_code=cc,country=country,currency_code=row.get("currency") or "",
                denomination_value=float(row.get("denomination") or 0),
                denomination_label=row.get("denomination_label") or str(row.get("denomination") or ""),
                year_from=row.get("year_from"),year_to=row.get("year_to"),
                coin_type="commemorative" if "commemorative" in str(row.get("variant","")).lower() else "circulation",
                variant=row.get("variant"),title=row.get("variant"),
                composition_text=comp,primary_metal=infer_primary_metal(comp),
                fineness_per_mille=row.get("fineness"),weight_g=row.get("weight_g"),
                diameter_mm=row.get("diameter_mm"),fine_metal_g=row.get("fine_metal_g"),
                source_priority=70 if "official" in str(priority) else 40,
                confidence=float(row.get("confidence",0.9) or 0.9),
                verified=bool(row.get("verified",False)),
                provenance=[Provenance(source,url,source_type,retrieved_at=now_iso(),
                                       note=f"Migrated from legacy CoinBids family database; original priority={priority}")]
            )
            out.append(normalize(r))
    return out

def dedupe(records: List[CoinSpec]) -> List[CoinSpec]:
    by={}
    for r in records:
        key=(r.country_code,r.currency_code,r.denomination_value,r.year_from,r.year_to,
             r.issue_year,r.coin_type,(r.variant or "").strip().lower())
        if key not in by:
            by[key]=r; continue
        a=by[key]
        # Higher priority wins; merge provenance.
        if (r.source_priority,r.confidence)>(a.source_priority,a.confidence):
            r.provenance=a.provenance+r.provenance
            by[key]=r
        else:
            a.provenance.extend(r.provenance)
    return list(by.values())

def audit(records):
    return {
        "records_total":len(records),
        "verified":sum(r.verified for r in records),
        "with_composition":sum(bool(r.composition_text) for r in records),
        "with_weight":sum(r.weight_g is not None for r in records),
        "with_diameter":sum(r.diameter_mm is not None for r in records),
        "precious_metal_records":sum(r.primary_metal in PRECIOUS for r in records),
        "metal_value_ready":sum(r.metal_value_ready for r in records),
        "official_or_eu":sum(any(p.source_type in {"official_central_bank","official_mint","eu"} for p in r.provenance) for r in records),
    }

def save(records,out_json,out_csv=None,failures=None):
    payload={
        "schema_version":2,
        "generated_at":now_iso(),
        "policy":{
            "primary_sources_first":True,
            "guess_missing_specs":False,
            "images_ingested":False,
            "precious_metal_requires_explicit_fineness":True,
            "variant_specific_matching":True
        },
        "audit":audit(records),
        "records":[asdict(normalize(r)) for r in records],
        "import_failures":failures or []
    }
    Path(out_json).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    if out_csv:
        cols=["record_id","country_code","country","currency_code","denomination_value","denomination_label",
              "year_from","year_to","issue_year","coin_type","variant","title","composition_text","primary_metal",
              "fineness_per_mille","weight_g","diameter_mm","thickness_mm","fine_metal_g","mintage",
              "source_priority","confidence","verified","metal_value_ready","source_url"]
        with open(out_csv,"w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
            for r in records:
                row={k:getattr(r,k,None) for k in cols if k!="source_url"}
                row["source_url"]=r.provenance[0].source_url if r.provenance else ""
                w.writerow(row)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out",default="coinbids_master_greece_v2.json")
    ap.add_argument("--csv",default="coinbids_master_greece_v2.csv")
    ap.add_argument("--legacy",default="")
    ap.add_argument("--max-mint-pages",type=int,default=250)
    ap.add_argument("--delay",type=float,default=1.25)
    ap.add_argument("--skip-mint",action="store_true")
    args=ap.parse_args()

    fetch=Fetcher(args.delay)
    records=[]; failures=[]

    print("[1/4] European Commission common euro specifications")
    common=parse_ec_common(fetch)
    records.extend(common)

    print("[2/4] ECB Greece national-side join")
    records.extend(parse_ecb_greece(fetch,common))

    if not args.skip_mint:
        print("[3/4] Bank of Greece National Mint product pages")
        mint,failed=import_mint_products(fetch,args.max_mint_pages)
        records.extend(mint);failures.extend(failed)
        print(f"  imported {len(mint)} official Mint product records; failures {len(failed)}")
    else:
        print("[3/4] Bank of Greece Mint skipped")

    if args.legacy:
        print("[4/4] Migrating legacy CoinBids family database")
        records.extend(migrate_legacy(Path(args.legacy)))
    else:
        print("[4/4] No legacy family database supplied")

    records=dedupe(records)
    save(records,args.out,args.csv,failures)
    print(json.dumps(audit(records),indent=2))
    print("Saved:",args.out)
    print("Saved:",args.csv)

if __name__=="__main__":
    main()
