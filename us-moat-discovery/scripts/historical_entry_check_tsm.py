#!/usr/bin/env python3
"""Historical entry check for the dedicated TSM valuation model."""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_bluechip_strategy_10y as base
import backtest_bluechip_strategy_v11 as v11
import backtest_bluechip_strategy_v14 as v14
import historical_entry_check_cost_asml as common


ROOT = base.ROOT
OUT = ROOT / "reports/historical-entry-tsm"
START, END = "2016-09-16", "2026-09-15"
TSM_ADR_MULTIPLIER = 5.0
PARAMS = {
    "moat_premium": 1.10,
    "discount_floor": 0.095,
    "spread": 0.050,
    "growth_cap": 0.100,
    "terminal_growth": 0.025,
    "pe_target": 22.0,
    "ev_ebit_multiple": 22.0,
    "primary_ceiling": 1.25,
    "reserve_ceiling": 1.45,
}


def finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def point_eps(year: dict) -> float | None:
    value = year.get("eps")
    if finite(value) and value > 0:
        return value * TSM_ADR_MULTIPLIER
    ni, shares = year.get("net_income"), year.get("shares")
    if finite(ni) and finite(shares) and ni > 0 and shares > 0:
        if shares < 1_000_000:
            shares *= 1_000_000
        return ni / shares * TSM_ADR_MULTIPLIER
    return None


def share_count(year: dict, fin: dict, when: str) -> float | None:
    shares = year.get("shares")
    if not finite(shares):
        snap = v11.latest_by_tag(fin.get("shares", []), when)
        shares = next(iter(snap.values()), {}).get("val") if snap else None
    if finite(shares) and shares < 1_000_000:
        shares *= 1_000_000
    return shares if finite(shares) and shares > 0 else None


def op_per_adr_share(years: list[dict], fin: dict, when: str) -> list[float]:
    values = []
    for y in years[-5:]:
        op, shares = y.get("operating_income"), share_count(y, fin, when)
        if finite(op) and shares and op > 0:
            values.append(op / shares * TSM_ADR_MULTIPLIER)
    return values


def dcf(c0: float | None, g: float | None, tnx: float) -> float | None:
    p = PARAMS
    if not finite(c0) or not finite(g) or c0 <= 0:
        return None
    r = max(p["discount_floor"], tnx + p["spread"])
    g = min(g, p["growth_cap"])
    gt = min(p["terminal_growth"], g)
    if r <= gt:
        return None
    cfs = []
    for year in range(1, 11):
        rate = g if year <= 5 else g + (gt - g) * (year - 5) / 5
        cfs.append(c0 * (1 + rate) ** year)
    pv = sum(cf / (1 + r) ** year for year, cf in enumerate(cfs, 1))
    terminal = cfs[-1] * (1 + gt) / (r - gt) / (1 + r) ** 10
    return pv + terminal


def score(when: str, price: float, tnx: float, fin: dict, pe_history: list[float]) -> dict:
    years = base.merge_annual(fin, when)
    eps_values = [point_eps(y) for y in years]
    eps_values = [x for x in eps_values if finite(x) and x > 0]
    eps0 = common.normalized_latest(eps_values)
    growth = common.growth_3y(eps_values)
    op_values = op_per_adr_share(years, fin, when)
    dcf_value = dcf(eps0, growth, tnx)
    pe_used = median(pe_history[-60:]) if len(pe_history) >= 24 else PARAMS["pe_target"]
    pe_anchor = eps0 * pe_used if eps0 else None
    ebit0 = median(op_values[-5:]) if op_values else None
    ebit_anchor = ebit0 * PARAMS["ev_ebit_multiple"] if ebit0 else None
    fv = 0.50 * dcf_value + 0.30 * pe_anchor + 0.20 * ebit_anchor if all(finite(x) for x in [dcf_value, pe_anchor, ebit_anchor]) else None
    fv_premium = fv * PARAMS["moat_premium"] if finite(fv) else None
    ratio = price / fv_premium if finite(fv_premium) and fv_premium > 0 else None
    quality = common.quality_snapshot("TSM", years, fin, when)
    q_ok = finite(quality.get("Q")) and quality["Q"] >= 35 and quality.get("q4", 0) >= 3
    primary = bool(q_ok and finite(ratio) and ratio <= PARAMS["primary_ceiling"])
    reserve = bool(q_ok and finite(ratio) and ratio <= PARAMS["reserve_ceiling"])
    current_pe = price / eps0 if finite(eps0) and eps0 > 0 else None
    return {
        "date": when, "ticker": "TSM", "price_usd_adr": price, "years": len(years),
        "eps0_adr": eps0, "eps_growth": growth, "dcf": dcf_value, "pe_used": pe_used,
        "pe_anchor": pe_anchor, "ev_ebit_anchor": ebit_anchor, "fv": fv,
        "moat_premium": PARAMS["moat_premium"], "fv_with_moat_premium": fv_premium,
        "price_fv_with_moat_premium": ratio, "current_pe": current_pe,
        "Q": quality.get("Q"), "q4": quality.get("q4"), "quality_ok": q_ok,
        "primary": primary, "reserve": reserve,
        "method": "50% cycle-adjusted EPS-DCF + 30% historical PE + 20% EV/EBIT, then 10% moat premium",
    }


