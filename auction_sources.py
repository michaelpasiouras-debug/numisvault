"""
CoinBids Auction Intelligence 3.0 — source adapter framework (Phase 3-4).

See auction_source_matrix.md for the actual, researched feasibility findings
behind why only ManualComparableAdapter and CSVComparableAdapter are enabled
in this delivery. Every other named source (CoinArchivesAdapter,
AcsearchAdapter, BiddrAdapter, NumisBidsAdapter) is defined here as a
disabled stub that reports its own capability honestly rather than
performing any network access — so the interface is ready for a real,
licensed implementation later without touching the valuation engine.
"""
from __future__ import annotations
import csv
import io
import re
from datetime import date, datetime
from typing import List, Optional

from auction_models import AuctionComparable, PriceSemantics, SourceStatus, parse_auction_date


class AuctionSourceAdapter:
    """Common interface every source (manual, CSV, or a future licensed
    automated adapter) must implement. The valuation engine only ever talks
    to this interface — it never contains source-specific code."""
    name = "base"
    status = SourceStatus.NOT_SUPPORTED

    def capabilities(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "automated": False,
            "requires_login": False,
            "requires_license": False,
        }

    def healthcheck(self) -> dict:
        return {"name": self.name, "ok": True, "status": self.status.value}

    def search(self, identity: dict, filters: Optional[dict] = None) -> List[AuctionComparable]:
        raise NotImplementedError

    def normalize(self, raw_record: dict) -> AuctionComparable:
        raise NotImplementedError


class ManualComparableAdapter(AuctionSourceAdapter):
    """Structured manual entry: Date | Hammer | Currency | Grade | Auction
    House | URL (spec §7), while still accepting the legacy one-number-per-
    line format so the existing simple textarea keeps working unchanged."""
    name = "manual"
    status = SourceStatus.ENABLED_MANUAL

    def capabilities(self) -> dict:
        d = super().capabilities()
        d["automated"] = False
        return d

    def parse_legacy_lines(self, text: str) -> List[AuctionComparable]:
        out = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^-?\d+(?:[.,]\d+)?$", line)
            if not m:
                continue
            price = float(line.replace(",", "."))
            if price <= 0:
                continue
            comp = AuctionComparable(
                source="manual",
                title="Manual realized comparable (legacy single-price entry)",
                hammer_price=price,
                price_semantics=PriceSemantics.HAMMER.value,
                sold=True,
            )
            comp.compute_dedupe_key()
            out.append(comp)
        return out

    def parse_structured_line(self, line: str, default_currency: str = "EUR") -> Optional[AuctionComparable]:
        if "|" not in line:
            return None
        parts = [p.strip() for p in line.split("|")]
        while len(parts) < 6:
            parts.append("")
        raw_date, raw_hammer, raw_currency, raw_grade, raw_house, raw_url = parts[:6]
        try:
            hammer = float(raw_hammer.replace(",", ".")) if raw_hammer else None
        except ValueError:
            hammer = None
        if hammer is None or hammer <= 0:
            return None
        adate = parse_auction_date(raw_date) if raw_date else None
        comp = AuctionComparable(
            source="manual",
            title=f"Manual realized comparable{(' — ' + raw_house) if raw_house else ''}",
            auction_house=raw_house or None,
            auction_date=adate.isoformat() if adate else None,
            source_url=raw_url or None,
            grade_raw=raw_grade or None,
            hammer_price=hammer,
            currency=(raw_currency or default_currency).upper(),
            price_semantics=PriceSemantics.HAMMER.value,
            sold=True,
        )
        comp.compute_dedupe_key()
        return comp

    def parse(self, text: str, default_currency: str = "EUR") -> List[AuctionComparable]:
        out = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                comp = self.parse_structured_line(line, default_currency)
                if comp:
                    out.append(comp)
            else:
                out.extend(self.parse_legacy_lines(line))
        return out


