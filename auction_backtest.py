"""
CoinBids Auction Intelligence 3.0 — backtesting framework (Phase 11).

WHAT THIS IS: a real, runnable tool for measuring how well
auction_valuation.py's Fair Value estimates would have predicted actual
realized prices, given a genuine historical dataset.

WHAT THIS IS NOT: a report of backtest RESULTS. No real historical CoinBids
auction dataset exists yet (see auction_source_matrix.md — no automated
source is enabled, and no manual archive has been built up over time). This
module has therefore never been run against real data, and this file
contains NO performance numbers, accuracy claims, or calibration results —
those would have to be invented, and this project's own rules (and Claude's)
prohibit fabricating auction/backtest data.

HOW TO ACTUALLY USE THIS, once real data exists:
    1. Build a dataset of HistoricalCase records — each one holds a set of
       "known-at-the-time" comparables (only sales that happened BEFORE the
       target sale date — never leak future information into a valuation)
       plus the actual realized price of a later sale of a comparable coin.
    2. Call run_backtest(cases) — it computes each case's valuation snapshot
       from only the "known-at-the-time" comparables, compares the predicted
       Fair Value range against the actual later realized price, and
       aggregates hit-rate / error metrics honestly.
    3. Inspect the returned BacktestReport — every number in it is computed
       from the cases you actually supplied. If you supply zero or synthetic
       cases, the report says so explicitly rather than presenting empty/
       fake results as if they were real findings.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Sequence

from auction_models import AuctionComparable
from auction_matching import classify_comparable
import auction_valuation as val


@dataclass
class HistoricalCase:
    """One backtestable case: the identity being valued, the set of
    comparables that were ALREADY KNOWN as of `as_of_date` (this is what
    prevents look-ahead bias — a comparable dated after as_of_date must not
    be included here), and the actual price the target coin (or an
    equivalent later sale) realized, used as ground truth."""
    case_id: str
    identity: dict
    identity_quality: float
    known_comparables: List[AuctionComparable]
    dealer_sample_prices: List[float]
    as_of_date: date
    actual_realized_price: float
    target_currency: str = "EUR"


@dataclass
class CaseResult:
    case_id: str
    predicted_low: Optional[float]
    predicted_central: Optional[float]
    predicted_high: Optional[float]
    confidence_label: Optional[str]
    actual: float
    within_range: Optional[bool]
    pct_error_vs_central: Optional[float]
    realized_comparable_count: int
    note: Optional[str] = None


@dataclass
class BacktestReport:
    n_cases: int
    n_scored: int
    hit_rate: Optional[float]              # fraction of scored cases where actual fell within [predicted_low, predicted_high]
    median_abs_pct_error: Optional[float]   # median of |actual - central| / actual
    mean_abs_pct_error: Optional[float]
    by_confidence_label: dict               # {label: {"n":..,"hit_rate":..,"median_abs_pct_error":..}}
    case_results: List[CaseResult]
    data_provenance_note: str
    is_synthetic_or_empty: bool


def run_backtest(cases: Sequence[HistoricalCase], data_provenance_note: str) -> BacktestReport:
    """`data_provenance_note` is REQUIRED and must describe where the cases
    actually came from (e.g. "n=0, no dataset available" or
    "internal CoinBids manually-logged realized sales, 2026-01 to 2026-08").
    This forces every caller to be explicit about provenance rather than the
    report silently looking authoritative regardless of input quality."""
    results: List[CaseResult] = []
    for case in cases:
        for c in case.known_comparables:
            classify_comparable(case.identity, c)
        snapshot = val.compute_valuation_snapshot(
            identity=case.identity, identity_quality=case.identity_quality,
            comparables=case.known_comparables, dealer_sample_prices=case.dealer_sample_prices,
            as_of=case.as_of_date, target_currency=case.target_currency,
        )
        fus = snapshot["fusion"]
        low, central, high = fus.get("fair_low"), fus.get("fair_central"), fus.get("fair_high")
        if central is None:
            results.append(CaseResult(case.case_id, low, central, high, snapshot["confidence"]["label"],
                                       case.actual_realized_price, None, None,
                                       snapshot["realized_market"]["count"],
                                       note="No fair-value estimate could be computed for this case."))
            continue
        within = (low is not None and high is not None and low <= case.actual_realized_price <= high)
        pct_err = abs(case.actual_realized_price - central) / case.actual_realized_price if case.actual_realized_price else None
        results.append(CaseResult(case.case_id, low, central, high, snapshot["confidence"]["label"],
                                   case.actual_realized_price, within, pct_err,
                                   snapshot["realized_market"]["count"]))

    scored = [r for r in results if r.within_range is not None]
    hit_rate = (sum(1 for r in scored if r.within_range) / len(scored)) if scored else None
    errs = sorted(r.pct_error_vs_central for r in scored if r.pct_error_vs_central is not None)
    median_err = errs[len(errs) // 2] if errs else None
    mean_err = (sum(errs) / len(errs)) if errs else None

    by_conf = {}
    for label in set(r.confidence_label for r in scored if r.confidence_label):
        subset = [r for r in scored if r.confidence_label == label]
        sub_errs = [r.pct_error_vs_central for r in subset if r.pct_error_vs_central is not None]
        by_conf[label] = {
            "n": len(subset),
            "hit_rate": (sum(1 for r in subset if r.within_range) / len(subset)) if subset else None,
            "median_abs_pct_error": sorted(sub_errs)[len(sub_errs) // 2] if sub_errs else None,
        }

    return BacktestReport(
        n_cases=len(cases), n_scored=len(scored), hit_rate=hit_rate,
        median_abs_pct_error=median_err, mean_abs_pct_error=mean_err,
        by_confidence_label=by_conf, case_results=results,
        data_provenance_note=data_provenance_note,
        is_synthetic_or_empty=(len(cases) == 0),
    )


def format_report(report: BacktestReport) -> str:
    lines = [
        "=== CoinBids Auction Intelligence — Backtest Report ===",
        f"Data provenance: {report.data_provenance_note}",
        f"Cases: {report.n_cases} supplied, {report.n_scored} produced a scoreable estimate.",
    ]
    if report.is_synthetic_or_empty:
        lines.append("NO CASES SUPPLIED — this report has no findings. Do not present an empty/synthetic "
                      "run as evidence of the engine's real-world accuracy.")
        return "\n".join(lines)
    if report.n_scored == 0:
        lines.append("No case produced a scoreable Fair Value estimate — nothing to report.")
        return "\n".join(lines)
    lines.append(f"Hit rate (actual within [fair_low, fair_high]): {report.hit_rate*100:.1f}%")
    lines.append(f"Median absolute %% error vs central estimate: {report.median_abs_pct_error*100:.1f}%")
    lines.append(f"Mean absolute %% error vs central estimate: {report.mean_abs_pct_error*100:.1f}%")
    lines.append("By confidence label:")
    for label, stats in sorted(report.by_confidence_label.items()):
        hr = f"{stats['hit_rate']*100:.1f}%" if stats["hit_rate"] is not None else "n/a"
        me = f"{stats['median_abs_pct_error']*100:.1f}%" if stats["median_abs_pct_error"] is not None else "n/a"
        lines.append(f"  {label}: n={stats['n']}, hit_rate={hr}, median_abs_pct_error={me}")
    return "\n".join(lines)