def ranges(rows: list[dict], key: str) -> list[dict]:
    dates = [r["date"] for r in rows if r.get(key)]
    if not dates:
        return []
    out, start, prev = [], dates[0], dates[0]
    for current in dates[1:]:
        a, b = date.fromisoformat(prev), date.fromisoformat(current)
        if (b.year - a.year) * 12 + b.month - a.month != 1:
            out.append({"start": start, "end": prev, "months": len([x for x in dates if start <= x <= prev])})
            start = current
        prev = current
    out.append({"start": start, "end": prev, "months": len([x for x in dates if start <= x <= prev])})
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    v14.install_extended_sec_parser()
    base.FACT_ALIASES["eps"] = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
    base.TICKERS["TSM"] = v14.EXTRA_CIKS["TSM"]
    prices = {"TSM": base.yahoo_chart("TSM"), "^TNX": base.yahoo_chart("^TNX")}
    fin = v14.build_financials_v14("TSM", base.sec_facts(base.TICKERS["TSM"][0]))
    common_dates = sorted(d for d in prices["TSM"] if START <= d <= END and d in prices["^TNX"])
    dates = sorted(base.month_end_dates(common_dates))
    history, rows = [], []
    for when in dates:
        tnx = prices["^TNX"][when]["close"] / 100
        row = score(when, prices["TSM"][when]["close"], tnx, fin, history)
        rows.append(row)
        if finite(row.get("current_pe")):
            history.append(row["current_pe"])
    fields = list(rows[0].keys())
    with (OUT / "monthly_entries.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    valid = [r for r in rows if finite(r.get("price_fv_with_moat_premium"))]
    summary = {
        "period": [START, END], "fx": "not required; USD facts and USD ADR price",
        "params": PARAMS, "valid_months": len(valid),
        "primary": ranges(rows, "primary"), "reserve": ranges(rows, "reserve"),
        "min_ratio": min(valid, key=lambda r: r["price_fv_with_moat_premium"]) if valid else None,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = ["# TSM历史入场检查", "", f"区间：{START}至{END}；月末信号；只使用当时已申报的年度数据。", "", "## 新模型", "", "TSM按ADR口径重建EPS（普通股EPS×5）；50%周期调整后EPS-DCF + 30%历史PE + 20% EV/EBIT，最后增加10%护城河溢价。", f"主买入：price/FV≤{PARAMS['primary_ceiling']:.2f}；储备买入：price/FV≤{PARAMS['reserve_ceiling']:.2f}；质量门槛Q≥35且风险分≥3。", "", "## 结果", "", f"主买入窗口：{summary['primary']}", f"储备买入窗口：{summary['reserve']}", f"最低price/FV：{summary['min_ratio']}", "", "注：TSM仍存在地缘政治、资本开支周期和客户集中度风险，因此溢价设为10%，低于ASML的15%。"]
    (OUT / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
