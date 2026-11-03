#!/usr/bin/env python3
"""Point-in-time, data-available proxy backtest for the revised blue-chip rules.

The manual contains several company-specific fields that are not standardized
in a machine-readable historical feed (unit-case volume, rate-base growth,
payment volume, renewal rate, etc.).  This script therefore uses only facts
that can be reconstructed from SEC Company Facts at their filing dates:
operating margin stability, reported revenue growth, cash conversion or
dividend coverage, and balance-sheet cash-flow leverage.  It keeps the exact
portfolio decision rule: one eligible name is 100%; two or more eligible names
are equal weight; no eligible name is cash.  It is labelled a proxy, not the
final audited backtest of every bespoke manual metric.
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/bluechip-strategy-10y"
RAW = OUT / "raw"
START = "2016-09-16"
# The old report used a hard-coded end date, which allowed the dashboard's
# daily NAV extender to move forward while the monthly moat signals stayed
# frozen.  Keep a reproducible override for backtests, but default to the
# current calendar date so the completed Yahoo bar immediately before a
# weekend/holiday is included when the script is run after the close.
END = os.environ.get("BLUECHIP_END", date.today().isoformat())
INITIAL = 10_000.0
COST_BPS = 5.0
REBALANCE = "month_end_signal_next_trading_day"

TICKERS = {
    "PG": ("0000080424", "USD", 0.04),
    "V": ("0001403161", "USD", 0.08),
    "MCD": ("0000063908", "USD", 0.04),
    "CNI": ("0000016868", "CAD", 0.03),
    "DUK": ("0001326160", "USD", 0.07),
    "AWK": ("0001410636", "USD", 0.07),
    "WM": ("0000823768", "USD", 0.04),
    "SPGI": ("0000064040", "USD", 0.06),
    "KO": ("0000021344", "USD", 0.04),
    "CME": ("0001156375", "USD", 0.04),
    "COST": ("0000909832", "USD", 0.05),
}

# The weights are used only as company-specific growth caps in the proxy.
# Portfolio allocation itself is deliberately equal among currently eligible
# names, per the user's instruction.
GROWTH_CAPS = {k: v[2] for k, v in TICKERS.items()}

FACT_ALIASES = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "Revenues"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebtCurrent", "ShortTermBorrowings"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "eps": ["EarningsPerShareDiluted"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "EntityCommonStockSharesOutstanding"],
    "dividend_per_share": ["CommonStockDividendsPerShareDeclared"],
}


def fetch_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "Codex research backtest contact@example.com"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def cached_json(name: str, url: str, headers: dict[str, str] | None = None) -> dict:
    path = RAW / name
    refresh = os.environ.get("BLUECHIP_REFRESH_DATA", "0").lower() in {"1", "true", "yes", "on"}
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch_json(url, headers)
    path.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(0.2)
    return data


def yahoo_chart(symbol: str, period1: int = 1420070400, period2: int | None = None) -> dict[str, dict[str, float]]:
    # Yahoo's period2 is exclusive. A fixed endpoint silently freezes the
    # dashboard once that date is reached, even when the refresh job succeeds.
    if period2 is None:
        period2 = int(datetime.now(timezone.utc).timestamp()) + 86_400
    query = urllib.parse.urlencode({"period1": period1, "period2": period2, "interval": "1d", "events": "div,splits"})
    data = cached_json(f"yahoo_{symbol.replace('^', 'tnx')}.json", f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{query}", {"User-Agent": "Mozilla/5.0"})
    result = data["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    adj = result["indicators"].get("adjclose", [{}])[0].get("adjclose", quote["close"])
    rows: dict[str, dict[str, float]] = {}
    for ts, close, adjusted in zip(result["timestamp"], quote["close"], adj):
        if close is None or adjusted is None:
            continue
        day = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        rows[day] = {"close": float(close), "adjclose": float(adjusted)}
    return rows


def sec_facts(cik: str) -> dict:
    return cached_json(f"sec_{cik}.json", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", {"User-Agent": "Codex research backtest contact@example.com"})


def pick_tag(facts: dict, aliases: list[str]) -> tuple[str, dict] | None:
    facts_by_namespace = facts.get("facts", {})
    for alias in aliases:
        if alias in usgaap:
            return alias, usgaap[alias]
    return None


def signed_value(tag: str, unit: str, item: dict) -> float:
    value = float(item["val"])
    # SEC cash-flow outflows are often reported as negative values.  Normalize
    # capex to a positive use of cash while retaining other signs.
    if tag in {"PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"}:
        return abs(value)
    return value


def annual_records(facts: dict, aliases: list[str]) -> list[dict]:
    rows = []
    facts_by_namespace = facts.get("facts", {})
    for tag in aliases:
        meta = next((facts_by_namespace.get(namespace, {}).get(tag) for namespace in ("us-gaap", "dei") if facts_by_namespace.get(namespace, {}).get(tag)), None)
        if not meta:
            continue
        units = meta.get("units", {})
        unit_name = "USD" if "USD" in units else next((u for u in units if "/shares" in u), None)
        unit_name = unit_name or next(iter(units), None)
        if unit_name is None:
            continue
        for item in units[unit_name]:
            if item.get("form") not in {"10-K", "20-F", "40-F", "6-K"}:
                continue
            if item.get("fp") not in {"FY", None}:
                continue
            if not item.get("end") or not item.get("filed"):
                continue
            start = item.get("start")
            end = item["end"]
            if start:
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                if days < 300 or days > 430:
                    continue
            rows.append({"end": end, "filed": item["filed"], "val": signed_value(tag, unit_name, item), "tag": tag, "unit": unit_name})
    # One annual fact can be restated; keep the latest fact filed by date for
    # each fiscal end, then sort chronologically.
    by_end: dict[str, dict] = {}
    for row in rows:
        if row["end"] not in by_end or row["filed"] > by_end[row["end"]]["filed"]:
            by_end[row["end"]] = row
    return sorted(by_end.values(), key=lambda x: (x["end"], x["filed"]))


def instant_records(facts: dict, aliases: list[str]) -> list[dict]:
    rows = []
    facts_by_namespace = facts.get("facts", {})
    for tag in aliases:
        meta = next((facts_by_namespace.get(namespace, {}).get(tag) for namespace in ("us-gaap", "dei") if facts_by_namespace.get(namespace, {}).get(tag)), None)
        if not meta:
            continue
        units = meta.get("units", {})
        unit_name = "USD" if "USD" in units else next((u for u in units if "/shares" in u), None)
        unit_name = unit_name or next(iter(units), None)
        if unit_name is None:
            continue
        for item in units[unit_name]:
            if item.get("form") not in {"10-K", "20-F", "40-F", "6-K", "10-Q"}:
                continue
            if not item.get("end") or not item.get("filed"):
                continue
            rows.append({"end": item["end"], "filed": item["filed"], "val": signed_value(tag, unit_name, item), "tag": tag, "unit": unit_name})
    return sorted(rows, key=lambda x: (x["end"], x["filed"]))


def latest_before(rows: list[dict], when: str) -> dict | None:
    eligible = [row for row in rows if row["filed"] <= when]
    if not eligible:
        return None
    return max(eligible, key=lambda x: (x["end"], x["filed"]))


def annual_before(rows: list[dict], when: str) -> list[dict]:
    return [row for row in rows if row["filed"] <= when]


def build_financials(facts: dict) -> dict[str, list[dict]]:
    output: dict[str, list[dict]] = {}
    for key, aliases in FACT_ALIASES.items():
        output[key] = annual_records(facts, aliases) if key in {"revenue", "operating_income", "net_income", "cfo", "capex", "eps", "shares", "dividend_per_share"} else instant_records(facts, aliases)
        if key == "shares" and len(output[key]) < 4:
            output[key] = instant_records(facts, aliases)
    return output


def merge_annual(fin: dict[str, list[dict]], when: str) -> list[dict]:
    # Use the latest disclosed annual value for each metric, matching by fiscal
    # end where possible. A minimum of four annual periods is required.
    series: dict[str, dict[str, dict]] = {}
    for key, rows in fin.items():
        eligible = annual_before(rows, when)
        by_end: dict[str, dict] = {}
        for row in eligible:
            by_end[row["end"]] = row
        series[key] = by_end
    ends = sorted(set().union(*(set(v) for v in series.values())))
    merged = []
    for end in ends:
        row = {"end": end}
        for key, by_end in series.items():
            if end in by_end:
                row[key] = by_end[end]["val"]
        if "revenue" in row and "operating_income" in row and "net_income" in row:
            merged.append(row)
    return merged


def cagr(new: float, old: float, years: int = 3) -> float | None:
    if old <= 0 or new <= 0:
        return None
    return (new / old) ** (1 / years) - 1


def score_band(value: float | None, bands: list[tuple[float, int]], default: int = 0) -> int:
    if value is None or not math.isfinite(value):
        return default
    for threshold, score in bands:
        if value >= threshold:
            return score
    return 0


def point_in_time_score(ticker: str, fin: dict[str, list[dict]], when: str, price: float, tnx: float) -> dict:
    years = merge_annual(fin, when)
    if len(years) < 4:
        return {"eligible": False, "reason": "insufficient_reported_annual_history", "Q": None, "V": None}
    recent = years[-1]
    prior3 = years[-4]
    revenue_growth = cagr(recent.get("revenue", 0), prior3.get("revenue", 0))
    margin = recent.get("operating_income", 0) / recent["revenue"] if recent.get("revenue", 0) else None
    prior_margins = [y.get("operating_income", 0) / y["revenue"] for y in years[-6:-1] if y.get("revenue", 0)]
    median_margin = sorted(prior_margins)[len(prior_margins)//2] if prior_margins else None
    margin_delta = (margin - median_margin) * 100 if margin is not None and median_margin is not None else None
    q1 = score_band(margin_delta, [(0, 20), (-2, 15), (-4, 10), (-6, 5)])
    cap = GROWTH_CAPS[ticker]
    # The manual's bespoke growth metrics are unavailable in Company Facts;
    # reported revenue CAGR is a deliberately conservative proxy.
    growth = min(revenue_growth, cap) if revenue_growth is not None else None
    q2 = score_band(growth, [(cap, 15), (cap * 0.5, 10), (0, 5)]) if growth is not None else 0
    cfo = recent.get("cfo")
    capex = recent.get("capex")
    ni_total = sum(y.get("net_income", 0) for y in years[-3:])
    fcf_total = sum(y.get("cfo", 0) - y.get("capex", 0) for y in years[-3:] if y.get("cfo") is not None and y.get("capex") is not None)
    if ticker in {"DUK", "AWK"}:
        dividends = sum(y.get("dividend_per_share", 0) for y in years[-3:] if y.get("dividend_per_share") is not None)
        eps_total = sum(y.get("eps", 0) for y in years[-3:] if y.get("eps") is not None)
        payout = dividends / eps_total if eps_total > 0 else None
        q3 = score_band(-payout if payout is not None else None, [(-0.70, 15), (-0.80, 10), (-0.90, 5)], default=0)
    else:
        cash_conversion = fcf_total / ni_total if ni_total > 0 else None
        q3 = score_band(cash_conversion, [(1.00, 15), (0.80, 10), (0.60, 5)])
    debt = latest_before(fin["debt"], when)
    cash = latest_before(fin["cash"], when)
    leverage = None
    if debt and cash and recent.get("operating_income") is not None:
        # EBITDA proxy uses latest annual D&A only when available; operating
        # income is the conservative denominator otherwise.
        denominator = max(recent["operating_income"], 1.0)
        leverage = (debt["val"] - cash["val"]) / denominator
    q4 = score_band(-leverage if leverage is not None else None, [(-1.5, 10), (-2.5, 7), (-3.5, 3)], default=0)
    Q = q1 + q2 + q3 + q4

    fcf_per_share_values = []
    disclosed_share_snapshot = latest_before(fin["shares"], when)
    for y in years[-3:]:
        share_count = y.get("shares", 0) or (disclosed_share_snapshot["val"] if disclosed_share_snapshot else 0)
        if share_count and share_count < 1_000_000:
            # Several issuers disclose weighted shares in millions although
            # the SEC unit remains simply "shares".
            share_count *= 1_000_000
        if y.get("cfo") is None or y.get("capex") is None or share_count <= 0:
            continue
        fcf_per_share_values.append((y["cfo"] - y["capex"]) / share_count)
    c0 = min(fcf_per_share_values) if fcf_per_share_values and min(fcf_per_share_values) > 0 else None
    historical_multiple = None
    if c0 and price > 0:
        historical_multiple = price / c0
    # Historical relative valuation is formed from the available quarterly
    # decision observations in this run; current score only needs the last ten
    # years of the same point-in-time construction.
    valuation = 20 if historical_multiple is not None and historical_multiple <= 18 else 15 if historical_multiple is not None and historical_multiple <= 24 else 10 if historical_multiple is not None and historical_multiple <= 32 else 5 if historical_multiple is not None and historical_multiple <= 40 else 0
    r = max(0.08, tnx + 0.05)
    g = min(growth if growth is not None else -0.05, cap)
    gt = min(0.02, g)
    fv = None
    if c0 and r > gt:
        pv = sum(c0 * (1 + g) ** t / (1 + r) ** t for t in range(1, 6))
        terminal = c0 * (1 + g) ** 5 * (1 + gt) / (r - gt) / (1 + r) ** 5
        fv = pv + terminal
    safety = 20 if fv and price <= 0.80 * fv else 15 if fv and price <= 0.90 * fv else 10 if fv and price <= fv else 5 if fv and price <= 1.15 * fv else 0
    V = valuation + safety
    risk_green = q4 >= 7 and leverage is not None and leverage < 3.5
    eligible = bool(Q >= 45 and V >= 20 and safety >= 10 and risk_green and fv is not None)
    reason = "eligible" if eligible else ";".join(x for x, ok in [
        ("Q_below_45", Q < 45), ("V_below_20", V < 20), ("absolute_margin_below_10", safety < 10), ("risk_gate", not risk_green), ("FV_missing", fv is None)
    ] if ok)
    return {"eligible": eligible, "reason": reason, "Q": Q, "V": V, "q1": q1, "q2": q2, "q3": q3, "q4": q4, "E": valuation, "A": safety, "leverage": leverage, "revenue_growth": revenue_growth, "fv": fv, "c0": c0, "multiple": historical_multiple}


def month_end_dates(dates: list[str]) -> set[str]:
    by_month: dict[str, list[str]] = defaultdict(list)
    for d in dates:
        by_month[d[:7]].append(d)
    # Do not turn a partial current month into a second "month-end" signal.
    # A report may include the latest daily bars (useful for the NAV curve)
    # while the moat/valuation decision must remain on the last completed
    # calendar month.  The >=25-day allowance handles month ends that fall on
    # a weekend or market holiday.
    output: set[str] = set()
    for values in by_month.values():
        last = date.fromisoformat(values[-1])
        if last.day >= 25 or (last + timedelta(days=1)).month != last.month:
            output.add(values[-1])
    return output


def first_next(dates: list[str], day: str) -> str | None:
    for d in dates:
        if d > day:
            return d
    return None


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    result = 0.0
    for value in values:
        peak = max(peak, value)
        result = min(result, value / peak - 1)
    return result


def metrics(dates: list[str], nav: list[float], daily_returns: list[float], exposures: list[set[str]]) -> dict:
    years = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days / 365.25
    avg = sum(daily_returns) / len(daily_returns)
    variance = sum((r - avg) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)
    vol = math.sqrt(variance) * math.sqrt(252)
    nav_ratio = nav[-1] / INITIAL
    normalized = [v / INITIAL for v in nav]
    return {"start": dates[0], "end": dates[-1], "days": len(dates), "ending_nav": nav_ratio, "ending_value": nav[-1], "total_return_pct": (nav_ratio - 1) * 100, "cagr_pct": (nav_ratio ** (1 / years) - 1) * 100, "annualized_vol_pct": vol * 100, "max_drawdown_pct": max_drawdown(normalized) * 100, "sharpe_0rf": avg / math.sqrt(variance) * math.sqrt(252) if variance else 0, "avg_names": sum(len(x) for x in exposures) / len(exposures), "invested_days_pct": sum(bool(x) for x in exposures) / len(exposures) * 100}


def draw_chart(rows: list[dict], path: Path) -> None:
    width, height = 1600, 900
    img = Image.new("RGB", (width, height), "#F8FAFB")
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = 100, 80, 1510, 790
    all_values = [float(r["strategy_value"]) for r in rows] + [float(r["equal_weight_value"]) for r in rows] + [float(r["spy_value"]) for r in rows]
    lo, hi = min(all_values), max(all_values)
    lo = max(0, lo * 0.95)
    def xy(i: int, value: float) -> tuple[float, float]:
        x = left + (right-left) * i / (len(rows)-1)
        y = bottom - (bottom-top) * (value-lo) / (hi-lo)
        return x, y
    for frac in [0, .25, .5, .75, 1]:
        y = bottom - (bottom-top)*frac
        draw.line((left,y,right,y), fill="#DDE5E8", width=1)
        draw.text((15,y-8), f"${lo+(hi-lo)*frac:,.0f}", fill="#52636B")
    for idx in [0, len(rows)//4, len(rows)//2, 3*len(rows)//4, len(rows)-1]:
        x,_ = xy(idx, lo)
        draw.line((x,top,x,bottom), fill="#EEF2F3", width=1)
        draw.text((x-35,bottom+16), rows[idx]["date"][:7], fill="#52636B")
    colors = [("strategy_value", "#147D80", "Strategy"), ("equal_weight_value", "#183345", "11-stock EW B&H"), ("spy_value", "#C57C1F", "SPY B&H")]
    for key, color, _ in colors:
        pts=[xy(i,float(r[key])) for i,r in enumerate(rows)]
        draw.line(pts, fill=color, width=4)
    draw.text((left, 20), "Revised blue-chip strategy 10Y backtest | Initial $10,000", fill="#183345")
    lx=right-340
    for j,(_, color,label) in enumerate(colors):
        draw.line((lx+j*110,45,lx+j*110+20,45),fill=color,width=4); draw.text((lx+j*110+26,37),label,fill="#263642")
    draw.text((left, bottom+55), "Yahoo Finance adjusted close | SEC Company Facts at filing date | proxy implementation", fill="#62717B")
    img.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    prices = {ticker: yahoo_chart(ticker) for ticker in list(TICKERS) + ["SPY", "^TNX"]}
    facts = {ticker: build_financials(sec_facts(cik)) for ticker, (cik, _, _) in TICKERS.items()}
    dates = sorted(d for d in prices["SPY"] if START <= d <= END and all(d in prices[t] for t in TICKERS))
    month_ends = month_end_dates(dates)
    signal_at: dict[str, dict] = {}
    signal_rows: list[dict] = []
    active: set[str] = set()
    multiple_history: dict[str, list[float]] = defaultdict(list)
    for day in sorted(month_ends):
        ranked = {}
        tnx_price = prices["^TNX"].get(day, {}).get("close", 3.5) / 100
        for ticker in TICKERS:
            ranked[ticker] = point_in_time_score(ticker, facts[ticker], day, prices[ticker][day]["close"], tnx_price)
            block = ranked[ticker]
            multiple = block.get("multiple")
            prior = multiple_history[ticker]
            if multiple is not None and len(prior) >= 20:
                below = sum(x < multiple for x in prior)
                equal = sum(x == multiple for x in prior)
                percentile = (below + 0.5 * equal) / len(prior)
                relative_e = 20 if percentile <= 0.20 else 15 if percentile <= 0.40 else 10 if percentile <= 0.60 else 5 if percentile <= 0.80 else 0
            else:
                percentile = None
                relative_e = 0
            if multiple is not None:
                prior.append(multiple)
            block["valuation_percentile"] = percentile
            block["E"] = relative_e
            block["V"] = relative_e + (block.get("A") or 0)
            risk_green = block.get("q4") is not None and block.get("q4", 0) >= 7 and block.get("leverage") is not None and block["leverage"] < 3.5
            block["eligible"] = bool(block.get("Q") is not None and block["Q"] >= 45 and block["V"] >= 20 and (block.get("A") or 0) >= 10 and risk_green and block.get("fv") is not None and percentile is not None)
            if block.get("reason") != "insufficient_reported_annual_history":
                block["reason"] = "eligible" if block["eligible"] else ";".join(x for x, ok in [
                    ("Q_below_45", (block.get("Q") or 0) < 45), ("V_below_20", block["V"] < 20), ("relative_history_missing", percentile is None), ("absolute_margin_below_10", (block.get("A") or 0) < 10), ("risk_gate", not risk_green), ("FV_missing", block.get("fv") is None)
                ] if ok)
        eligible = sorted(t for t, block in ranked.items() if block["eligible"])
        active = set(eligible)
        execution = first_next(dates, day)
        signal_at[execution] = {"eligible": eligible, "scores": ranked, "signal_date": day} if execution else {"eligible": eligible, "scores": ranked, "signal_date": day}
        signal_rows.append({"signal_date": day, "execution_date": execution or "", "eligible": ";".join(eligible), "count": len(eligible), "detail": json.dumps(ranked, ensure_ascii=False)})

    nav = INITIAL
    strategy_nav = []
    strategy_returns = []
    exposures: list[set[str]] = []
    current: set[str] = set()
    previous_prices = {t: prices[t][dates[0]]["adjclose"] for t in TICKERS}
    previous_spy = prices["SPY"][dates[0]]["adjclose"]
    previous_equal = {t: prices[t][dates[0]]["adjclose"] for t in TICKERS}
    events=[]
    for i, day in enumerate(dates):
        if i > 0:
            if current:
                r = sum(prices[t][day]["adjclose"] / previous_prices[t] - 1 for t in current) / len(current)
            else:
                r = 0.0
            nav *= 1 + r
            strategy_returns.append(r)
        if day in signal_at:
            target = set(signal_at[day]["eligible"])
            if target != current:
                turnover = sum(abs((1/len(target) if t in target else 0) - (1/len(current) if t in current else 0)) for t in TICKERS) if target else len(current)
                cost = turnover * COST_BPS / 10000
                nav *= 1 - cost
                events.append({"signal_date": signal_at[day]["signal_date"], "execution_date": day, "old": sorted(current), "new": sorted(target), "turnover": turnover, "cost": cost})
                current = target
        strategy_nav.append(nav)
        exposures.append(set(current))
        previous_prices = {t: prices[t][day]["adjclose"] for t in TICKERS}
        previous_spy = prices["SPY"][day]["adjclose"]
    equal_nav = INITIAL
    spy_nav = INITIAL
    equal_series=[]; spy_series=[]
    prevs={t:prices[t][dates[0]]["adjclose"] for t in TICKERS}
    prevspy=prices["SPY"][dates[0]]["adjclose"]
    for day in dates:
        if day != dates[0]:
            equal_nav *= 1 + sum(prices[t][day]["adjclose"] / prevs[t] - 1 for t in TICKERS) / len(TICKERS)
            spy_nav *= prices["SPY"][day]["adjclose"] / prevspy
        equal_series.append(equal_nav); spy_series.append(spy_nav)
        prevs={t:prices[t][day]["adjclose"] for t in TICKERS}; prevspy=prices["SPY"][day]["adjclose"]
    series=[]
    for d, s, e, sp, names in zip(dates, strategy_nav, equal_series, spy_series, exposures):
        series.append({"date":d,"strategy_value":round(s,6),"equal_weight_value":round(e,6),"spy_value":round(sp,6),"eligible_count":len(names),"eligible":';'.join(sorted(names))})
    strategy_stats=metrics(dates,strategy_nav,strategy_returns,exposures)
    equal_stats=metrics(dates,equal_series,[equal_series[i]/equal_series[i-1]-1 for i in range(1,len(equal_series))],[set(TICKERS)]*len(equal_series[:-1]))
    spy_stats=metrics(dates,[x for x in spy_series],[spy_series[i]/spy_series[i-1]-1 for i in range(1,len(spy_series))],[{"SPY"}]*len(spy_series[:-1]))
    result={"config":{"start":START,"end":END,"initial_usd":INITIAL,"rule":"one eligible stock = 100%; two or more eligible stocks = equal weight; zero = cash","review":"month-end score, next trading day execution","cost_bps_per_side":COST_BPS,"cash_return":0,"price_adjustment":"Yahoo adjclose includes distributions","fundamental_proxy":True},"data":{"tickers":list(TICKERS),"price_source":"Yahoo Finance chart endpoint","fundamental_source":"SEC Company Facts, using facts no later than filing date","tnx_source":"Yahoo Finance ^TNX proxy for 10-year Treasury yield","trading_days":len(dates),"start":dates[0],"end":dates[-1]},"strategy":strategy_stats,"equal_weight_buy_hold":equal_stats,"spy_buy_hold":spy_stats,"difference":{"vs_equal_cagr_pct_points":strategy_stats["cagr_pct"]-equal_stats["cagr_pct"],"vs_spy_cagr_pct_points":strategy_stats["cagr_pct"]-spy_stats["cagr_pct"],"vs_equal_max_drawdown_pct_points":strategy_stats["max_drawdown_pct"]-equal_stats["max_drawdown_pct"]},"events":events,"signal_rows":signal_rows,"series":series}
    (OUT/"summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    with (OUT/"equity_curve.csv").open("w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=list(series[0]));w.writeheader();w.writerows(series)
    with (OUT/"monthly_signals.csv").open("w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=list(signal_rows[0]));w.writeheader();w.writerows(signal_rows)
    with (OUT/"rebalance_log.csv").open("w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=["signal_date","execution_date","old","new","turnover","cost"]);w.writeheader();w.writerows(events)
    draw_chart(series, OUT/"equity_curve.png")
    def line(label, block):
        return f"| {label} | ${block['ending_value']:,.0f} | {block['total_return_pct']:.2f}% | {block['cagr_pct']:.2f}% | {block['annualized_vol_pct']:.2f}% | {block['max_drawdown_pct']:.2f}% | {block['sharpe_0rf']:.2f} |"
    report=["# 修订版永续蓝筹策略：十年回测（可回测近似版）","",f"区间：{dates[0]} 至 {dates[-1]}；起始资金：${INITIAL:,.0f}。", "", "## 结果", "", "| 组合 | 期末资产 | 累计收益 | 年化收益 | 年化波动 | 最大回撤 | 夏普（0无风险） |", "|---|---:|---:|---:|---:|---:|---:|", line("策略：动态等权",strategy_stats), line("11股等权买入持有",equal_stats), line("SPY买入持有",spy_stats), "", f"策略相对11股等权买入持有：年化收益差 {strategy_stats['cagr_pct']-equal_stats['cagr_pct']:+.2f} 个百分点，最大回撤差 {strategy_stats['max_drawdown_pct']-equal_stats['max_drawdown_pct']:+.2f} 个百分点。", f"策略平均持有 {strategy_stats['avg_names']:.2f} 只，处于持仓状态的交易日占比 {strategy_stats['invested_days_pct']:.1f}%，调仓/信号事件 {len(events)} 次。", "", "## 重要口径", "", "1. 按用户指定：一个合格标的满仓；多个合格标的等权；没有合格标的持现金，现金收益按0%。", "2. 月末用截至当日已在SEC披露的年度财务数据评估，下一交易日执行；价格收益用Yahoo Finance复权收盘价，包含分红再投资。", "3. 修订版中的部分核心指标没有统一历史机器数据，例如Visa支付量、COST续费率、DUK/AWK已实现费基、MCD同店销售、CNI RTM、CME ADV等。本回测用可公开重建的标准财务代理指标，属于可回测近似版，不能冒充完全严格版。", "4. 这不是用当前评分回填过去；每个历史月只读取当时已经提交的SEC事实。代价是信号较保守，且可能与人工按完整年报/季报复核的最终信号不同。", "5. 研究用途，不构成收益保证或投资建议。", "", "文件：`summary.json`、`equity_curve.csv`、`monthly_signals.csv`、`rebalance_log.csv`、`equity_curve.png`。"]
    (OUT/"report.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    print(json.dumps({"strategy":strategy_stats,"equal_weight_buy_hold":equal_stats,"spy_buy_hold":spy_stats,"events":len(events),"output":str(OUT)},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
