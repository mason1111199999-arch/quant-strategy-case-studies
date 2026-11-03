#!/usr/bin/env python3
"""V1.4 company-specific valuation-gated core-moat rotation backtest.

This is a transparent, point-in-time proxy implementation of the latest
rulebook.  It keeps the user's allocation rule: one qualifying name is 100%;
multiple qualifying names are equal-weighted.  The important change from the
older runner is that a high fundamental score cannot override an expensive
price: new buys must pass both an absolute-value gate and a company-category
price/FV ceiling.

The SEC/Yahoo feeds do not expose every bespoke measure in the written
rulebook.  Therefore this runner uses filing-date annual fundamentals plus
adjusted prices, and records the exact valuation proxy in each signal row.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date

import backtest_bluechip_strategy_10y as base
import backtest_bluechip_strategy_v11 as v11


ROOT = base.ROOT
OUT = ROOT / "reports/bluechip-strategy-10y-v14"
START, END, INITIAL, COST_BPS = base.START, base.END, base.INITIAL, base.COST_BPS
CHART_VERSION = "V1.4"

# Yahoo symbols.  The portfolio is intentionally the 13-name core pool agreed
# in the conversation; CPRT and the rate-sensitive utility candidates remain
# excluded.
TICKERS = [
    "PG", "V", "MCD", "WM", "KO", "COST", "BRK-B",
    "MA", "CNI", "SPGI", "TSM", "ASML", "AAPL",
]

# SEC CIKs for the six additions plus Berkshire's Yahoo/SEC mapping.
EXTRA_CIKS = {
    "MA": ("0001141391", "USD", 0.10),
    "CNI": ("0000016868", "CAD", 0.04),
    "SPGI": ("0000064040", "USD", 0.07),
    "TSM": ("0001046179", "USD", 0.10),
    "ASML": ("0000937966", "EUR", 0.08),
    "AAPL": ("0000320193", "USD", 0.07),
    "BRK-B": ("0001067983", "USD", 0.06),
}

CATEGORY = {
    "PG": "consumer", "KO": "consumer", "MCD": "consumer", "COST": "consumer",
    "V": "payments", "MA": "payments", "SPGI": "information",
    "WM": "infrastructure", "CNI": "rail",
    "TSM": "semiconductor", "ASML": "semiconductor", "AAPL": "platform",
    "BRK-B": "conglomerate",
}

# These are scoring bands, not portfolio weights.  The thresholds are
# deliberately company/industry-specific rather than one universal hurdle.
GROWTH_BANDS = {
    "PG": [(0.04, 15), (0.02, 10), (-0.02, 5)],
    "KO": [(0.03, 15), (0.01, 10), (-0.02, 5)],
    "MCD": [(0.04, 15), (0.02, 10), (-0.02, 5)],
    "COST": [(0.07, 15), (0.04, 10), (0.00, 5)],
    "V": [(0.10, 15), (0.06, 10), (0.00, 5)],
    "MA": [(0.10, 15), (0.06, 10), (0.00, 5)],
    "SPGI": [(0.07, 15), (0.04, 10), (0.00, 5)],
    "WM": [(0.04, 15), (0.02, 10), (-0.01, 5)],
    "CNI": [(0.04, 15), (0.02, 10), (-0.01, 5)],
    "TSM": [(0.10, 15), (0.06, 10), (0.00, 5)],
    "ASML": [(0.08, 15), (0.05, 10), (0.00, 5)],
    "AAPL": [(0.08, 15), (0.04, 10), (0.00, 5)],
}

FCF_BANDS = {
    "PG": [(1.00, 15), (0.80, 10), (0.60, 5)],
    "KO": [(0.80, 15), (0.60, 10), (0.40, 5)],
    "MCD": [(0.80, 15), (0.60, 10), (0.40, 5)],
    "COST": [(1.00, 15), (0.80, 10), (0.60, 5)],
    "V": [(0.90, 15), (0.70, 10), (0.50, 5)],
    "MA": [(0.90, 15), (0.70, 10), (0.50, 5)],
    "SPGI": [(0.90, 15), (0.70, 10), (0.50, 5)],
    "WM": [(0.80, 15), (0.60, 10), (0.40, 5)],
    "CNI": [(0.80, 15), (0.60, 10), (0.40, 5)],
    "TSM": [(0.90, 15), (0.70, 10), (0.50, 5)],
    "ASML": [(0.90, 15), (0.70, 10), (0.50, 5)],
    "AAPL": [(0.90, 15), (0.70, 10), (0.50, 5)],
}

# Company-specific growth caps used only by the intrinsic-value model.
GROWTH_CAPS = {ticker: spec[2] for ticker, spec in EXTRA_CIKS.items()}

# (good, acceptable, weak) net-debt/operating-income proxy.  Q4 is only a
# risk-quality component; it is not allowed to rescue a failed valuation gate.
LEVERAGE_BANDS = {
    "PG": (2.0, 3.0, 4.0), "KO": (2.5, 3.5, 4.5), "MCD": (4.0, 5.0, 6.0),
    "COST": (0.5, 1.5, 2.5), "V": (1.0, 2.0, 3.0), "MA": (1.0, 2.0, 3.0),
    "SPGI": (2.0, 3.0, 4.0), "WM": (3.0, 3.8, 4.5), "CNI": (2.5, 3.3, 4.0),
    "TSM": (0.5, 1.5, 2.5), "ASML": (0.5, 1.5, 2.5), "AAPL": (0.0, 0.5, 1.5),
}

# Category-specific discount-rate floors/spreads.  This is the macro input:
# rising Treasury yields lower FV, especially for long-duration technology.
RATE_RULES = {
    "consumer": (0.085, 0.045), "payments": (0.090, 0.050),
    "information": (0.090, 0.050), "infrastructure": (0.085, 0.045),
    "rail": (0.085, 0.045), "platform": (0.090, 0.050),
    "semiconductor": (0.100, 0.055), "conglomerate": (0.085, 0.045),
}

# Stable FCF/share valuation anchors, used as a cross-check alongside DCF.
# Tech uses cycle-adjusted FCF and a multiple anchor more heavily to avoid
# buying a semiconductor peak as if it were permanent.
TARGET_MULTIPLE = {
    "PG": 22.0, "KO": 24.0, "MCD": 25.0, "COST": 35.0,
    "V": 28.0, "MA": 30.0, "SPGI": 28.0, "WM": 25.0, "CNI": 23.0,
    "TSM": 22.0, "ASML": 28.0, "AAPL": 25.0,
}
TARGET_FCF_YIELD = {"WM": 0.045, "CNI": 0.045}

# SEC's machine-readable data uses IFRS namespaces for TSM and some foreign
# issuers, while the older runner only searched US-GAAP.  These aliases keep
# the extension explicit and still prefer USD units when the issuer supplies
# both reporting currency and USD presentation.
IFRS_ALIASES = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "operating_income": ["ProfitLossFromOperatingActivities"],
    "net_income": ["ProfitLoss", "ProfitLossAttributableToOwnersOfParent"],
    "cfo": ["CashFlowsFromUsedInOperatingActivities", "CashFlowsFromUsedInOperations"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
    "debt": ["LongtermBorrowings", "ShorttermBorrowings", "CurrentPortionOfLongtermBorrowings"],
    "cash": ["CashAndCashEquivalents"],
    "eps": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"],
    "shares": ["AdjustedWeightedAverageShares", "WeightedAverageShares"],
    "equity": ["Equity", "EquityAttributableToOwnersOfParent"],
}

# TSM supplies the same IFRS facts in both local TWD and USD in its 20-F.  Use
# the USD-presented primary tag only; mixing tags by fiscal year would create
# artificial revenue jumps and an invalid FCF/share history.
TSM_ALIASES = {
    "revenue": ["Revenue"],
    "operating_income": ["ProfitLossFromOperatingActivities"],
    "net_income": ["ProfitLoss"],
    "cfo": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
    "debt": ["LongtermBorrowings"],
    "cash": ["CashAndCashEquivalents"],
    "eps": ["DilutedEarningsLossPerShare"],
    "shares": ["AdjustedWeightedAverageShares"],
    "equity": ["Equity"],
}

# Yahoo's V series is post-2015 split, but Visa's SEC share tag in this feed
# is a pre-split snapshot.  TSM's US ticker is a 5:1 ADR, whereas its USD
# financial facts are per ordinary share.
SHARE_SPLIT_ADJUSTMENT = {"V": 4.0}
ADR_MULTIPLIER = {"TSM": 5.0}


def _extended_records(facts: dict, aliases: list[str], annual: bool) -> list[dict]:
    """Read US-GAAP/IFRS/DEI facts with point-in-time filing filters."""
    rows = []
    namespaces = ("us-gaap", "ifrs-full", "dei")
    for tag in aliases:
        meta = None
        for namespace in namespaces:
            if facts.get("facts", {}).get(namespace, {}).get(tag):
                meta = facts["facts"][namespace][tag]
                break
        if not meta:
            continue
        units = meta.get("units", {})
        # When a foreign issuer presents both local currency and USD, keep USD
        # so that USD ADR prices are comparable.  Otherwise use its disclosed
        # reporting currency consistently for both numerator and denominator.
        unit_name = next((unit for unit in units if unit == "USD" or unit.startswith("USD/")), None) or next(iter(units), None)
        if unit_name is None:
            continue
        for item in units[unit_name]:
            if item.get("form") not in {"10-K", "20-F", "40-F", "6-K", "10-Q"}:
                continue
            if annual and item.get("fp") not in {"FY", None}:
                continue
            if not item.get("end") or not item.get("filed"):
                continue
            start = item.get("start")
            if annual and start:
                days = (date.fromisoformat(item["end"]) - date.fromisoformat(start)).days
                if days < 300 or days > 430:
                    continue
            value = float(item["val"])
            if "Acquire" in tag or "PurchaseOf" in tag:
                value = abs(value)
            rows.append({"end": item["end"], "filed": item["filed"], "val": value, "tag": tag, "unit": unit_name})
    if annual:
        by_end = {}
        for row in rows:
            if row["end"] not in by_end or row["filed"] > by_end[row["end"]]["filed"]:
                by_end[row["end"]] = row
        return sorted(by_end.values(), key=lambda x: (x["end"], x["filed"]))
    return sorted(rows, key=lambda x: (x["end"], x["filed"]))


def install_extended_sec_parser() -> None:
    """Extend the shared proxy parser only inside this V1.4 process."""
    for key, aliases in IFRS_ALIASES.items():
        base.FACT_ALIASES[key] = list(dict.fromkeys(base.FACT_ALIASES.get(key, []) + aliases))
    base.FACT_ALIASES["debt"] = list(dict.fromkeys(base.FACT_ALIASES["debt"] + ["LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"]))
    base.annual_records = lambda facts, aliases: _extended_records(facts, aliases, True)
    base.instant_records = lambda facts, aliases: _extended_records(facts, aliases, False)


def build_financials_v14(ticker: str, facts: dict) -> dict[str, list[dict]]:
    """Build the filing-date series, with TSM on a consistent IFRS/USD tag."""
    if ticker == "TSM":
        aliases = dict(TSM_ALIASES)
    else:
        aliases = dict(base.FACT_ALIASES)
        aliases["equity"] = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
    output = {}
    for key, names in aliases.items():
        annual = key in {"revenue", "operating_income", "net_income", "cfo", "capex", "eps", "shares", "dividend_per_share", "equity"}
        output[key] = _extended_records(facts, names, annual)
    return output

# New buys: primary is the preferred valuation zone; reserve is an acceptable
# but less attractive zone.  The backtest includes both zones if they qualify,
# because the user's rule says all qualifying names are held equally.
PRIMARY_RATIO = {
    "consumer": 1.10, "payments": 1.15, "information": 1.15,
    "infrastructure": 1.10, "rail": 1.10, "platform": 1.15,
    "semiconductor": 1.15, "conglomerate": 1.15,
}
RESERVE_RATIO = {
    "consumer": 1.20, "payments": 1.25, "information": 1.25,
    "infrastructure": 1.20, "rail": 1.20, "platform": 1.25,
    "semiconductor": 1.25, "conglomerate": 1.25,
}


def ratio_score(ratio: float | None) -> int | None:
    if ratio is None or not math.isfinite(ratio):
        return None
    return 20 if ratio <= 0.90 else 15 if ratio <= 1.05 else 10 if ratio <= 1.15 else 5 if ratio <= 1.25 else 0


def percentile_score(multiple: float | None, history: list[float]) -> tuple[int | None, float | None]:
    if multiple is None:
        return None, None
    if len(history) < 8:
        # Neutral score while the strategy builds its own history.  This avoids
        # an artificial two-year cash period caused solely by a short sample.
        return 10, None
    below = sum(x < multiple for x in history)
    equal = sum(x == multiple for x in history)
    percentile = (below + 0.5 * equal) / len(history)
    score = 20 if percentile <= 0.30 else 15 if percentile <= 0.50 else 10 if percentile <= 0.70 else 5 if percentile <= 0.90 else 0
    return score, percentile


def cycle_fcf_per_share(ticker: str, years: list[dict], fin: dict[str, list[dict]], when: str, tech: bool) -> tuple[float | None, list[float]]:
    snapshot = v11.latest_by_tag(fin.get("shares", []), when)
    shares = next(iter(snapshot.values()), {}).get("val") if snapshot else None
    values = []
    sample = years[-5:] if tech else years[-3:]
    for year in sample:
        # Prefer annual diluted shares; if the issuer does not provide it,
        # infer shares from net income/EPS before falling back to DEI.  This
        # fixes Visa's stale pre-split DEI snapshot without using future data.
        inferred = None
        if year.get("net_income") and year.get("eps") and year["eps"] > 0:
            inferred = year["net_income"] / year["eps"]
        share_count = year.get("shares") or inferred or shares
        if share_count and share_count < 1_000_000:
            share_count *= 1_000_000
        if ticker in SHARE_SPLIT_ADJUSTMENT and share_count:
            share_count *= SHARE_SPLIT_ADJUSTMENT[ticker]
        if share_count and year.get("cfo") is not None and year.get("capex") is not None:
            value = (year["cfo"] - year["capex"]) / share_count
            if math.isfinite(value) and value > 0:
                values.append(value * ADR_MULTIPLIER.get(ticker, 1.0))
    if not values:
        return None, values
    if tech:
        return sorted(values)[len(values) // 2], values
    return v11.latest_and_median(values), values


def blended_fv(ticker: str, category: str, c0: float | None, growth: float | None, tnx: float, price: float, book_value_per_share: float | None) -> tuple[float | None, str]:
    if c0 is None or growth is None:
        return None, "missing_owner_earnings"
    floor, spread = RATE_RULES[category]
    r = max(floor, tnx + spread)
    cap = GROWTH_CAPS.get(ticker, 0.06)
    dcf = v11.dcf_value(c0, growth, cap, r)
    if dcf is None:
        return None, "dcf_invalid"
    if ticker == "BRK-B":
        owner = dcf
        pb_anchor = book_value_per_share * 1.35 if book_value_per_share and book_value_per_share > 0 else None
        if pb_anchor:
            return 0.50 * owner + 0.50 * pb_anchor, "50% owner-earnings DCF + 50% P/B anchor"
        return owner, "owner-earnings DCF"
    if category in {"infrastructure", "rail"}:
        yield_anchor = c0 / TARGET_FCF_YIELD.get(ticker, 0.045)
        return 0.50 * dcf + 0.50 * yield_anchor, "50% DCF + 50% FCF-yield anchor"
    if category == "semiconductor":
        multiple_anchor = c0 * TARGET_MULTIPLE[ticker]
        return 0.40 * dcf + 0.60 * multiple_anchor, "40% DCF + 60% cycle-adjusted FCF multiple"
    if category in {"payments", "information", "platform"}:
        multiple_anchor = c0 * TARGET_MULTIPLE[ticker]
        return 0.50 * dcf + 0.50 * multiple_anchor, "50% DCF + 50% FCF multiple"
    multiple_anchor = c0 * TARGET_MULTIPLE[ticker]
    return 0.60 * dcf + 0.40 * multiple_anchor, "60% DCF + 40% FCF multiple"


def score_berkshire(fin: dict[str, list[dict]], when: str, price: float, tnx: float, history: list[float]) -> dict:
    years = v11.base.merge_annual(fin, when)
    if len(years) < 4:
        return {"eligible": False, "hold": False, "reason": "insufficient_reported_annual_history"}
    revenues = [y.get("revenue") for y in years if y.get("revenue") and y["revenue"] > 0]
    growth = base.cagr(revenues[-1], revenues[-4]) if len(revenues) >= 4 else None
    share_equiv = 1_450_000_000.0
    fcf_values = [(y["cfo"] - y["capex"]) / share_equiv for y in years[-5:] if y.get("cfo") is not None and y.get("capex") is not None]
    c0 = sorted([v for v in fcf_values if v > 0])[len([v for v in fcf_values if v > 0]) // 2] if any(v > 0 for v in fcf_values) else None
    q1 = 20
    q2 = base.score_band(growth, [(0.05, 15), (0.02, 10), (-0.02, 5)])
    q3 = 15 if sum(v > 0 for v in fcf_values) >= 4 else 10 if sum(v > 0 for v in fcf_values) >= 3 else 5
    q4 = 10
    multiple = price / c0 if c0 and price > 0 else None
    E, percentile = percentile_score(multiple, history)
    equity = v11.latest_by_tag(fin.get("equity", []), when)
    book_value = next(iter(equity.values()), {}).get("val") / share_equiv if equity else None
    FV, fv_method = blended_fv("BRK-B", "conglomerate", c0, growth, tnx, price, book_value)
    ratio = price / FV if FV and price > 0 else None
    A = ratio_score(ratio)
    Q = q1 + q2 + q3 + q4
    S = Q + (E or 0) + (A or 0) if E is not None and A is not None else None
    primary = bool(S is not None and Q >= 40 and S >= 65 and A is not None and A >= 10 and ratio is not None and ratio <= PRIMARY_RATIO["conglomerate"])
    reserve = bool(S is not None and Q >= 40 and S >= 60 and E is not None and E >= 10 and A is not None and A >= 5 and ratio is not None and ratio <= RESERVE_RATIO["conglomerate"])
    eligible = primary or reserve
    hold = bool(Q >= 35 and S is not None and S >= 55 and A is not None and A >= 5 and ratio is not None and ratio <= 1.35)
    return {"eligible": eligible, "primary": primary, "reserve": reserve, "hold": hold, "reason": "primary" if primary else "reserve" if reserve else "valuation_or_quality_gate", "Q": Q, "q1": q1, "q2": q2, "q3": q3, "q4": q4, "E": E, "A": A, "S": S, "leverage": -0.5, "growth": growth, "c0": c0, "FV": FV, "multiple": multiple, "percentile": percentile, "price_fv": ratio, "fv_method": fv_method, "metric": "owner earnings + book value", "category": "conglomerate"}


def score_company(ticker: str, fin: dict[str, list[dict]], when: str, price: float, tnx: float, history: list[float]) -> dict:
    if ticker == "BRK-B":
        return score_berkshire(fin, when, price, tnx, history)
    years = base.merge_annual(fin, when)
    category = CATEGORY[ticker]
    if len(years) < 4:
        return {"eligible": False, "hold": False, "reason": "insufficient_reported_annual_history", "category": category}
    q1, margin_delta = v11.margin_scores(years)
    revenues = [y.get("revenue") for y in years if y.get("revenue") and y["revenue"] > 0]
    growth = base.cagr(revenues[-1], revenues[-4]) if len(revenues) >= 4 else None
    q2 = base.score_band(growth, GROWTH_BANDS[ticker]) if growth is not None else 0
    fcf_total = sum(y.get("cfo", 0) - y.get("capex", 0) for y in years[-3:] if y.get("cfo") is not None and y.get("capex") is not None)
    ni_total = sum(y.get("net_income", 0) for y in years[-3:])
    cash_conversion = fcf_total / ni_total if ni_total > 0 else None
    q3 = base.score_band(cash_conversion, FCF_BANDS[ticker]) if cash_conversion is not None else 0
    debt = v11.balance_total(fin.get("debt", []), when)
    cash = v11.balance_total(fin.get("cash", []), when)
    leverage = None
    if debt is not None and cash is not None and years[-1].get("operating_income"):
        leverage = (debt - cash) / max(years[-1]["operating_income"], 1.0)
    q4 = 0
    if leverage is not None:
        good, acceptable, weak = LEVERAGE_BANDS[ticker]
        q4 = 10 if leverage <= good else 7 if leverage <= acceptable else 3 if leverage <= weak else 0
    Q = q1 + q2 + q3 + q4
    tech = category == "semiconductor"
    c0, fcf_history = cycle_fcf_per_share(ticker, years, fin, when, tech)
    multiple = price / c0 if c0 and price > 0 else None
    E, percentile = percentile_score(multiple, history)
    fv, fv_method = blended_fv(ticker, category, c0, growth, tnx, price, None)
    ratio = price / fv if fv and price > 0 else None
    A = ratio_score(ratio)
    S = Q + E + A if E is not None and A is not None else None
    macro_status = "green" if tnx < 0.045 else "yellow" if tnx < 0.060 else "red"
    primary_ratio = PRIMARY_RATIO[category]
    reserve_ratio = RESERVE_RATIO[category]
    primary = bool(Q >= 40 and S is not None and S >= 65 and A is not None and A >= 10 and ratio is not None and ratio <= primary_ratio and q4 >= 3 and leverage is not None)
    if tech and tnx >= 0.055 and (A or 0) < 15:
        primary = False
    reserve = bool(Q >= 40 and S is not None and S >= 60 and E is not None and E >= 10 and A is not None and A >= 5 and ratio is not None and ratio <= reserve_ratio and q4 >= 3 and leverage is not None)
    eligible = primary or reserve
    hold = bool(Q >= 35 and S is not None and S >= 55 and A is not None and A >= 5 and ratio is not None and ratio <= min(1.35, reserve_ratio + 0.10) and q4 >= 3 and leverage is not None)
    if primary:
        reason = "primary"
    elif reserve:
        reason = "reserve"
    elif not leverage or q4 == 0:
        reason = "risk_gate"
    elif ratio is None:
        reason = "valuation_missing"
    elif ratio > reserve_ratio:
        reason = "price_above_reserve_ceiling"
    else:
        reason = "quality_or_score_gate"
    return {"eligible": eligible, "primary": primary, "reserve": reserve, "hold": hold, "reason": reason, "Q": Q, "q1": q1, "q2": q2, "q3": q3, "q4": q4, "E": E, "A": A, "S": S, "leverage": leverage, "growth": growth, "margin_delta": margin_delta, "c0": c0, "fcf_history": fcf_history, "FV": fv, "multiple": multiple, "percentile": percentile, "price_fv": ratio, "fv_method": fv_method, "metric": "cycle-adjusted FCF/share" if tech else "FCF/share", "category": category, "macro_status": macro_status}


def target_weights(current: dict[str, float], scores: dict[str, dict], fail_counts: dict[str, int], extreme_counts: dict[str, int]) -> tuple[dict[str, float], set[str], set[str]]:
    # All names passing either primary or reserve are held; one name therefore
    # gets 100%, while two or more are equal-weighted as requested.
    buy = {ticker for ticker, score in scores.items() if score.get("eligible")}
    retained = set()
    for ticker in current:
        score = scores[ticker]
        extreme = score.get("E") == 0 and score.get("A") == 0
        extreme_counts[ticker] = extreme_counts.get(ticker, 0) + 1 if extreme else 0
        if extreme_counts[ticker] >= 3:
            continue
        fail = not score.get("hold", False)
        fail_counts[ticker] = fail_counts.get(ticker, 0) + 1 if fail else 0
        if fail_counts[ticker] < 2:
            retained.add(ticker)
    active = buy | retained
    frozen = {ticker for ticker in current if scores[ticker].get("q4", 0) == 0}
    frozen_weights = {ticker: current[ticker] for ticker in frozen}
    remaining = max(0.0, 1.0 - sum(frozen_weights.values()))
    live = active - frozen
    target = dict(frozen_weights)
    if live:
        for ticker in live:
            target[ticker] = remaining / len(live)
    return target, active, frozen


def daily_metrics(dates, nav, daily_returns, exposure_weights):
    return base.metrics(dates, nav, daily_returns, [set(weights) for weights in exposure_weights])


def draw_chart_v14(rows: list[dict], path) -> None:
    """Render a correctly labelled PNG for the 13-stock comparison."""
    width, height = 1600, 900
    img = base.Image.new("RGB", (width, height), "#F8FAFB")
    draw = base.ImageDraw.Draw(img)
    left, top, right, bottom = 100, 80, 1510, 790
    all_values = [float(r["strategy_value"]) for r in rows] + [float(r["equal_weight_value"]) for r in rows] + [float(r["spy_value"]) for r in rows]
    lo, hi = max(0, min(all_values) * 0.95), max(all_values)
    def xy(i: int, value: float) -> tuple[float, float]:
        x = left + (right - left) * i / (len(rows) - 1)
        y = bottom - (bottom - top) * (value - lo) / (hi - lo)
        return x, y
    for frac in [0, .25, .5, .75, 1]:
        y = bottom - (bottom - top) * frac
        draw.line((left, y, right, y), fill="#DDE5E8", width=1)
        draw.text((15, y - 8), f"${lo + (hi - lo) * frac:,.0f}", fill="#52636B")
    for idx in [0, len(rows) // 4, len(rows) // 2, 3 * len(rows) // 4, len(rows) - 1]:
        x, _ = xy(idx, lo)
        draw.line((x, top, x, bottom), fill="#EEF2F3", width=1)
        draw.text((x - 35, bottom + 16), rows[idx]["date"][:7], fill="#52636B")
    colors = [("strategy_value", "#147D80", f"{CHART_VERSION} Strategy"), ("equal_weight_value", "#183345", "13-stock EW B&H"), ("spy_value", "#C57C1F", "SPY B&H")]
    for key, color, _ in colors:
        draw.line([xy(i, float(r[key])) for i, r in enumerate(rows)], fill=color, width=4)
    draw.text((left, 20), f"{CHART_VERSION} company-specific valuation-gated backtest | Initial $10,000", fill="#183345")
    lx = right - 390
    for j, (_, color, label) in enumerate(colors):
        draw.line((lx + j * 130, 45, lx + j * 130 + 20, 45), fill=color, width=4)
        draw.text((lx + j * 130 + 26, 37), label, fill="#263642")
    draw.text((left, bottom + 55), "Yahoo Finance adjusted close | SEC Company Facts at filing date | proxy implementation", fill="#62717B")
    img.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    install_extended_sec_parser()
    for ticker, spec in EXTRA_CIKS.items():
        base.TICKERS[ticker] = spec
    v11.TICKERS = TICKERS
    v11.GROWTH_CAPS.update(GROWTH_CAPS)
    prices = {ticker: base.yahoo_chart(ticker) for ticker in TICKERS + ["SPY", "^TNX"]}
    facts = {ticker: build_financials_v14(ticker, base.sec_facts(base.TICKERS[ticker][0])) for ticker in TICKERS}
    dates = sorted(day for day in prices["SPY"] if START <= day <= END and all(day in prices[ticker] for ticker in TICKERS))
    month_ends = base.month_end_dates(dates)
    history = defaultdict(list)
    current: dict[str, float] = {}
    fail_counts, extreme_counts = {}, {}
    signal_at, signal_rows = {}, []
    for day in sorted(month_ends):
        tnx = prices["^TNX"].get(day, {}).get("close", 3.5) / 100
        scores = {ticker: score_company(ticker, facts[ticker], day, prices[ticker][day]["close"], tnx, history[ticker]) for ticker in TICKERS}
        for ticker in TICKERS:
            if scores[ticker].get("multiple") is not None and date.fromisoformat(day).month in {3, 6, 9, 12}:
                history[ticker].append(scores[ticker]["multiple"])
        target, active, frozen = target_weights(current, scores, fail_counts, extreme_counts)
        execution = base.first_next(dates, day)
        signal_at[execution] = {"signal_date": day, "target": target, "active": active, "frozen": frozen} if execution else {"signal_date": day, "target": target, "active": active, "frozen": frozen}
        signal_rows.append({"signal_date": day, "execution_date": execution or "", "primary": ";".join(sorted(t for t in TICKERS if scores[t].get("primary"))), "reserve": ";".join(sorted(t for t in TICKERS if scores[t].get("reserve"))), "selected": ";".join(sorted(active)), "count_primary": sum(scores[t].get("primary", False) for t in TICKERS), "count_reserve": sum(scores[t].get("reserve", False) for t in TICKERS), "count_selected": len(active), "frozen": ";".join(sorted(frozen)), "detail": json.dumps(scores, ensure_ascii=False)})
        current = target

    nav = INITIAL
    strategy_nav, strategy_returns, exposure_weights, events = [], [], [], []
    weights: dict[str, float] = {}
    previous = {ticker: prices[ticker][dates[0]]["adjclose"] for ticker in TICKERS}
    for index, day in enumerate(dates):
        if index:
            daily_return = sum(weight * (prices[ticker][day]["adjclose"] / previous[ticker] - 1) for ticker, weight in weights.items())
            nav *= 1 + daily_return
            strategy_returns.append(daily_return)
        if day in signal_at:
            target = signal_at[day]["target"]
            turnover = sum(abs(target.get(ticker, 0) - weights.get(ticker, 0)) for ticker in TICKERS)
            cost = turnover * COST_BPS / 10000
            if target != weights:
                nav *= 1 - cost
                events.append({"signal_date": signal_at[day]["signal_date"], "execution_date": day, "old": ";".join(sorted(weights)), "new": ";".join(sorted(target)), "old_weights": json.dumps(weights, ensure_ascii=False), "new_weights": json.dumps(target, ensure_ascii=False), "turnover": turnover, "cost": cost, "active": ";".join(sorted(signal_at[day]["active"])), "frozen": ";".join(sorted(signal_at[day]["frozen"]))})
            weights = target
        strategy_nav.append(nav)
        exposure_weights.append(dict(weights))
        previous = {ticker: prices[ticker][day]["adjclose"] for ticker in TICKERS}

    equal_nav, spy_nav = INITIAL, INITIAL
    equal_series, spy_series = [], []
    prev_eq = {ticker: prices[ticker][dates[0]]["adjclose"] for ticker in TICKERS}
    prev_spy = prices["SPY"][dates[0]]["adjclose"]
    for day in dates:
        if day != dates[0]:
            equal_nav *= 1 + sum(prices[ticker][day]["adjclose"] / prev_eq[ticker] - 1 for ticker in TICKERS) / len(TICKERS)
            spy_nav *= prices["SPY"][day]["adjclose"] / prev_spy
        equal_series.append(equal_nav); spy_series.append(spy_nav)
        prev_eq = {ticker: prices[ticker][day]["adjclose"] for ticker in TICKERS}; prev_spy = prices["SPY"][day]["adjclose"]
    series = [{"date": day, "strategy_value": round(strategy_nav[i], 6), "equal_weight_value": round(equal_series[i], 6), "spy_value": round(spy_series[i], 6), "active_count": len(exposure_weights[i]), "active": ";".join(sorted(exposure_weights[i]))} for i, day in enumerate(dates)]
    strategy_stats = daily_metrics(dates, strategy_nav, strategy_returns, exposure_weights)
    equal_stats = daily_metrics(dates, equal_series, [equal_series[i] / equal_series[i - 1] - 1 for i in range(1, len(equal_series))], [dict.fromkeys(TICKERS, 1 / len(TICKERS))] * len(equal_series))
    spy_stats = daily_metrics(dates, spy_series, [spy_series[i] / spy_series[i - 1] - 1 for i in range(1, len(spy_series))], [{"SPY": 1}] * len(spy_series))
    summary = {
        "config": {"version": "V1.4-company-specific-valuation-gated", "start": START, "end": END, "initial_usd": INITIAL, "universe": "13-name core moat rotation pool; CPRT excluded", "buy_rule": "Q>=40, S>=60, E>=10, A>=5 and price/FV<=category reserve ceiling; primary zone uses Q>=40, S>=65, A>=10 and tighter ceiling", "valuation_gate": "All qualifying primary+reserve names are held; one name=100%, multiple names=equal weight; price/FV is a hard new-buy gate", "hold_rule": "Q>=35, S>=55, A>=5 and price/FV<=1.35 or category reserve+10%; two monthly failures to exit", "review": "month_end signal, next trading day execution", "cost_bps_per_side": COST_BPS, "cash_return": 0, "fundamental_proxy": True},
        "data": {"tickers": TICKERS, "trading_days": len(dates), "start": dates[0], "end": dates[-1], "price_source": "Yahoo Finance adjusted close", "fundamental_source": "SEC Company Facts at filing date", "yield_source": "Yahoo ^TNX proxy"},
        "strategy": strategy_stats, "equal_weight_buy_hold": equal_stats, "spy_buy_hold": spy_stats,
        "difference": {"vs_equal_cagr_pct_points": strategy_stats["cagr_pct"] - equal_stats["cagr_pct"], "vs_spy_cagr_pct_points": strategy_stats["cagr_pct"] - spy_stats["cagr_pct"], "vs_equal_max_drawdown_pct_points": strategy_stats["max_drawdown_pct"] - equal_stats["max_drawdown_pct"]},
        "events": events, "signal_rows": signal_rows, "series": series,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for filename, rows in [("equity_curve.csv", series), ("monthly_signals.csv", signal_rows), ("rebalance_log.csv", events)]:
        fields = list(rows[0]) if rows else ["signal_date", "execution_date"]
        with (OUT / filename).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    draw_chart_v14(series, OUT / "equity_curve.png")
    def row(label, stat):
        return f"| {label} | ${stat['ending_value']:,.2f} | {stat['total_return_pct']:.2f}% | {stat['cagr_pct']:.2f}% | {stat['annualized_vol_pct']:.2f}% | {stat['max_drawdown_pct']:.2f}% | {stat['sharpe_0rf']:.2f} |"
    report = ["# 永续蓝筹策略V1.4：13股专属估值门槛回测", "", f"区间：{dates[0]}至{dates[-1]}；起始资金：${INITIAL:,.0f}。", "", "## 回测结果", "", "| 组合 | 期末资产 | 累计收益 | 年化收益 | 年化波动 | 最大回撤 | 夏普（0无风险） |", "|---|---:|---:|---:|---:|---:|---:|", row("V1.4核心轮动", strategy_stats), row("13股等权买入持有", equal_stats), row("SPY买入持有", spy_stats), "", f"相对13股等权买入持有：年化收益差 {summary['difference']['vs_equal_cagr_pct_points']:+.2f}个百分点，最大回撤差 {summary['difference']['vs_equal_max_drawdown_pct_points']:+.2f}个百分点。", f"平均活跃标的 {strategy_stats['avg_names']:.2f}只，持仓交易日占比 {strategy_stats['invested_days_pct']:.1f}%，调仓事件 {len(events)}次。", "", "## 估值模型", "", "消费：60% FCF DCF + 40% FCF倍数；支付/信息服务/平台：50% DCF + 50% FCF倍数；WM/CNI：50% DCF + 50% FCF收益率；TSM/ASML：40% DCF + 60%周期调整后FCF倍数；BRK.B：50%所有者收益DCF + 50%P/B锚。", "估值分数A只是结果，价格/FV上限是硬门槛；基本面高分不能覆盖明显高估。所有通过主区或储备区的公司都进入组合，按一只100%、多只等权。", "", "## 数据边界", "", "财务数据使用决策日前已提交的SEC Company Facts，价格使用Yahoo复权收盘价，分红计入，现金收益按0%，单边换手成本5bp。CNI、TSM、ASML等跨市场披露存在币种/口径代理，非标准业务指标也用财务代理，故这是可复核的近似回测，不是审计级历史重演。", "", "输出：summary.json、equity_curve.csv、monthly_signals.csv、rebalance_log.csv、equity_curve.png。"]
    (OUT / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"strategy": strategy_stats, "equal_weight_buy_hold": equal_stats, "spy_buy_hold": spy_stats, "events": len(events), "signals": len(signal_rows), "output": str(OUT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