class CSVComparableAdapter(AuctionSourceAdapter):
    """Structured CSV/XLSX-exported-as-CSV import."""
    name = "csv"
    status = SourceStatus.ENABLED_MANUAL

    REQUIRED = {"hammer"}
    KNOWN_COLUMNS = {"date", "hammer", "currency", "grade", "auction_house", "url",
                      "auction_name", "lot_number", "grading_company", "cert_number",
                      "title", "description", "country", "country_code", "currency_code",
                      "denomination", "denomination_value", "year", "coin_year", "issuer",
                      "mint", "mintmark", "variant"}

    def capabilities(self) -> dict:
        d = super().capabilities()
        d["automated"] = False
        return d

    def parse(self, csv_text: str, default_currency: str = "EUR") -> List[AuctionComparable]:
        reader = csv.DictReader(io.StringIO(csv_text))
        if reader.fieldnames is None:
            return []
        header = {h.strip().lower(): h for h in reader.fieldnames}
        missing = self.REQUIRED - set(header.keys())
        if missing:
            raise ValueError(f"CSV missing required column(s): {', '.join(sorted(missing))}")
        out = []
        for row in reader:
            def get(col):
                key = header.get(col)
                return (row.get(key) or "").strip() if key else ""
            raw_hammer = get("hammer")
            try:
                hammer = float(raw_hammer.replace(",", ".")) if raw_hammer else None
            except ValueError:
                hammer = None
            if hammer is None or hammer <= 0:
                continue
            adate = parse_auction_date(get("date")) if get("date") else None

            raw_denom = get("denomination_value") or get("denomination")
            denom_value = None
            if raw_denom:
                # BUG 12: preserve fractional numismatic denominations such as
                # 1/2, 1/4 and mixed values such as 2 1/2 during CSV import.
                raw_denom_clean = raw_denom.replace(",", ".").strip()
                if "/" in raw_denom_clean:
                    m_frac = re.search(r"(\d+)?\s*(\d+)/(\d+)", raw_denom_clean)
                    if m_frac:
                        wh = float(m_frac.group(1)) if m_frac.group(1) else 0.0
                        num = float(m_frac.group(2))
                        den = float(m_frac.group(3))
                        denom_value = wh + (num / den) if den > 0 else None
                if denom_value is None:
                    try:
                        denom_value = float(raw_denom_clean)
                    except ValueError:
                        denom_value = None

            raw_year = get("coin_year") or get("year")
            try:
                coin_year = int(raw_year) if raw_year else None
            except ValueError:
                coin_year = None
            comp = AuctionComparable(
                source="csv",
                title=get("title") or f"CSV import — {get('auction_house') or 'unknown house'}",
                description=get("description") or None,
                auction_house=get("auction_house") or None,
                auction_name=get("auction_name") or None,
                auction_date=adate.isoformat() if adate else None,
                lot_number=get("lot_number") or None,
                source_url=get("url") or None,
                country=get("country") or None,
                country_code=(get("country_code") or None),
                currency_code=(get("currency_code") or None),
                denomination_value=denom_value,
                coin_year=coin_year,
                issuer=get("issuer") or None,
                mint=get("mint") or None,
                mintmark=get("mintmark") or None,
                variant=get("variant") or None,
                grade_raw=get("grade") or None,
                grading_company=get("grading_company") or None,
                cert_number=get("cert_number") or None,
                hammer_price=hammer,
                currency=(get("currency") or default_currency).upper(),
                price_semantics=PriceSemantics.HAMMER.value,
                sold=True,
            )
            comp.compute_dedupe_key()
            out.append(comp)
        return out


class CoinArchivesAdapter(AuctionSourceAdapter):
    name = "coinarchives"
    status = SourceStatus.NOT_SUPPORTED

    def search(self, identity, filters=None):
        raise NotImplementedError(
            "CoinArchives automated access is disabled: their Terms of Service explicitly prohibit automated harvesting."
        )


class AcsearchAdapter(AuctionSourceAdapter):
    name = "acsearch"
    status = SourceStatus.NOT_SUPPORTED

    def search(self, identity, filters=None):
        raise NotImplementedError(
            "acsearch.info automated access is disabled: their Terms of Use explicitly forbid web scrapers/robots."
        )


class BiddrAdapter(AuctionSourceAdapter):
    name = "biddr"
    status = SourceStatus.UNDER_REVIEW

    def search(self, identity, filters=None):
        raise NotImplementedError("Biddr automated access is not enabled without explicit permission.")


class NumisBidsAdapter(AuctionSourceAdapter):
    name = "numisbids"
    status = SourceStatus.UNDER_REVIEW

    def search(self, identity, filters=None):
        raise NotImplementedError("NumisBids automated access is not enabled without explicit permission.")


def get_enabled_adapters() -> List[AuctionSourceAdapter]:
    all_adapters = [ManualComparableAdapter(), CSVComparableAdapter(),
                     CoinArchivesAdapter(), AcsearchAdapter(), BiddrAdapter(), NumisBidsAdapter()]
    return [a for a in all_adapters if a.status in (SourceStatus.ENABLED_AUTOMATIC, SourceStatus.ENABLED_MANUAL)]
