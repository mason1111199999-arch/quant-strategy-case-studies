#!/usr/bin/env python3
"""Historical entry check for the dedicated COST/ASML valuation models.

This is not a portfolio backtest. It asks a narrower question: using only
fundamentals filed by each month-end, did the dedicated valuation formula
ever put COST or ASML into the primary/reserve buying zone during the last
ten years?

The ASML SEC facts are in EUR while the Yahoo ADR price is in USD.  Because
the local cache does not contain a historical EURUSD series, the script runs
three FX sensitivities (1.00, 1.10 and 1.20 USD/EUR).  A signal is robust if
it survives that range.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_bluechip_strategy_10y as base
import backtest_bluechip_strategy_v11 as v11
import backtest_bluechip_strategy_v14 as v14


ROOT = base.ROOT
OUT = ROOT / "reports/historical-entry-cost-asml"
START, END = "2016-09-16", "2026-09-15"
TICKERS = ["COST", "ASML"]

# Dedicated model agreed in the discussion.
PARAMS = {
    "COST": {
        "currency": "USD",
        "moat_premium": 1.00,
        "discount_floor": 0.090,
        "spread": 0.045,
        "growth_cap": 0.080,
        "terminal_growth": 0.025,
        "pe_target": 40.0,
        "fcf_yield_target": 0.027,
        "primary_ceiling": 1.25,
        "reserve_ceiling": 1.40,
    },
    "ASML": {
        "currency": "EUR",
        # ASML is not treated like an ordinary semiconductor company: EUV
        # monopoly, installed-base/service economics and switching costs
        # justify a moderate 15% premium on the blended fair value.
        "moat_premium": 1.15,
        "discount_floor": 0.100,
        "spread": 0.055,
        "growth_cap": 0.080,
        "terminal_growth": 0.030,
        "pe_target": 35.0,
        "ev_ebit_multiple": 25.0,
        "primary_ceiling": 1.25,
        "reserve_ceiling": 1.45,
    },
}


def finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def point_eps(ticker: str, year: dict) -> float | None:
    """Use reported EPS for COST; derive ASML EPS in EUR where needed."""
    if ticker == "COST":
        value = year.get("eps")
        return float(value) if finite(value) and value > 0 else None
    ni, shares = year.get("net_income"), year.get("shares")
    if finite(ni) and finite(shares) and ni > 0 and shares > 0:
        if shares < 1_000_000:
            shares *= 1_000_000
        return ni / shares
    value = year.get("eps")
    return float(value) if finite(value) and value > 0 else None


def share_count(ticker: str, year: dict, fin: dict, when: str) -> float | None:
    shares = year.get("shares")
    if not finite(shares):
        snap = v11.latest_by_tag(fin.get("shares", []), when)
        shares = next(iter(snap.values()), {}).get("val") if snap else None
    if finite(shares) and shares < 1_000_000:
        shares *= 1_000_000
    return shares if finite(shares) and shares > 0 else None


def fcf_per_share(ticker: str, years: list[dict], fin: dict, when: str) -> list[float]:
    values = []
    for year in years[-5:]:
        cfo, capex = year.get("cfo"), year.get("capex")
        shares = share_count(ticker, year, fin, when)
        if finite(cfo) and finite(capex) and shares:
            value = (cfo - capex) / shares
            if finite(value) and value > 0:
                values.append(value)
    return values


def operating_profit_per_share(ticker: str, years: list[dict], fin: dict, when: str) -> list[float]:
    values = []
    for year in years[-5:]:
        op, shares = year.get("operating_income"), share_count(ticker, year, fin, when)
        if finite(op) and shares and op > 0:
            values.append(op / shares)
    return values


def normalized_latest(values: list[float]) -> float | None:
    values = [x for x in values if finite(x) and x > 0]
    if not values:
        return None
    return 0.60 * values[-1] + 0.40 * median(values[-5:])


def growth_3y(values: list[float]) -> float | None:
    values = [x for x in values if finite(x) and x > 0]
    if len(values) < 4:
        return None
    return (values[-1] / values[-4]) ** (1 / 3) - 1


def quality_snapshot(ticker: str, years: list[dict], fin: dict, when: str) -> dict:
    """Light quality gate; deliberately separate from the new valuation."""
    if len(years) < 4:
        return {"Q": None, "q4": None}
    q1, margin_delta = v11.margin_scores(years)
    revenues = [y.get("revenue") for y in years if finite(y.get("revenue")) and y["revenue"] > 0]
    growth = growth_3y(revenues)
    q2 = base.score_band(growth, v14.GROWTH_BANDS[ticker]) if growth is not None else 0
    fcf_values = []
    for year in years[-3:]:
        cfo, capex = year.get("cfo"), year.get("capex")
        if finite(cfo) and finite(capex):
            fcf_values.append(cfo - capex)
    ni_total = sum(y.get("net_income", 0) for y in years[-3:] if finite(y.get("net_income")))
    conversion = sum(fcf_values) / ni_total if ni_total > 0 else None
    q3 = base.score_band(conversion, v14.FCF_BANDS[ticker]) if conversion is not None else 0
    debt = v11.balance_total(fin.get("debt", []), when)
    cash = v11.balance_total(fin.get("cash", []), when)
    latest_op = years[-1].get("operating_income")
    leverage = (debt - cash) / latest_op if finite(debt) and finite(cash) and finite(latest_op) and latest_op > 0 else None
    q4 = 0
    if leverage is not None:
        good, acceptable, weak = v14.LEVERAGE_BANDS[ticker]
        q4 = 10 if leverage <= good else 7 if leverage <= acceptable else 3 if leverage <= weak else 0
    return {
        "Q": q1 + q2 + q3 + q4,
        "q1": q1,
        "q2": q2,
        "q3": q3,
        "q4": q4,
        "growth": growth,
        "conversion": conversion,
        "leverage": leverage,
        "margin_delta": margin_delta,
    }


def dcf(c0: float | None, g: float | None, p: dict, tnx: float) -> float | None:
    if not finite(c0) or not finite(g) or c0 <= 0:
        return None
    r = max(p["discount_floor"], tnx + p["spread"])
    g = min(g, p["growth_cap"])
    gt = min(p["terminal_growth"], g)
    if r <= gt:
        return None
    cashflows = []
    for year in range(1, 11):
        if year <= 5:
            rate = g
        else:
            rate = g + (gt - g) * (year - 5) / 5
        cashflows.append(c0 * (1 + rate) ** year)
    pv = sum(cf / (1 + r) ** year for year, cf in enumerate(cashflows, 1))
    terminal = cashflows[-1] * (1 + gt) / (r - gt) / (1 + r) ** 10
    return pv + terminal


def price_history(prices: dict, dates: list[str], ticker: str, year: str) -> float | None:
    row = prices[ticker].get(year)
    return row.get("close") if row else None


def month_end_dates(prices: dict, ticker: str = "COST") -> list[str]:
    common = sorted(d for d in prices[ticker] if START <= d <= END and all(d in prices[t] for t in TICKERS + ["^TNX"]))
    months = {}
    for d in common:
        months[d[:7]] = d
    return list(months.values())


def ranges(rows: list[dict], key: str) -> list[dict]:
    dates = [r["date"] for r in rows if r.get(key)]
    if not dates:
        return []
    out = []
    start = prev = dates[0]
    for d in dates[1:]:
        a = date.fromisoformat(prev)
        b = date.fromisoformat(d)
        consecutive = (b.year - a.year) * 12 + b.month - a.month == 1
        if not consecutive:
            out.append({"start": start, "end": prev, "months": len([x for x in dates if start <= x <= prev])})
            start = d
        prev = d
    out.append({"start": start, "end": prev, "months": len([x for x in dates if start <= x <= prev])})
    return out


def score_month(ticker: str, when: str, price_usd: float, tnx: float, fin: dict, fx: float | None, pe_history: list[float]) -> dict:
    years = base.merge_annual(fin, when)
    p = PARAMS[ticker]
    eps_values = [point_eps(ticker, y) for y in years]
    eps_values = [x for x in eps_values if finite(x) and x > 0]
    fcf_values = fcf_per_share(ticker, years, fin, when)
    op_values = operating_profit_per_share(ticker, years, fin, when)
    e0 = normalized_latest(eps_values)
    g = growth_3y(eps_values)
    fcf0 = median(fcf_values[-5:]) if fcf_values else None
    dcf_value = dcf(e0, g, p, tnx)
    if ticker == "COST":
        price_native = price_usd
        pe_used = median(pe_history[-60:]) if len(pe_history) >= 24 else p["pe_target"]
        pe_anchor = e0 * pe_used if e0 else None
        cash_anchor = fcf0 / p["fcf_yield_target"] if fcf0 else None
        anchors = [x for x in [dcf_value, pe_anchor, cash_anchor] if finite(x)]
        fv = 0.50 * dcf_value + 0.30 * pe_anchor + 0.20 * cash_anchor if len(anchors) == 3 else None
        method = "50% EPS-DCF + 30% PE anchor + 20% FCF-yield anchor"
    else:
        price_native = price_usd / fx if fx and fx > 0 else None
        pe_used = median(pe_history[-60:]) if len(pe_history) >= 24 else p["pe_target"]
        pe_anchor = e0 * pe_used if e0 else None
        ebit0 = median(op_values[-5:]) if op_values else None
        ebit_anchor = ebit0 * p["ev_ebit_multiple"] if ebit0 else None
        anchors = [x for x in [dcf_value, pe_anchor, ebit_anchor] if finite(x)]
        fv = 0.50 * dcf_value + 0.30 * pe_anchor + 0.20 * ebit_anchor if len(anchors) == 3 else None
        method = "50% cycle-adjusted EPS-DCF + 30% PE anchor + 20% EV/EBIT anchor"
    ratio = price_native / fv if finite(price_native) and finite(fv) and fv > 0 else None
    fv_with_premium = fv * p["moat_premium"] if finite(fv) else None
    ratio_with_premium = price_native / fv_with_premium if finite(price_native) and finite(fv_with_premium) and fv_with_premium > 0 else None
    quality = quality_snapshot(ticker, years, fin, when)
    q_ok = finite(quality.get("Q")) and quality["Q"] >= 35 and quality.get("q4", 0) >= 3
    return {
        "date": when,
        "ticker": ticker,
        "price_usd": price_usd,
        "fx_usd_per_eur": fx,
        "price_native": price_native,
        "years": len(years),
        "eps0": e0,
        "eps_growth": g,
        "pe_used": pe_used,
        "fcf0": fcf0,
        "dcf": dcf_value,
        "pe_anchor": pe_anchor,
        "secondary_anchor": cash_anchor if ticker == "COST" else ebit_anchor,
        "fv": fv,
        "price_fv": ratio,
        "moat_premium": p["moat_premium"],
        "fv_with_moat_premium": fv_with_premium,
        "price_fv_with_moat_premium": ratio_with_premium,
        "Q": quality.get("Q"),
        "q4": quality.get("q4"),
        "quality_ok": q_ok,
        "primary_value_only": finite(ratio) and ratio <= p["primary_ceiling"],
        "reserve_value_only": finite(ratio) and ratio <= p["reserve_ceiling"],
        "primary_with_quality": finite(ratio) and ratio <= p["primary_ceiling"] and q_ok,
        "reserve_with_quality": finite(ratio) and ratio <= p["reserve_ceiling"] and q_ok,
        "primary_premium_only": finite(ratio_with_premium) and ratio_with_premium <= p["primary_ceiling"],
        "reserve_premium_only": finite(ratio_with_premium) and ratio_with_premium <= p["reserve_ceiling"],
        "primary_premium_quality": finite(ratio_with_premium) and ratio_with_premium <= p["primary_ceiling"] and q_ok,
        "reserve_premium_quality": finite(ratio_with_premium) and ratio_with_premium <= p["reserve_ceiling"] and q_ok,
        "fv_method": method,
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    v14.install_extended_sec_parser()
    # The base parser historically had only diluted EPS; COST's facts also
    # need the basic fallback.  v14's extended parser accepts USD/shares.
    base.FACT_ALIASES["eps"] = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
    for ticker, spec in v14.EXTRA_CIKS.items():
        base.TICKERS[ticker] = spec
    prices = {ticker: base.yahoo_chart(ticker) for ticker in TICKERS + ["^TNX"]}
    facts = {ticker: v14.build_financials_v14(ticker, base.sec_facts(base.TICKERS[ticker][0])) for ticker in TICKERS}
    dates = month_end_dates(prices)
    fx_scenarios = [1.00, 1.10, 1.20]
    all_rows = []
    for fx in fx_scenarios:
        pe_histories = {ticker: [] for ticker in TICKERS}
        for when in dates:
            tnx_row = prices["^TNX"].get(when)
            tnx = (tnx_row.get("close") / 100.0) if tnx_row else 0.04
            for ticker in TICKERS:
                price = prices[ticker][when]["close"]
                row = score_month(ticker, when, price, tnx, facts[ticker], fx if ticker == "ASML" else None, pe_histories[ticker])
                # The FX scenario does not affect COST; keep one copy in the
                # output while still maintaining a proper ASML history.
                if ticker == "COST" and fx != fx_scenarios[0]:
                    continue
                all_rows.append(row)
                if finite(row.get("price_native")) and finite(row.get("eps0")) and row["eps0"] > 0:
                    pe_histories[ticker].append(row["price_native"] / row["eps0"])
    # COST has one result series; ASML has one series per FX scenario.
    compact = [r for r in all_rows if r["ticker"] == "COST"]
    compact += [r for r in all_rows if r["ticker"] == "ASML"]
    with (OUT / "monthly_entries.csv").open("w", newline="", encoding="utf-8") as fh:
        fields = list(compact[0].keys())
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(compact)

    summary = {"period": [START, END], "method": "dedicated valuation, point-in-time annual SEC facts, month-end close", "fx_scenarios_usd_per_eur": fx_scenarios, "tickers": {}}
    for ticker in TICKERS:
        summary["tickers"][ticker] = {}
        rows = [r for r in all_rows if r["ticker"] == ticker]
        if ticker == "COST":
            rows = [r for r in rows if r["fx_usd_per_eur"] is None]
            for key in ["primary_value_only", "reserve_value_only", "primary_with_quality", "reserve_with_quality"]:
                summary["tickers"][ticker][key] = ranges(rows, key)
            valid = [r for r in rows if finite(r.get("price_fv"))]
            summary["tickers"][ticker]["min_ratio"] = min(valid, key=lambda r: r["price_fv"]) if valid else None
            summary["tickers"][ticker]["valid_months"] = len(valid)
        else:
            for fx in fx_scenarios:
                srows = [r for r in rows if r["fx_usd_per_eur"] == fx]
                block = {"valid_months": len([r for r in srows if finite(r.get("price_fv"))])}
                for key in ["primary_value_only", "reserve_value_only", "primary_with_quality", "reserve_with_quality"]:
                    block[key] = ranges(srows, key)
                for key in ["primary_premium_only", "reserve_premium_only", "primary_premium_quality", "reserve_premium_quality"]:
                    block[key] = ranges(srows, key)
                valid = [r for r in srows if finite(r.get("price_fv"))]
                block["min_ratio"] = min(valid, key=lambda r: r["price_fv"]) if valid else None
                valid_premium = [r for r in srows if finite(r.get("price_fv_with_moat_premium"))]
                block["min_ratio_with_moat_premium"] = min(valid_premium, key=lambda r: r["price_fv_with_moat_premium"]) if valid_premium else None
                summary["tickers"][ticker][str(fx)] = block
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    report = []
    report.append("# COST / ASML 历史入场检查")
    report.append("")
    report.append(f"区间：{START} 至 {END}；月末信号；基本面只使用当时已申报数据。")
    report.append("ASML 财报为 EUR、价格为 USD ADR；因本地未缓存历史 EURUSD，采用 1.00/1.10/1.20 美元兑欧元敏感性。")
    report.append("ASML 新版模型在基础综合公允价值上增加 15% 护城河溢价；原模型结果同时保留作对照。")
    report.append("")
    for ticker, data in summary["tickers"].items():
        report.append(f"## {ticker}")
        if ticker == "COST":
            for key in ["primary_value_only", "reserve_value_only", "primary_with_quality", "reserve_with_quality"]:
                report.append(f"- {key}: {data[key]}")
            report.append(f"- 最低 price/FV: {data['min_ratio']}")
        else:
            for fx in fx_scenarios:
                block = data[str(fx)]
                report.append(f"- FX={fx:.2f}: 原模型 primary={block['primary_value_only']}; 原模型 reserve={block['reserve_value_only']}; 加15%护城河溢价后 primary={block['primary_premium_quality']}; 加15%护城河溢价后 reserve={block['reserve_premium_quality']}; min={block['min_ratio']}")
        report.append("")
    (OUT / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
