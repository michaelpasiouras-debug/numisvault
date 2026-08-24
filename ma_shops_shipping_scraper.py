#!/usr/bin/env python3
"""
MA-Shops.com Shipping Fees Scraper
====================================

Μαζεύει τους πίνακες shipping fees (versandkosten.php) από dealers στο MA-Shops.com
και τους αποθηκεύει σε ένα ενιαίο CSV / Excel αρχείο.

ΔΟΜΗ ΤΗΣ ΣΕΛΙΔΑΣ (επιβεβαιωμένο δείγμα, https://www.ma-shops.com/kaup/versandkosten.php?lang=en):

    | Shipping fees  |                 |                          |                          |                 |
    |----------------|-----------------|--------------------------|--------------------------|-----------------|
    |                | up to 11.66 US$ | 11.66 US$ to 174.89 US$ | 174.89 US$ to 349.78 US$ | over 349.78 US$ |
    | Germany        | 4.55 US$        | 6.41 US$                | 6.88 US$                 | Free shipping   |
    | European Union | 11.66 US$       | 11.66 US$                | 11.66 US$                | 11.66 US$       |
    | World          | 11.66 US$       | 11.66 US$                | 11.66 US$                | 23.32 US$       |

Δηλαδή: στήλες = "price tiers" (εύρος αξίας παραγγελίας), γραμμές = προορισμός
(μπορεί να είναι μεμονωμένη χώρα, "European Union" συγκεντρωτικά, "World", κλπ —
διαφέρει ανά dealer, όπως περιγράφηκε).

Ο parser παρακάτω είναι σχεδιασμένος να είναι ανεκτικός σε αυτή την ετερογένεια:
διαβάζει ΟΠΟΙΟΝΔΗΠΟΤΕ αριθμό γραμμών/στηλών βρει στον πίνακα.

ΣΗΜΑΝΤΙΚΟ: Το sandbox αυτού του περιβάλλοντος δεν έχει πρόσβαση στο δίκτυο προς το
ma-shops.com, άρα ο κώδικας ΔΕΝ έχει τρέξει end-to-end εδώ. Έχει δοκιμαστεί ο parser
πάνω στο πραγματικό HTML που ανακτήθηκε (βλ. test_parser.py). Τρέξε πρώτα με
--limit 5 --debug για να επιβεβαιώσεις ότι όλα δουλεύουν στο δικό σου δίκτυο πριν
τρέξεις το πλήρες batch των 1000+ dealers.

ΧΡΗΣΗ
-----

1) Με λίστα dealers που ήδη έχεις (π.χ. από CoinBids DB) — πιο αξιόπιστο:

    python ma_shops_shipping_scraper.py --dealers-file dealers.txt --out shipping_fees.csv

   Το dealers.txt: ένα slug ανά γραμμή (π.χ. "dylla", "kaup", "koelnermuenzkabinett")
   ή ένα πλήρες URL ανά γραμμή — και τα δύο αναγνωρίζονται.

2) Αυτόματη ανακάλυψη dealers (best-effort, crawl στις κατηγορίες) + scrape:

    python ma_shops_shipping_scraper.py --discover --out shipping_fees.csv

3) Συνδυασμός (προτεινόμενο): δικιά σου λίστα + auto-discovery για ό,τι λείπει:

    python ma_shops_shipping_scraper.py --dealers-file dealers.txt --discover --out shipping_fees.csv

4) Μία εκτέλεση (button press):

    python ma_shops_shipping_scraper.py --dealers-file dealers.txt --out shipping_fees.csv

5) Επαναλαμβανόμενη εκτέλεση κάθε Ν ώρες (τρέχει σαν daemon μέσα στο ίδιο process):

    python ma_shops_shipping_scraper.py --dealers-file dealers.txt --out shipping_fees.csv --loop --interval-hours 6

   (Εναλλακτικά, πιο robust: βάλε το single-run command σε cron / Windows Task
   Scheduler — βλ. README.md)

ΕΞΑΡΤΗΣΕΙΣ
----------
    pip install requests beautifulsoup4 pandas openpyxl lxml

ΣΥΜΒΑΤΟΤΗΤΑ: Λειτουργεί με Python 3.8+ (συμπεριλαμβανομένου Python 3.9, η
τελευταία έκδοση με επίσημη υποστήριξη Windows 7).
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.ma-shops.com"
USER_AGENT = "CoinBidsShippingResearchBot/1.0 (+contact: research use, respects robots.txt, rate-limited)"

# Κατηγορίες-σελίδες που χρησιμοποιούνται σαν "seeds" για auto-discovery.
# Αυτές είναι πραγματικές, επιβεβαιωμένες διαδρομές του site.
DISCOVERY_SEED_PATHS = [
    "/coins/", "/paper-money/", "/world/", "/usa/", "/ancient/", "/euro/",
    "/european-coins/", "/medieval/", "/gold-silver-platinum/", "/medals-jetons/",
    "/germany-before-1871/", "/germany-since-1871/", "/islamic-coins/",
    "/holy-roman-empire/", "/canadian-coins/", "/united-kingdom/", "/france/",
    "/africa/", "/asia/", "/australia-oceania/", "/netherlands/",
    "/error-coins/", "/motif-coins/", "/stamps/", "/militaria/", "/watches/",
    "/jewellery/", "/emergency-coins/",
]

# Regex για να εντοπίζουμε dealer slugs μέσα σε href attributes, π.χ.
# https://www.ma-shops.com/dylla/item.php?id=123  ή  /dylla/
DEALER_LINK_RE = re.compile(
    r"(?:https?://(?:www\.)?ma-shops\.com)?/([a-zA-Z0-9_\-]+)/(?:item\.php|new\.php|cat\.php|feedback\.php|search\.php|versandkosten\.php|withdraw\.php|agb\.php|contact\.php|usermenu\.php)?"
)

# Μονοπάτια που ΔΕΝ είναι dealer shops (system/generic paths του site)
NON_DEALER_SLUGS = {
    "shops", "maservice", "incUser", "images", "coins", "paper-money", "world",
    "usa", "ancient", "euro", "european-coins", "medieval", "gold-silver-platinum",
    "medals-jetons", "germany-before-1871", "germany-since-1871", "islamic-coins",
    "holy-roman-empire", "canadian-coins", "united-kingdom", "france", "africa",
    "asia", "australia-oceania", "netherlands", "error-coins", "motif-coins",
    "stamps", "militaria", "watches", "jewellery", "emergency-coins", "new",
    "privacy", "central-america", "south-america", "mexico", "cuba",
    "medieval-coins", "literature", "accessories", "coin-accessories",
    "postcards", "antiques-art", "coin-weights", "design", "numis-sheets",
    "historical-documents",
}


@dataclass
class ShippingRow:
    dealer_slug: str
    dealer_name: str
    destination: str
    tier_label: str
    price_tier_min: Optional[float]
    price_tier_max: Optional[float]
    weight_tier_min_g: Optional[float]
    weight_tier_max_g: Optional[float]
    cost: Optional[float]
    currency: Optional[str]
    free_shipping: bool
    source_url: str
    scraped_at: str


class PoliteSession:
    """requests.Session wrapper με rate limiting + robots.txt check + retries."""

    def __init__(self, delay_seconds: float = 1.5, jitter: float = 0.7,
                 max_retries: int = 3, respect_robots: bool = True):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT, "Accept-Language": "en",
            "Cache-Control": "no-cache", "Pragma": "no-cache",
        })
        self.delay = delay_seconds
        self.jitter = jitter
        self.max_retries = max_retries
        self._last_request_time = 0.0
        self._robots = None
        if respect_robots:
            self._robots = urllib.robotparser.RobotFileParser()
            self._robots.set_url(urljoin(BASE, "/robots.txt"))
            try:
                self._robots.read()
            except Exception:
                logging.warning("Could not read robots.txt — proceeding cautiously.")
                self._robots = None

    def allowed(self, url: str) -> bool:
        if self._robots is None:
            return True
        try:
            return self._robots.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        wait = self.delay + random.uniform(0, self.jitter) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.time()

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        if not self.allowed(url):
            logging.warning(f"robots.txt disallows: {url} — skipping")
            return None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=20, **kwargs)
                if resp.status_code == 429:
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    logging.warning(f"429 on {url}, backing off {backoff:.1f}s")
                    time.sleep(backoff)
                    continue
                if resp.status_code >= 500:
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    logging.warning(f"{resp.status_code} on {url}, retry in {backoff:.1f}s")
                    time.sleep(backoff)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                logging.warning(f"Request error on {url} (attempt {attempt}/{self.max_retries}): {e}")
                time.sleep(2 * attempt)
        logging.error(f"Giving up on {url} after {self.max_retries} attempts")
        return None


# ---------------------------------------------------------------------------
# Dealer discovery
# ---------------------------------------------------------------------------

def load_dealers_from_file(path: Path) -> list[str]:
    slugs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Δέξου είτε bare slug είτε πλήρες URL
        if line.startswith("http"):
            parsed = urlparse(line)
            parts = [p for p in parsed.path.split("/") if p]
            if parts:
                slugs.append(parts[0])
        else:
            # αν είναι CSV με header/κόμματα, πάρε το πρώτο πεδίο
            slugs.append(line.split(",")[0].strip())
    # de-dup, keep order
    seen = set()
    out = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def discover_dealers(session: PoliteSession, max_pages_per_seed: int = 1) -> set[str]:
    """
    Best-effort ανακάλυψη dealer slugs μέσα από τις listing/category σελίδες.
    ΠΡΟΣΟΧΗ: Αυτό ΔΕΝ εγγυάται πλήρη κάλυψη των 1000+ dealers — οι category
    σελίδες δείχνουν μόνο dealers με ενεργά αντικείμενα στη συγκεκριμένη κατηγορία,
    και δεν είναι σίγουρο πόσες σελίδες pagination υπάρχουν. Χρησιμοποίησέ το
    ΣΥΜΠΛΗΡΩΜΑΤΙΚΑ με δική σου λίστα dealers, όχι σαν μοναδική πηγή.
    """
    found: set[str] = set()
    for seed_path in DISCOVERY_SEED_PATHS:
        url = urljoin(BASE, seed_path)
        resp = session.get(url)
        if resp is None:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = DEALER_LINK_RE.match(href) or DEALER_LINK_RE.search(href)
            if m:
                slug = m.group(1)
                if slug and slug not in NON_DEALER_SLUGS and "." not in slug:
                    found.add(slug)
        logging.info(f"Discovery seed {seed_path}: {len(found)} unique dealer slugs so far")
    return found


# ---------------------------------------------------------------------------
# Shipping-page parsing
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS = {"US$": "USD", "$": "USD", "€": "EUR", "EUR": "EUR",
                     "£": "GBP", "GBP": "GBP", "CHF": "CHF"}

FREE_SHIPPING_RE = re.compile(r"free\s+shipping|kostenlos|gratis", re.IGNORECASE)
NUMBER_RE = re.compile(r"(\d[\d.,]*\d|\d)")
CURRENCY_RE = re.compile(r"(US\$|CHF|EUR|GBP|[€£$])")


def parse_money(text: str) -> tuple[Optional[float], Optional[str]]:
    """Εξάγει (ποσό, νόμισμα) από string τύπου '11.66 US$' ή '6,88 €'."""
    text = text.strip()
    if not text:
        return None, None
    currency = None
    cm = CURRENCY_RE.search(text)
    if cm:
        currency = CURRENCY_SYMBOLS.get(cm.group(1), cm.group(1))
    nm = NUMBER_RE.search(text.replace(",", "."))
    amount = None
    if nm:
        try:
            amount = float(nm.group(1))
        except ValueError:
            amount = None
    return amount, currency


WEIGHT_UNIT_RE = re.compile(r"(\d[\d.,]*)\s*(kg|g)\b", re.IGNORECASE)
PRICE_TOKEN_RE = re.compile(
    r"(up to|bis|over|über|ab|from|to|und|and)?\s*"
    r"(\d[\d.,]*\d|\d)\s*(US\$|CHF|EUR|GBP|[€£$])",
    re.IGNORECASE,
)


def _direction(word: Optional[str]) -> str:
    """Επιστρέφει 'upto' | 'over' | 'between' | 'unknown' για μια λέξη-δείκτη ορίου."""
    if not word:
        return "unknown"
    w = word.lower().strip()
    if w in ("up to", "bis", "to"):
        return "upto"
    if w in ("over", "über", "ab", "from"):
        return "over"
    return "unknown"


def parse_tier_label(label: str) -> dict:
    """
    Αναλύει tier labels όπως:
      'up to 11.66 US$'                              -> price (0, 11.66)
      '11.66 US$ to 174.89 US$'                       -> price (11.66, 174.89)
      'over 349.78 US$'                               -> price (349.78, None)
      'bis 35,00 EUR und bis 30 g'                    -> price (0, 35.00), weight (0, 30) [g]
      'über 4000,00 EUR und über 1800 g'              -> price (4000.00, None), weight (1800, None) [g]
      'bis 500,00 EUR und 500 bis 1500 g'             -> price (0, 500.00), weight (500, 1500) [g]

    Επιστρέφει dict: {price_min, price_max, weight_min_g, weight_max_g}
    Όλα Optional[float]. Το βάρος μετατρέπεται πάντα σε γραμμάρια (kg -> *1000).
    """
    result = {"price_min": None, "price_max": None, "weight_min_g": None, "weight_max_g": None}
    label_clean = label.strip()
    if not label_clean:
        return result

    # --- Τιμή (currency-anchored τμήματα) ---
    price_matches = list(PRICE_TOKEN_RE.finditer(label_clean))
    price_nums = []
    directions = []
    for m in price_matches:
        word, num, _cur = m.group(1), m.group(2), m.group(3)
        try:
            price_nums.append(float(num.replace(",", ".")))
        except ValueError:
            continue
        directions.append(_direction(word))

    if price_nums:
        # Explicit two-price ranges (e.g. "21 EUR to 50 EUR") must win over
        # the direction token on the second number. Otherwise "to" would be
        # mistaken for a standalone "up to" and incorrectly produce 0..50.
        if len(price_nums) >= 2:
            result["price_min"], result["price_max"] = price_nums[0], price_nums[1]
        elif "upto" in directions:
            result["price_min"], result["price_max"] = 0.0, price_nums[directions.index("upto")]
        elif "over" in directions:
            result["price_min"], result["price_max"] = price_nums[directions.index("over")], None
        else:
            result["price_min"] = result["price_max"] = price_nums[0]

    # --- Βάρος (g/kg-anchored τμήματα) ---
    # Πρώτα έλεγξε για ρητό εύρος "X bis/to Y g" (π.χ. "500 bis 1500 g") όπου
    # μόνο ο δεύτερος αριθμός έχει τη μονάδα δίπλα του.
    weight_range_m = re.search(
        r"(\d[\d.,]*)\s*(?:bis|to)\s*(\d[\d.,]*)\s*(kg|g)\b", label_clean, re.IGNORECASE
    )
    if weight_range_m:
        lo, hi, unit = weight_range_m.groups()
        mult = 1000.0 if unit.lower() == "kg" else 1.0
        try:
            result["weight_min_g"] = float(lo.replace(",", ".")) * mult
            result["weight_max_g"] = float(hi.replace(",", ".")) * mult
        except ValueError:
            pass
        return result

    weight_matches = list(WEIGHT_UNIT_RE.finditer(label_clean))
    weight_nums_g = []
    weight_directions = []
    for m in weight_matches:
        num, unit = m.group(1), m.group(2).lower()
        try:
            val = float(num.replace(",", "."))
        except ValueError:
            continue
        if unit == "kg":
            val *= 1000.0
        weight_nums_g.append(val)
        # ψάξε τη λέξη-δείκτη ΠΡΙΝ από αυτό το ταίριασμα βάρους (π.χ. "bis 30 g", "über 1800 g")
        preceding = label_clean[max(0, m.start() - 12):m.start()].lower()
        if "bis" in preceding or "up to" in preceding or preceding.strip().endswith("to"):
            weight_directions.append("upto")
        elif "über" in preceding or "over" in preceding or "ab" in preceding:
            weight_directions.append("over")
        else:
            weight_directions.append("unknown")

    if weight_nums_g:
        if "upto" in weight_directions:
            result["weight_min_g"], result["weight_max_g"] = 0.0, weight_nums_g[weight_directions.index("upto")]
        elif "over" in weight_directions:
            result["weight_min_g"], result["weight_max_g"] = weight_nums_g[weight_directions.index("over")], None
        elif len(weight_nums_g) >= 2:
            result["weight_min_g"], result["weight_max_g"] = weight_nums_g[0], weight_nums_g[1]
        else:
            result["weight_min_g"] = result["weight_max_g"] = weight_nums_g[0]

    return result


def _shipping_table_score(table) -> int:
    trs = table.find_all("tr")
    if len(trs) < 2: return -1
    txt = table.get_text(" ", strip=True).lower()
    score = 0
    if any(k in txt for k in ("shipping fees","shipping","versandkosten","frais de port","verzendkosten")): score += 10
    if any(k in txt for k in ("germany","european union","world","europe")): score += 8
    if any(k in txt for k in ("up to","over"," to ","bis ","über ","ab ")): score += 8
    if CURRENCY_RE.search(table.get_text(" ", strip=True)): score += 5
    widths=[len(tr.find_all(["th","td"])) for tr in trs]
    if widths and max(widths) >= 3: score += 8
    if widths and max(widths) >= 5: score += 4
    return score

def find_shipping_table(soup: BeautifulSoup):
    """Choose the real structured shipping matrix instead of the first shipping-like table."""
    tables=soup.find_all("table")
    if not tables: return None
    ranked=sorted(((_shipping_table_score(t),i,t) for i,t in enumerate(tables)),
                  key=lambda x:(x[0],-x[1]), reverse=True)
    return ranked[0][2] if ranked[0][0] >= 10 else None


def parse_versandkosten_page(html: str, dealer_slug: str, dealer_name: str,
                              source_url: str) -> list[ShippingRow]:
    soup = BeautifulSoup(html, "lxml")
    table = find_shipping_table(soup)
    rows_out: list[ShippingRow] = []
    scraped_at = datetime.now(timezone.utc).isoformat()

    if table is None:
        logging.warning(f"[{dealer_slug}] Δεν βρέθηκε πίνακας shipping fees στη σελίδα.")
        return rows_out

    trs = table.find_all("tr")
    if not trs:
        return rows_out

    # Header row: πρώτη γραμμή με >1 κελιά — τα κελιά μετά το πρώτο είναι tier labels
    header_cells = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
    tier_labels = header_cells[1:] if len(header_cells) > 1 else []

    # Ειδική περίπτωση: η πρώτη γραμμή είναι απλά ένας τίτλος ("Shipping fees")
    # χωρίς πραγματικά tier labels (όλα τα υπόλοιπα κελιά κενά) — τότε η
    # ΕΠΟΜΕΝΗ γραμμή είναι το πραγματικό header row με τα tier labels.
    # (Δεν το κάνουμε αυτό αν η 2η γραμμή είναι ήδη γραμμή δεδομένων — δηλ.
    # μόνο αν το header_cells[1:] είναι πραγματικά άδειο/κενό.)
    tier_labels_empty = len(tier_labels) == 0 or all(not c.strip() for c in tier_labels)
    if tier_labels_empty and len(trs) > 1:
        header_cells2 = [c.get_text(" ", strip=True) for c in trs[1].find_all(["th", "td"])]
        if len(header_cells2) > 1 and any(header_cells2[1:]):
            tier_labels = header_cells2[1:]
            trs = trs[1:]  # skip the pure-title row

    data_rows = trs[1:]

    for tr in data_rows:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        cell_texts = [c.get_text(" ", strip=True) for c in cells]
        destination = cell_texts[0]
        if not destination:
            continue
        cost_cells = cell_texts[1:]
        if not cost_cells:
            continue

        # CoinBids: κρατάμε ΟΛΑ τα price/weight tiers. Κάθε κελί κόστους
        # αντιστοιχεί στην ίδια θέση του header. Έτσι μπορούμε αργότερα να
        # επιλέξουμε το σωστό shipping από item price + coin weight.
        for tier_idx, cost_text in enumerate(cost_cells):
            tier_label = tier_labels[tier_idx] if tier_idx < len(tier_labels) else f"tier_{tier_idx + 1}"
            tier_info = parse_tier_label(tier_label)
            is_free = bool(FREE_SHIPPING_RE.search(cost_text))

            if is_free:
                amount, currency = 0.0, None
                # Το "Free shipping" cell δεν περιέχει currency. Προσπάθησε
                # να τη συμπεράνεις από το header ή από άλλο cell της ίδιας row.
                for candidate in [tier_label] + cost_cells:
                    _, c = parse_money(candidate)
                    if c:
                        currency = c
                        break
                # Weight-only free-shipping tables may contain no monetary token
                # anywhere in the row/header. Since this scraper explicitly
                # requests curr=EUR, preserve EUR instead of emitting null.
                if currency is None and "curr=EUR" in source_url:
                    currency = "EUR"
            else:
                amount, currency = parse_money(cost_text)

            # Κενό/μη αναγνωρίσιμο cell: μην εφεύρεις shipping cost.
            if amount is None and not is_free:
                continue

            rows_out.append(ShippingRow(
                dealer_slug=dealer_slug,
                dealer_name=dealer_name,
                destination=destination,
                tier_label=tier_label,
                price_tier_min=tier_info["price_min"],
                price_tier_max=tier_info["price_max"],
                weight_tier_min_g=tier_info["weight_min_g"],
                weight_tier_max_g=tier_info["weight_max_g"],
                cost=amount,
                currency=currency,
                free_shipping=is_free,
                source_url=source_url,
                scraped_at=scraped_at,
            ))

    unique_tiers={r.tier_label for r in rows_out}
    if len(tier_labels)>1 and len(unique_tiers)<=1:
        logging.error(f"[{dealer_slug}] TIER COLLAPSE: HTML had {len(tier_labels)} headers but output has {len(unique_tiers)} tier(s)")
    return rows_out


def validate_dealer_rows(dealer_slug: str, rows: list[ShippingRow]) -> list[str]:
    issues=[]
    if not rows: return ["no shipping rows parsed"]
    by_dest={}
    for r in rows: by_dest.setdefault(r.destination,[]).append(r)
    for dest,rr in by_dest.items():
        seen={}
        for r in rr:
            key=(r.price_tier_min,r.price_tier_max,r.weight_tier_min_g,r.weight_tier_max_g,r.currency)
            if key in seen and seen[key] != r.cost:
                issues.append(f"{dest}: conflicting duplicate {key}: {seen[key]} vs {r.cost}")
            seen[key]=r.cost
            tl=(r.tier_label or "").lower()
            if any(x in tl for x in ("up to","over"," to ","bis ","über ","ab ")) and CURRENCY_RE.search(r.tier_label or "") and r.price_tier_min is None and r.price_tier_max is None:
                issues.append(f"{dest}: unparsed price tier '{r.tier_label}'")
    return issues


def normalize_destination_name(name: str) -> str:
    s = (name or "").strip().lower()
    aliases = {
        "greece": "greece",
        "hellas": "greece",
        "ellada": "greece",
        "ελλαδα": "greece",
        "european union": "european union",
        "eu": "european union",
        "europe": "europe",
        "world": "world",
        "worldwide": "world",
    }
    return aliases.get(s, s)


def destination_priority(destination: str, target_country: str = "Greece") -> int:
    """Lower is better: exact country, then EU, then Europe, then World."""
    d = normalize_destination_name(destination)
    t = normalize_destination_name(target_country)
    if d == t:
        return 0
    if t == "greece" and d == "european union":
        return 1
    if d == "europe":
        return 2
    if d == "world":
        return 3
    return 99


def shipping_rule_matches(row: ShippingRow, item_price: Optional[float],
                          item_weight_g: Optional[float]) -> bool:
    """Return True only when the rule safely covers the known item data."""
    if item_price is not None:
        if row.price_tier_min is not None and item_price < row.price_tier_min:
            return False
        if row.price_tier_max is not None and item_price > row.price_tier_max:
            return False
    elif row.price_tier_min is not None or row.price_tier_max is not None:
        return False

    if item_weight_g is not None:
        if row.weight_tier_min_g is not None and item_weight_g < row.weight_tier_min_g:
            return False
        if row.weight_tier_max_g is not None and item_weight_g > row.weight_tier_max_g:
            return False
    elif row.weight_tier_min_g is not None or row.weight_tier_max_g is not None:
        return False
    return True


def select_shipping_rule(rows: list[ShippingRow], dealer_slug: str,
                         target_country: str = "Greece",
                         item_price: Optional[float] = None,
                         item_weight_g: Optional[float] = None) -> Optional[ShippingRow]:
    """Pick the most specific valid rule for one dealer/item/destination."""
    candidates = []
    for r in rows:
        if r.dealer_slug != dealer_slug:
            continue
        pr = destination_priority(r.destination, target_country)
        if pr >= 99:
            continue
        if not shipping_rule_matches(r, item_price, item_weight_g):
            continue

        price_span = (
            r.price_tier_max - r.price_tier_min
            if r.price_tier_min is not None and r.price_tier_max is not None
            else float("inf")
        )
        weight_span = (
            r.weight_tier_max_g - r.weight_tier_min_g
            if r.weight_tier_min_g is not None and r.weight_tier_max_g is not None
            else float("inf")
        )
        candidates.append((pr, price_span, weight_span, r))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return candidates[0][3]



def extract_dealer_name(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    # Το <title> είναι συνήθως "Dealer Name | MA-Shops"
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        title = title.split("|")[0].strip()
        if title:
            return title
    return fallback


# ---------------------------------------------------------------------------
# Main scrape loop
# ---------------------------------------------------------------------------

def scrape_dealers(dealer_slugs: list[str], session: PoliteSession,
                    lang: str = "en", currency_param: str = "EUR",
                    debug_dir: Optional[Path] = None) -> list[ShippingRow]:
    all_rows: list[ShippingRow] = []
    total = len(dealer_slugs)
    for idx, slug in enumerate(dealer_slugs, start=1):
        url = f"{BASE}/{slug}/versandkosten.php?lang={lang}&curr={currency_param}"
        logging.info(f"[{idx}/{total}] {slug} -> {url}")
        resp = session.get(url)
        if resp is None:
            logging.warning(f"[{slug}] Αποτυχία fetch, skipping.")
            continue
        if resp.status_code == 404:
            logging.warning(f"[{slug}] 404 — μάλλον δεν υπάρχει τέτοιο shop/σελίδα.")
            continue

        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{slug}.html").write_text(resp.text, encoding="utf-8")

        dealer_name = extract_dealer_name(resp.text, fallback=slug)
        rows = parse_versandkosten_page(resp.text, slug, dealer_name, url)
        if not rows:
            logging.info(f"[{slug}] Καμία γραμμή shipping fees εξήχθη (κενός/άγνωστος πίνακας).")
        for issue in validate_dealer_rows(slug, rows):
            logging.warning(f"[{slug}] SHIPPING VALIDATION: {issue}")
        all_rows.extend(rows)
    return all_rows


def write_output(rows: list[ShippingRow], out_path: Path, fmt: str):
    fieldnames = ["dealer_slug", "dealer_name", "destination", "tier_label",
                  "price_tier_min", "price_tier_max", "weight_tier_min_g", "weight_tier_max_g",
                  "cost", "currency", "free_shipping", "source_url", "scraped_at"]
    dict_rows = [r.__dict__ for r in rows]

    if fmt == "csv":
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dict_rows)
    elif fmt == "xlsx":
        import pandas as pd
        df = pd.DataFrame(dict_rows, columns=fieldnames)
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="shipping_fees")
            # auto width
            ws = writer.sheets["shipping_fees"]
            for col_cells in ws.columns:
                length = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
                ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 50)
    else:
        raise ValueError(f"Άγνωστη μορφή εξόδου: {fmt}")

    logging.info(f"Γράφτηκαν {len(rows)} γραμμές στο {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_once(args):
    session = PoliteSession(delay_seconds=args.delay, respect_robots=not args.ignore_robots)

    slugs: list[str] = []
    if getattr(args, "dealer", None):
        slugs.append(args.dealer.strip())
    if args.dealers_file:
        slugs.extend(load_dealers_from_file(Path(args.dealers_file)))
        logging.info(f"Φορτώθηκαν {len(slugs)} dealers από {args.dealers_file}")

    if args.discover:
        discovered = discover_dealers(session)
        logging.info(f"Auto-discovery βρήκε {len(discovered)} υποψήφια dealer slugs")
        for s in discovered:
            if s not in slugs:
                slugs.append(s)

    if not slugs:
        logging.error("Δεν δόθηκαν dealers (ούτε --dealers-file ούτε --discover). Τερματισμός.")
        sys.exit(1)

    if args.limit:
        slugs = slugs[: args.limit]

    logging.info(f"Συνολικά {len(slugs)} dealers προς scrape.")

    debug_dir = Path(args.debug_dir) if args.debug else None
    rows = scrape_dealers(slugs, session, lang=args.lang, currency_param=args.currency,
                           debug_dir=debug_dir)

    out_path = Path(args.out)
    write_output(rows, out_path, fmt=args.format)

    # Επιπλέον: πάντα κράτα και ένα timestamped αντίγραφο για ιστορικό tracking
    if args.archive:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = out_path.with_name(f"{out_path.stem}_{ts}{out_path.suffix}")
        write_output(rows, archive_path, fmt=args.format)


def main():
    parser = argparse.ArgumentParser(description="MA-Shops.com shipping fees scraper")
    parser.add_argument("--dealers-file", help="Αρχείο με dealer slugs/URLs, ένα ανά γραμμή")
    parser.add_argument("--dealer", help="Scrape μόνο ένα dealer slug για validation")
    parser.add_argument("--discover", action="store_true",
                         help="Best-effort auto-discovery dealers από category σελίδες")
    parser.add_argument("--out", default="shipping_fees.csv", help="Path αρχείου εξόδου")
    parser.add_argument("--format", choices=["csv", "xlsx"], default=None,
                         help="Μορφή εξόδου (default: από την κατάληξη του --out)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Περιόρισε σε Ν dealers (χρήσιμο για δοκιμή)")
    parser.add_argument("--delay", type=float, default=1.5,
                         help="Δευτερόλεπτα καθυστέρησης ανάμεσα σε requests (default 1.5)")
    parser.add_argument("--lang", default="en", help="lang param στο URL (default en)")
    parser.add_argument("--currency", default="EUR", help="curr param στο URL (default EUR)")
    parser.add_argument("--ignore-robots", action="store_true",
                         help="Αγνόησε το robots.txt (ΔΕΝ συνιστάται)")
    parser.add_argument("--debug", action="store_true",
                         help="Αποθήκευσε το raw HTML κάθε dealer σελίδας για επιθεώρηση")
    parser.add_argument("--debug-dir", default="debug_html", help="Φάκελος για --debug output")
    parser.add_argument("--archive", action="store_true",
                         help="Κράτα και timestamped αντίγραφο κάθε run (ιστορικό)")
    parser.add_argument("--loop", action="store_true",
                         help="Τρέξε επαναλαμβανόμενα κάθε --interval-hours ώρες")
    parser.add_argument("--interval-hours", type=float, default=6.0,
                         help="Διάστημα επανάληψης σε ώρες (μόνο με --loop)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.format is None:
        args.format = "xlsx" if args.out.lower().endswith(".xlsx") else "csv"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("ma_shops_scraper.log", encoding="utf-8"),
        ],
    )

    if args.loop:
        logging.info(f"Loop mode: θα τρέχει κάθε {args.interval_hours} ώρες. Ctrl+C για διακοπή.")
        while True:
            try:
                run_once(args)
            except Exception:
                logging.exception("Σφάλμα στο run — θα ξαναδοκιμάσει στον επόμενο κύκλο.")
            logging.info(f"Επόμενο run σε {args.interval_hours} ώρες...")
            time.sleep(args.interval_hours * 3600)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
