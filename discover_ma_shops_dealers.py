#!/usr/bin/env python3
import argparse, csv, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

BASE="https://www.ma-shops.com/"
HEADERS={"User-Agent":"Mozilla/5.0 (compatible; CoinBidsDealerDiscovery/1.0)"}
RESERVED={
 "index","shop","shops","search","suche","news","info","contact","kontakt","login",
 "register","agb","privacy","datenschutz","impressum","help","faq","api","images",
 "css","js","en","de","fr","it","es","nl","pl"
}

def norm_slug(s):
    s=(s or "").strip().strip("/").lower()
    return s if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}",s) and s not in RESERVED else None

def slug_from_url(href):
    try:
        u=urlparse(urljoin(BASE,href))
        if "ma-shops.com" not in u.netloc.lower(): return None
        parts=[p for p in u.path.split("/") if p]
        return norm_slug(parts[0]) if parts else None
    except Exception:return None

def fetch(session,url):
    r=session.get(url,headers=HEADERS,timeout=25,allow_redirects=True)
    print(f"GET {url} -> {r.status_code} ({len(r.text)} chars)")
    r.raise_for_status()
    return r.text

def discover_from_page(session,url):
    html=fetch(session,url); soup=BeautifulSoup(html,"html.parser"); out=set()
    for a in soup.find_all("a",href=True):
        s=slug_from_url(a["href"])
        if s: out.add(s)
    return out

def discover_api(session):
    urls=[
      "https://www.ma-shops.com/apiShops.php",
      "https://www.ma-shops.com/apiShops.php?lang=en",
    ]
    out=set()
    for url in urls:
        try:
            text=fetch(session,url)
            # Extract any first path component from MA-Shops URLs and likely slug tokens.
            for m in re.finditer(r'https?://(?:www\.)?ma-shops\.com/([a-zA-Z0-9_-]+)',text):
                s=norm_slug(m.group(1))
                if s: out.add(s)
            soup=BeautifulSoup(text,"html.parser")
            for a in soup.find_all("a",href=True):
                s=slug_from_url(a["href"])
                if s: out.add(s)
        except Exception as e:
            print("apiShops warning:",type(e).__name__,e)
    return out

def verify_shipping(session,slug):
    url=f"https://www.ma-shops.com/{slug}/versandkosten.php?lang=en&curr=EUR"
    try:
        r=session.get(url,headers=HEADERS,timeout=20,allow_redirects=True)
        txt=r.text.lower()
        ok=(r.status_code==200 and len(r.text)>5000 and
            ("shipping" in txt or "versand" in txt) and
            "page not found" not in txt and "404" not in (r.url or ""))
        return ok,r.status_code,r.url
    except Exception as e:
        return False,None,f"{type(e).__name__}: {e}"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--existing",default="dealers_from_apishops.txt")
    ap.add_argument("--delay",type=float,default=1.2)
    args=ap.parse_args()
    existing_path=Path(args.existing)
    if not existing_path.exists():
        raise SystemExit(f"Missing {existing_path}")
    existing={norm_slug(x) for x in existing_path.read_text(encoding="utf-8-sig").splitlines()}
    existing.discard(None)
    print("Existing dealers:",len(existing))

    s=requests.Session()
    candidates=set(existing)
    candidates |= discover_api(s)

    # Public MA-Shops pages can expose dealer links not present in apiShops.
    seed_urls=[
      "https://www.ma-shops.com/",
      "https://www.ma-shops.com/shops.php?lang=en",
      "https://www.ma-shops.com/shops.php",
    ]
    for url in seed_urls:
        try:
            candidates |= discover_from_page(s,url)
        except Exception as e:
            print("seed warning:",url,type(e).__name__,e)

    # Explicit Barcelona probes: discovery only; verification below decides validity.
    candidates |= {"coinsnumismaticabarcelona","coinsbarcelona","coinsnb","coins"}

    print("Candidate slugs:",len(candidates))
    verified=[]
    for i,slug in enumerate(sorted(candidates),1):
        ok,status,url=verify_shipping(s,slug)
        print(f"[{i}/{len(candidates)}] {slug}: {'OK' if ok else 'no'}")
        if ok: verified.append((slug,status,url))
        time.sleep(max(0,args.delay))

    verified_slugs={x[0] for x in verified}
    missing=sorted(verified_slugs-existing)
    gone=sorted(existing-verified_slugs)

    Path("all_discovered_dealers.txt").write_text("\n".join(sorted(verified_slugs))+"\n",encoding="utf-8")
    Path("missing_dealers.txt").write_text("\n".join(missing)+("\n" if missing else ""),encoding="utf-8")
    with open("dealer_comparison.csv","w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(["dealer_slug","in_existing_289","verified_shipping_page","status"])
        for slug in sorted(existing|verified_slugs):
            w.writerow([slug,slug in existing,slug in verified_slugs,
                        "MISSING_FROM_289" if slug in missing else ("OK" if slug in verified_slugs else "NOT_VERIFIED")])
    print("\nDONE")
    print("Verified:",len(verified_slugs))
    print("Missing from existing list:",len(missing))
    print("Existing not verified:",len(gone))
    print("Files: all_discovered_dealers.txt, missing_dealers.txt, dealer_comparison.csv")

if __name__=="__main__":
    main()
