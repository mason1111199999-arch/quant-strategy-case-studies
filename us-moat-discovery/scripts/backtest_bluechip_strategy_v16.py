#!/usr/bin/env python3
"""V1.6 comprehensive backtest with dedicated COST/ASML valuation.

Universe: the current 12-name core pool, with BRK.B removed.  V1.5 rules
remain for the other companies.  COST and ASML use their dedicated models;
ASML receives a 15% fair-value moat premium.  ASML financials are in EUR and
the ADR price is in USD, so ASML_FX is an explicit sensitivity assumption.
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from datetime import date

import backtest_bluechip_strategy_10y as base
import backtest_bluechip_strategy_v11 as v11
import backtest_bluechip_strategy_v14 as v14
import backtest_bluechip_strategy_v15 as v15
import historical_entry_check_cost_asml as dedicated


ROOT = base.ROOT
OUT = ROOT / "reports/bluechip-strategy-10y-v16"
START, END, INITIAL, COST_BPS = base.START, base.END, base.INITIAL, base.COST_BPS
VERSION = "V1.6"
ASML_FX = float(os.environ.get("ASML_FX", "1.10"))

TICKERS = [
    "PG", "V", "MCD", "WM", "KO", "COST", "MA", "CNI", "SPGI", "TSM", "ASML", "AAPL",
]
DEDICATED_TICKERS = {"COST", "ASML"}

# V1.5's relaxed leverage calibration applies to the non-dedicated names.
v14.LEVERAGE_BANDS.update({"CNI": (3.0, 4.5, 6.5), "WM": (4.0, 6.0, 8.0), "KO": (4.0, 6.0, 8.0)})


def dedicated_a(ratio: float | None) -> int | None:
    """Valuation score for the dedicated models after moat premium."""
    if ratio is None or not math.isfinite(ratio):
        return None
    return 20 if ratio <= 1.00 else 15 if ratio <= 1.10 else 10 if ratio <= 1.25 else 5 if ratio <= 1.45 else 0


def score_dedicated(ticker: str, fin: dict, when: str, price: float, tnx: float, history: list[float]) -> dict:
    fx = ASML_FX if ticker == "ASML" else None
    raw = dedicated.score_month(ticker, when, price, tnx, fin, fx, history)
    ratio = raw.get("price_fv_with_moat_premium")
    a = dedicated_a(ratio)
    multiple = None
    if raw.get("price_native") and raw.get("eps0") and raw["eps0"] > 0:
        multiple = raw["price_native"] / raw["eps0"]
    e, percentile = v14.percentile_score(multiple, history)
    q = raw.get("Q")
    s = q + (e or 0) + (a or 0) if q is not None and e is not None and a is not None else None
    q4 = raw.get("q4", 0)
    # q4 is already computed from the point-in-time leverage proxy inside the
    # dedicated model.  The raw helper intentionally returns the score rather
    # than the underlying leverage number, so q4 is the risk gate here.
    risk_ok = q4 >= 3
    # The dedicated models already combine intrinsic value, historical
    # multiple and the company-specific secondary anchor.  Do not apply the
    # generic V1.5 E/S gate a second time; retain only a moderate quality and
    # risk gate, matching the standalone COST/ASML validation.
    quality_ok = risk_ok and q is not None and q >= 35
    primary = bool(quality_ok and ratio is not None and ratio <= 1.25)
    reserve = bool(quality_ok and ratio is not None and ratio <= 1.45)
    eligible = primary or reserve
    hold = bool(quality_ok and ratio is not None and ratio <= 1.55)
    return {
        **raw,
        "eligible": eligible, "primary": primary, "reserve": reserve, "hold": hold,
        "Q": q, "E": e, "A": a, "S": s, "multiple": multiple, "percentile": percentile,
        "price_fv": ratio, "reason": "primary" if primary else "reserve" if reserve else "dedicated_quality_or_valuation_gate",
        "category": v14.CATEGORY[ticker], "v16_moat_premium": raw.get("moat_premium", 1.0),
    }


def score_v16(ticker: str, fin: dict, when: str, price: float, tnx: float, history: list[float]) -> dict:
    if ticker in DEDICATED_TICKERS:
        return score_dedicated(ticker, fin, when, price, tnx, history)
    return v15.score_v15(ticker, fin, when, price, tnx, history)


def draw_chart(rows: list[dict], path) -> None:
    width, height = 1600, 900
    img = base.Image.new("RGB", (width, height), "#F8FAFB")
    draw = base.ImageDraw.Draw(img)
    left, top, right, bottom = 100, 80, 1510, 790
    values = [float(r[key]) for r in rows for key in ("strategy_value", "equal_weight_value", "spy_value")]
    lo, hi = max(0, min(values) * 0.95), max(values)
    def xy(i, value):
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
    lines = [("strategy_value", "#147D80", f"{VERSION} Strategy"), ("equal_weight_value", "#183345", "12-stock EW B&H"), ("spy_value", "#C57C1F", "SPY B&H")]
    for key, color, _ in lines:
        draw.line([xy(i, float(r[key])) for i, r in enumerate(rows)], fill=color, width=4)
    draw.text((left, 20), f"{VERSION} comprehensive backtest | Initial $10,000", fill="#183345")
    lx = right - 390
    for j, (_, color, label) in enumerate(lines):
        draw.line((lx + j * 130, 45, lx + j * 130 + 20, 45), fill=color, width=4)
        draw.text((lx + j * 130 + 26, 37), label, fill="#263642")
    draw.text((left, bottom + 55), "Yahoo adjusted close | SEC Company Facts at filing date | ASML FX sensitivity", fill="#62717B")
    img.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    v14.TICKERS = TICKERS
    v11.TICKERS = TICKERS
    v14.install_extended_sec_parser()
    base.FACT_ALIASES["eps"] = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
    for ticker, spec in v14.EXTRA_CIKS.items():
        base.TICKERS[ticker] = spec
    v11.GROWTH_CAPS.update(v14.GROWTH_CAPS)
    prices = {ticker: base.yahoo_chart(ticker) for ticker in TICKERS + ["SPY", "^TNX"]}
    facts = {ticker: v14.build_financials_v14(ticker, base.sec_facts(base.TICKERS[ticker][0])) for ticker in TICKERS}
    dates = sorted(day for day in prices["SPY"] if START <= day <= END and all(day in prices[ticker] for ticker in TICKERS))
    month_ends = base.month_end_dates(dates)
    history = defaultdict(list)
    current: dict[str, float] = {}
    fail_counts, extreme_counts = {}, {}
    signal_at, signal_rows = {}, []
    for day in sorted(month_ends):
        tnx = prices["^TNX"].get(day, {}).get("close", 3.5) / 100
        scores = {ticker: score_v16(ticker, facts[ticker], day, prices[ticker][day]["close"], tnx, history[ticker]) for ticker in TICKERS}
        for ticker in TICKERS:
            if scores[ticker].get("multiple") is not None and (ticker in DEDICATED_TICKERS or date.fromisoformat(day).month in {3, 6, 9, 12}):
                history[ticker].append(scores[ticker]["multiple"])
        target, active, frozen = v14.target_weights(current, scores, fail_counts, extreme_counts)
        execution = base.first_next(dates, day)
        signal_at[execution or day] = {"signal_date": day, "target": target, "active": active, "frozen": frozen}
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
    strategy_stats = v14.daily_metrics(dates, strategy_nav, strategy_returns, exposure_weights)
    equal_stats = v14.daily_metrics(dates, equal_series, [equal_series[i] / equal_series[i - 1] - 1 for i in range(1, len(equal_series))], [dict.fromkeys(TICKERS, 1 / len(TICKERS))] * len(equal_series))
    spy_stats = v14.daily_metrics(dates, spy_series, [spy_series[i] / spy_series[i - 1] - 1 for i in range(1, len(spy_series))], [{"SPY": 1}] * len(spy_series))
    summary = {
        "config": {"version": VERSION, "start": START, "end": dates[-1], "initial_usd": INITIAL, "universe": TICKERS, "brk_removed": True, "asml_fx_usd_per_eur": ASML_FX, "buy_rule": "one eligible name=100%; multiple eligible names=equal weight; zero eligible names=cash/hold rule", "review": "month_end signal, next trading day execution", "cost_bps_per_side": COST_BPS, "cash_return": 0, "fundamental_proxy": True, "valuation_update": "COST dedicated model; ASML dedicated model with 15% moat premium; other names V1.5 rules"},
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
    draw_chart(series, OUT / "equity_curve.png")

    def row(label, stat):
        return f"| {label} | ${stat['ending_value']:,.2f} | {stat['total_return_pct']:.2f}% | {stat['cagr_pct']:.2f}% | {stat['annualized_vol_pct']:.2f}% | {stat['max_drawdown_pct']:.2f}% | {stat['sharpe_0rf']:.2f} |"
    report = [
        f"# 永续蓝筹策略{VERSION}：12股综合回测", "", f"区间：{dates[0]}至{dates[-1]}；起始资金：${INITIAL:,.0f}。", "",
        "## 回测结果", "", "| 组合 | 期末资产 | 累计收益 | 年化收益 | 年化波动 | 最大回撤 | 夏普（0无风险） |", "|---|---:|---:|---:|---:|---:|---:|",
        row(f"{VERSION}核心轮动", strategy_stats), row("12股等权买入持有", equal_stats), row("SPY买入持有", spy_stats), "",
        f"相对12股等权买入持有：年化收益差 {summary['difference']['vs_equal_cagr_pct_points']:+.2f}个百分点，最大回撤差 {summary['difference']['vs_equal_max_drawdown_pct_points']:+.2f}个百分点。",
        f"平均活跃标的 {strategy_stats['avg_names']:.2f}只，持仓交易日占比 {strategy_stats['invested_days_pct']:.1f}%，调仓事件 {len(events)}次。", "",
        "## 最新估值调整", "", f"COST使用专属EPS-DCF/历史PE/FCF收益率模型；ASML使用周期调整后EPS-DCF/历史PE/EV-EBIT模型，并加入15%护城河溢价。ASML按{ASML_FX:.2f}美元/欧元做估值换算；其他10家公司沿用V1.5行业估值规则。", "", 
        "## 数据边界", "", "财务数据使用决策日前已提交的SEC Company Facts，价格使用Yahoo复权收盘价，分红计入，现金收益按0%，单边换手成本5bp。CNI、TSM、ASML等跨市场披露存在币种/口径代理，非标准业务指标也用财务代理；因此这是可复核的近似回测，不是审计级历史重演。", "", "输出：summary.json、equity_curve.csv、monthly_signals.csv、rebalance_log.csv、equity_curve.png。",
    ]
    (OUT / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUT), "strategy": strategy_stats, "equal_weight_buy_hold": equal_stats, "spy_buy_hold": spy_stats, "events": len(events), "signals": len(signal_rows)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
