#!/usr/bin/env python3
"""V1.1 proxy backtest for the revised blue-chip rulebook.

The rulebook contains company-specific measures that are not consistently
available in a filing-date machine-readable history.  This runner uses the
same SEC/Yahoo cache as the earlier backtest, but fixes its key proxy issues:
company-specific growth and leverage bands, V1.1 gates, a ten-year growth
fade, and the hold buffer.  It is still a proxy implementation, not a manual
restatement of every bespoke metric.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

import backtest_bluechip_strategy_10y as base

ROOT = base.ROOT
OUT = ROOT / "reports/bluechip-strategy-10y-v11"
RAW = base.RAW
START, END, INITIAL, COST_BPS = base.START, base.END, base.INITIAL, base.COST_BPS
TICKERS = list(base.TICKERS)

# These are the V1.1 company caps from the manual, not portfolio weights.
GROWTH_CAPS = {'PG': .03, 'V': .10, 'MCD': .03, 'CNI': .04, 'DUK': .05,
               'AWK': .06, 'WM': .06, 'SPGI': .08, 'KO': .04, 'CME': .05,
               'COST': .08}
GROWTH_METRIC = {**{t: 'revenue' for t in TICKERS}, 'DUK': 'eps', 'AWK': 'eps'}
Q2_BANDS = {
    'PG': [(.03, 15), (.01, 10), (-.01, 5)], 'V': [(.07, 15), (.03, 10), (-.02, 5)],
    'MCD': [(.03, 15), (.01, 10), (-.02, 5)], 'CNI': [(.02, 15), (0, 10), (-.03, 5)],
    'DUK': [(.05, 15), (.03, 10), (-.01, 5)], 'AWK': [(.06, 15), (.03, 10), (-.01, 5)],
    'WM': [(.03, 15), (.01, 10), (-.01, 5)], 'SPGI': [(.05, 15), (.02, 10), (-.01, 5)],
    'KO': [(.02, 15), (0, 10), (-.02, 5)], 'CME': [(.04, 15), (0, 10), (-.03, 5)],
    'COST': [(.04, 15), (.02, 10), (-.01, 5)],
}
Q3_BANDS = {
    'PG': [(1.00, 15), (.80, 10), (.60, 5)], 'V': [(.90, 15), (.70, 10), (.50, 5)],
    'MCD': [(.80, 15), (.60, 10), (.40, 5)], 'CNI': [(.12, 15), (.08, 10), (.03, 5)],
    'WM': [(.10, 15), (.06, 10), (.02, 5)], 'SPGI': [(.90, 15), (.70, 10), (.50, 5)],
    'KO': [(.80, 15), (.60, 10), (.40, 5)], 'CME': [(.90, 15), (.70, 10), (.50, 5)],
    'COST': [(.02, 15), (.01, 10), (0, 5)],
}
Q4_LEVERAGE = {
    'PG': (2, 3, 4), 'V': (1, 2, 3), 'MCD': (4, 5, 6), 'CNI': (2.5, 3.3, 4),
    'DUK': (7.7, 9.0, 11.0), 'AWK': (8.0, 9.0, 11.0), 'WM': (3, 3.8, 4.5),
    'SPGI': (2, 3, 4), 'KO': (2.5, 3.5, 4.5), 'CME': (.5, 1.5, 2.5),
    'COST': (.5, 1.5, 2.5),
}


def build_financials_v11(facts: dict) -> dict[str, list[dict]]:
    aliases = dict(base.FACT_ALIASES)
    aliases['equity'] = ['StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest']
    out = {}
    for key, names in aliases.items():
        if key == 'equity':
            out[key] = base.annual_records(facts, names)
        elif key in {'revenue', 'operating_income', 'net_income', 'cfo', 'capex', 'eps', 'shares', 'dividend_per_share'}:
            out[key] = base.annual_records(facts, names)
        else:
            out[key] = base.instant_records(facts, names)
    return out


def latest_by_tag(rows: list[dict], when: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        if row['filed'] <= when and (row['tag'] not in result or (row['end'], row['filed']) > (result[row['tag']]['end'], result[row['tag']]['filed'])):
            result[row['tag']] = row
    return result


def balance_total(rows: list[dict], when: str) -> float | None:
    values = latest_by_tag(rows, when)
    if not values:
        return None
    return sum(item['val'] for item in values.values())


def margin_scores(years: list[dict]) -> tuple[int, float | None]:
    if len(years) < 2:
        return 0, None
    margins = [y['operating_income'] / y['revenue'] for y in years if y.get('revenue', 0)]
    if len(margins) < 2:
        return 0, None
    current = margins[-1]
    prior = sorted(margins[:-1])
    median = prior[len(prior) // 2]
    delta = (current - median) * 100
    score = 20 if delta >= -1 else 15 if delta >= -3 else 10 if delta >= -5 else 5 if delta >= -8 else 0
    return score, delta


def latest_and_median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None and math.isfinite(v)]
    if not values or values[-1] <= 0:
        return None
    ordered = sorted(values[-3:])
    median = ordered[len(ordered) // 2]
    return .5 * values[-1] + .5 * median if median > 0 else None


def fcf_per_share(years: list[dict], fin: dict[str, list[dict]], when: str) -> tuple[float | None, list[float]]:
    snapshot = latest_by_tag(fin['shares'], when)
    shares = next(iter(snapshot.values()), {}).get('val') if snapshot else None
    values = []
    for year in years[-3:]:
        share_count = year.get('shares') or shares
        if share_count and share_count < 1_000_000:
            share_count *= 1_000_000
        if share_count and year.get('cfo') is not None and year.get('capex') is not None:
            value = (year['cfo'] - year['capex']) / share_count
            if math.isfinite(value):
                values.append(value)
    return latest_and_median(values), values


def growth_value(years: list[dict], ticker: str) -> float | None:
    metric = GROWTH_METRIC[ticker]
    values = [y.get(metric) for y in years if y.get(metric) is not None and y.get(metric) > 0]
    if len(values) < 4:
        return None
    return base.cagr(values[-1], values[-4])


def dcf_value(c0: float | None, g: float | None, cap: float, r: float, terminal_cap: float = .025) -> float | None:
    if c0 is None or g is None or not math.isfinite(c0) or not math.isfinite(g):
        return None
    g = min(g, cap)
    gt = min(terminal_cap, g)
    if r <= gt:
        return None
    cashflows = []
    cashflow = c0
    for year in range(1, 11):
        if year <= 5:
            rate = g
        else:
            rate = g + (gt - g) * (year - 5) / 5
        # Each year's growth rate applies to the prior year's cash flow.
        # Applying a tapered rate to c0**year compounds the wrong sequence
        # once growth starts declining after year five.
        cashflow *= 1 + rate
        cashflows.append(cashflow)
    present = sum(value / (1 + r) ** year for year, value in enumerate(cashflows, 1))
    terminal = cashflows[-1] * (1 + gt) / (r - gt) / (1 + r) ** 10
    return present + terminal


def utility_value(years: list[dict], ticker: str, fin: dict[str, list[dict]], r: float) -> float | None:
    eps = [y.get('eps') for y in years if y.get('eps') is not None and y.get('eps') > 0]
    dividends = [y.get('dividend_per_share') for y in years if y.get('dividend_per_share') is not None]
    equity = [y.get('equity') for y in years if y.get('equity') is not None and y.get('equity') > 0]
    if len(eps) < 4 or len(dividends) < 3 or not equity:
        return None
    eps0 = latest_and_median(eps)
    g = base.cagr(eps[-1], eps[-4])
    if eps0 is None or g is None:
        return None
    div3 = sum(dividends[-3:])
    eps3 = sum(eps[-3:])
    p0 = div3 / eps3 if eps3 > 0 else None
    # SEC per-share dividend tags can be duplicated across units; reject only
    # clearly unusable ratios while keeping the rule transparent.
    if p0 is None or p0 < 0 or p0 > .95:
        return None
    roe = sum(y.get('net_income', 0) for y in years[-3:]) / sum(equity[-3:]) if len(equity) >= 3 and sum(equity[-3:]) > 0 else None
    if roe is None or roe <= 0:
        return None
    cap = GROWTH_CAPS[ticker]
    g = min(g, cap)
    gt = min(.025, g)
    p_terminal = min(.90, max(0.0, 1 - gt / roe))
    pv = 0.0
    for year in range(1, 11):
        rate = g if year <= 5 else g + (gt - g) * (year - 5) / 5
        eps_t = eps0 * (1 + rate) ** year
        payout = p0 if year <= 5 else p0 + (p_terminal - p0) * (year - 5) / 5
        pv += eps_t * payout / (1 + r) ** year
    eps10 = eps0 * (1 + g) ** 5 * (1 + gt) ** 5
    terminal_div = eps10 * (1 + gt) * p_terminal
    return pv + terminal_div / (r - gt) / (1 + r) ** 10 if r > gt else None


def score_v11(ticker: str, fin: dict[str, list[dict]], when: str, price: float, tnx: float, history: list[float]) -> dict:
    years = base.merge_annual(fin, when)
    if len(years) < 4:
        return {'eligible': False, 'hold': False, 'reason': 'insufficient_reported_annual_history'}
    q1, margin_delta = margin_scores(years)
    metric = GROWTH_METRIC[ticker]
    g = growth_value(years, ticker)
    q2 = base.score_band(g, Q2_BANDS[ticker]) if g is not None else 0
    if ticker in {'DUK', 'AWK'}:
        eps = [y.get('eps') for y in years[-3:] if y.get('eps') is not None]
        div = [y.get('dividend_per_share') for y in years[-3:] if y.get('dividend_per_share') is not None]
        payout = sum(div) / sum(eps) if eps and sum(eps) > 0 else None
        q3 = base.score_band(-payout, [(-.70, 15), (-.85, 10), (-.95, 5)]) if payout is not None else 0
        q4_metric = 'FFO/debt proxy'
    else:
        fcf_total = sum(y.get('cfo', 0) - y.get('capex', 0) for y in years[-3:] if y.get('cfo') is not None and y.get('capex') is not None)
        ni_total = sum(y.get('net_income', 0) for y in years[-3:])
        ratio = fcf_total / ni_total if ni_total > 0 else None
        q3 = base.score_band(ratio, Q3_BANDS[ticker]) if ratio is not None else 0
        q4_metric = 'net debt/EBITDA proxy'
    debt = balance_total(fin['debt'], when)
    cash = balance_total(fin['cash'], when)
    da = sum(y.get('da', 0) or 0 for y in years[-1:])
    ebitda = years[-1].get('operating_income', 0) + abs(da)
    leverage = (debt - cash) / ebitda if debt is not None and cash is not None and ebitda > 0 else None
    if ticker in {'DUK', 'AWK'}:
        # FFO/debt is not consistently tagged; use inverse leverage as a
        # transparent balance-sheet proxy and keep the score bands conservative.
        metric_value = (1 / leverage) if leverage and leverage > 0 else None
        q4 = base.score_band(metric_value, [(1 / Q4_LEVERAGE[ticker][0], 10), (1 / Q4_LEVERAGE[ticker][1], 7), (1 / Q4_LEVERAGE[ticker][2], 3)]) if metric_value else 0
    else:
        q4 = base.score_band(-leverage, [(-Q4_LEVERAGE[ticker][0], 10), (-Q4_LEVERAGE[ticker][1], 7), (-Q4_LEVERAGE[ticker][2], 3)]) if leverage is not None else 0
    Q = q1 + q2 + q3 + q4
    c0, fcf_values = fcf_per_share(years, fin, when)
    multiple = price / c0 if c0 and price > 0 else None
    if multiple is not None:
        below = sum(x < multiple for x in history)
        equal = sum(x == multiple for x in history)
        percentile = (below + .5 * equal) / len(history) if len(history) >= 8 else None
    else:
        percentile = None
    E = 20 if percentile is not None and percentile <= .30 else 15 if percentile is not None and percentile <= .50 else 10 if percentile is not None and percentile <= .70 else 5 if percentile is not None and percentile <= .90 else 0 if percentile is not None else None
    r = max(.08, tnx + .045)
    if ticker in {'DUK', 'AWK'}:
        FV = utility_value(years, ticker, fin, max(.075, tnx + .04))
    else:
        FV = dcf_value(c0, g, GROWTH_CAPS[ticker], r)
    A = 20 if FV and price <= .90 * FV else 15 if FV and price <= 1.05 * FV else 10 if FV and price <= 1.15 * FV else 5 if FV and price <= 1.25 * FV else 0 if FV else None
    S = Q + E + A if E is not None and A is not None else None
    risk_ok = q4 >= 3 and leverage is not None
    # Q4=3 is permitted only as a review status; the proxy cannot verify all
    # twelve-month financing facts, so it is marked for manual confirmation.
    review_flag = 'Q4_review' if q4 == 3 else ''
    short_history = percentile is None and Q >= 45 and A is not None and A >= 15 and risk_ok
    eligible = bool(risk_ok and ((S is not None and Q >= 40 and S >= 65 and A >= 5) or short_history))
    hold = bool(risk_ok and ((S is not None and Q >= 35 and S >= 55) or (percentile is None and Q >= 35 and A is not None and A >= 5)))
    if not risk_ok:
        reason = 'risk_gate'
    elif eligible:
        reason = 'eligible_short_history' if short_history else 'eligible'
    else:
        reason = ';'.join(x for x, ok in [('Q_below_40', Q < 40), ('S_below_65', S is not None and S < 65), ('valuation_missing', S is None), ('A_below_5', A is not None and A < 5), ('A_missing', A is None), ('E_history_short', percentile is None)] if ok)
    return {'eligible': eligible, 'hold': hold, 'reason': reason, 'Q': Q, 'q1': q1, 'q2': q2, 'q3': q3, 'q4': q4, 'E': E, 'A': A, 'S': S, 'leverage': leverage, 'growth': g, 'c0': c0, 'FV': FV, 'multiple': multiple, 'percentile': percentile, 'review_flag': review_flag, 'metric': metric, 'q4_metric': q4_metric}


def target_weights(current: dict[str, float], scores: dict[str, dict], fail_counts: dict[str, int], extreme_counts: dict[str, int]) -> tuple[dict[str, float], set[str], set[str]]:
    buy = {ticker for ticker, score in scores.items() if score.get('eligible')}
    retained = set()
    for ticker in current:
        score = scores[ticker]
        extreme = score.get('E') == 0 and score.get('A') == 0
        extreme_counts[ticker] = extreme_counts.get(ticker, 0) + 1 if extreme else 0
        if extreme_counts[ticker] >= 3:
            continue
        fail = not score.get('hold', False)
        fail_counts[ticker] = fail_counts.get(ticker, 0) + 1 if fail else 0
        if fail_counts[ticker] < 2:
            retained.add(ticker)
    active = buy | retained
    # A yellow name is frozen, while non-yellow active names split the balance.
    frozen = {ticker for ticker in current if scores[ticker].get('q4', 0) == 0}
    frozen_weights = {ticker: current[ticker] for ticker in frozen}
    remaining = max(0.0, 1.0 - sum(frozen_weights.values()))
    target = dict(frozen_weights)
    live = active - frozen
    if live:
        for ticker in live:
            target[ticker] = remaining / len(live)
    return target, active, frozen


def daily_metrics(dates, nav, daily_returns, exposure_weights):
    exposures = [set(weights) for weights in exposure_weights]
    return base.metrics(dates, nav, daily_returns, exposures)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    prices = {ticker: base.yahoo_chart(ticker) for ticker in TICKERS + ['SPY', '^TNX']}
    facts = {ticker: build_financials_v11(base.sec_facts(cik)) for ticker, (cik, _, _) in base.TICKERS.items()}
    dates = sorted(day for day in prices['SPY'] if START <= day <= END and all(day in prices[ticker] for ticker in TICKERS))
    month_ends = base.month_end_dates(dates)
    signal_at, signal_rows = {}, []
    history = defaultdict(list)
    current: dict[str, float] = {}
    fail_counts, extreme_counts = {}, {}
    for day in sorted(month_ends):
        tnx = prices['^TNX'].get(day, {}).get('close', 3.5) / 100
        scores = {}
        for ticker in TICKERS:
            scores[ticker] = score_v11(ticker, facts[ticker], day, prices[ticker][day]['close'], tnx, history[ticker])
            # V1.1 requires a minimum of eight comparable quarters for E.
            # Keep only March/June/September/December month-end observations
            # in the historical relative-valuation sample; the current score
            # can still be evaluated at each monthly review date.
            if scores[ticker].get('multiple') is not None and date.fromisoformat(day).month in {3, 6, 9, 12}:
                history[ticker].append(scores[ticker]['multiple'])
        target, active, frozen = target_weights(current, scores, fail_counts, extreme_counts)
        execution = base.first_next(dates, day)
        signal_at[execution] = {'signal_date': day, 'target': target, 'active': active, 'frozen': frozen, 'scores': scores} if execution else {'signal_date': day, 'target': target, 'active': active, 'frozen': frozen, 'scores': scores}
        signal_rows.append({'signal_date': day, 'execution_date': execution or '', 'buy_eligible': ';'.join(sorted(t for t in TICKERS if scores[t].get('eligible'))), 'active_hold_set': ';'.join(sorted(active)), 'frozen_yellow': ';'.join(sorted(frozen)), 'count_buy': sum(scores[t].get('eligible', False) for t in TICKERS), 'count_active': len(active), 'detail': json.dumps(scores, ensure_ascii=False)})
        current = target

    nav = INITIAL; strategy_nav = []; strategy_returns = []; weights: dict[str, float] = {}; exposure_weights = []; events = []
    prev_prices = {ticker: prices[ticker][dates[0]]['adjclose'] for ticker in TICKERS}
    for index, day in enumerate(dates):
        if index:
            daily_return = sum(weight * (prices[ticker][day]['adjclose'] / prev_prices[ticker] - 1) for ticker, weight in weights.items())
            nav *= 1 + daily_return
            strategy_returns.append(daily_return)
        if day in signal_at:
            target = signal_at[day]['target']
            turnover = sum(abs(target.get(ticker, 0) - weights.get(ticker, 0)) for ticker in TICKERS)
            cost = turnover * COST_BPS / 10000
            if target != weights:
                nav *= 1 - cost
                events.append({'signal_date': signal_at[day]['signal_date'], 'execution_date': day, 'old': ';'.join(sorted(weights)), 'new': ';'.join(sorted(target)), 'old_weights': json.dumps(weights, ensure_ascii=False), 'new_weights': json.dumps(target, ensure_ascii=False), 'turnover': turnover, 'cost': cost, 'active': ';'.join(sorted(signal_at[day]['active'])), 'frozen': ';'.join(sorted(signal_at[day]['frozen']))})
            weights = target
        strategy_nav.append(nav); exposure_weights.append(dict(weights))
        prev_prices = {ticker: prices[ticker][day]['adjclose'] for ticker in TICKERS}

    equal_nav = INITIAL; spy_nav = INITIAL; equal_series = []; spy_series = []
    prev_eq = {ticker: prices[ticker][dates[0]]['adjclose'] for ticker in TICKERS}; prev_spy = prices['SPY'][dates[0]]['adjclose']
    for day in dates:
        if day != dates[0]:
            equal_nav *= 1 + sum(prices[ticker][day]['adjclose'] / prev_eq[ticker] - 1 for ticker in TICKERS) / len(TICKERS)
            spy_nav *= prices['SPY'][day]['adjclose'] / prev_spy
        equal_series.append(equal_nav); spy_series.append(spy_nav)
        prev_eq = {ticker: prices[ticker][day]['adjclose'] for ticker in TICKERS}; prev_spy = prices['SPY'][day]['adjclose']
    series = [{'date': day, 'strategy_value': round(strategy_nav[i], 6), 'equal_weight_value': round(equal_series[i], 6), 'spy_value': round(spy_series[i], 6), 'active_count': len(exposure_weights[i]), 'active': ';'.join(sorted(exposure_weights[i]))} for i, day in enumerate(dates)]
    strategy_stats = daily_metrics(dates, strategy_nav, strategy_returns, exposure_weights)
    eq_stats = daily_metrics(dates, equal_series, [equal_series[i] / equal_series[i-1] - 1 for i in range(1, len(equal_series))], [dict.fromkeys(TICKERS, 1/len(TICKERS))] * len(equal_series))
    spy_stats = daily_metrics(dates, spy_series, [spy_series[i] / spy_series[i-1] - 1 for i in range(1, len(spy_series))], [{'SPY': 1}] * len(spy_series))
    summary = {'config': {'version': 'V1.1', 'start': START, 'end': END, 'initial_usd': INITIAL, 'buy_rule': 'Q>=40, Q4>=3, S>=65, A>=5, no yellow/red', 'hold_rule': 'Q>=35, S>=55, Q4>=3, two consecutive month failures to exit', 'allocation': 'one active name=100%; multiple active names=equal weight; zero=cash; yellow current name frozen', 'review': 'month_end; next_trading_day_execution', 'cost_bps_per_side': COST_BPS, 'cash_return': 0, 'fundamental_proxy': True}, 'data': {'tickers': TICKERS, 'trading_days': len(dates), 'start': dates[0], 'end': dates[-1], 'price_source': 'Yahoo Finance adjusted close', 'fundamental_source': 'SEC Company Facts at filing date', 'yield_source': 'Yahoo ^TNX proxy'}, 'strategy': strategy_stats, 'equal_weight_buy_hold': eq_stats, 'spy_buy_hold': spy_stats, 'difference': {'vs_equal_cagr_pct_points': strategy_stats['cagr_pct'] - eq_stats['cagr_pct'], 'vs_spy_cagr_pct_points': strategy_stats['cagr_pct'] - spy_stats['cagr_pct'], 'vs_equal_max_drawdown_pct_points': strategy_stats['max_drawdown_pct'] - eq_stats['max_drawdown_pct']}, 'events': events, 'signal_rows': signal_rows, 'series': series}
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    for filename, rows, fields in [('equity_curve.csv', series, list(series[0])), ('monthly_signals.csv', signal_rows, list(signal_rows[0])), ('rebalance_log.csv', events, list(events[0]) if events else ['signal_date', 'execution_date'])]:
        with (OUT / filename).open('w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    base.draw_chart(series, OUT / 'equity_curve.png')
    def row(label, stat):
        return f"| {label} | ${stat['ending_value']:,.2f} | {stat['total_return_pct']:.2f}% | {stat['cagr_pct']:.2f}% | {stat['annualized_vol_pct']:.2f}% | {stat['max_drawdown_pct']:.2f}% | {stat['sharpe_0rf']:.2f} |"
    report = ['# 永续蓝筹策略V1.1：十年回测（可回测近似版）', '', f'区间：{dates[0]}至{dates[-1]}；起始资金：${INITIAL:,.0f}。', '', '## 回测结果', '', '| 组合 | 期末资产 | 累计收益 | 年化收益 | 年化波动 | 最大回撤 | 夏普（0无风险） |', '|---|---:|---:|---:|---:|---:|---:|', row('V1.1动态等权', strategy_stats), row('11股等权买入持有', eq_stats), row('SPY买入持有', spy_stats), '', f"V1.1相对11股等权买入持有：年化收益差 {summary['difference']['vs_equal_cagr_pct_points']:+.2f}个百分点，最大回撤差 {summary['difference']['vs_equal_max_drawdown_pct_points']:+.2f}个百分点。", f"平均活跃标的 {strategy_stats['avg_names']:.2f}只，持仓交易日占比 {strategy_stats['invested_days_pct']:.1f}%，调仓事件 {len(events)}次。", '', '## 规则和数据边界', '', '1. 逐月用V1.1买入/持有线判定；新买为Q≥40、Q4≥3、S≥65、A≥5，取消独立V≥20与压力估值一票否决。', '2. 一只活跃标的100%，多只等权；老仓位按持有线保留两次月末观察；黄灯仓位冻结。', '3. 财务数据只读决策日前已提交的SEC Company Facts；价格用Yahoo Finance复权收盘价，包含分红；现金收益为0%，单边换手成本5bp。', '4. Visa支付量、COST续费率、DUK/AWK费基、MCD同店、CNI RTM、CME ADV等非标准历史指标使用文稿中说明的财务代理，故本结果是近似回测。CNI与其他股票的价格币种影响也未另加汇率收益。', '5. V1.1模型修正了上一轮代理中的错误增长上限和统一门槛，但仍需人工完整数据回测确认。', '', '输出：summary.json、equity_curve.csv、monthly_signals.csv、rebalance_log.csv、equity_curve.png。']
    (OUT / 'report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print(json.dumps({'strategy': strategy_stats, 'equal_weight_buy_hold': eq_stats, 'spy_buy_hold': spy_stats, 'events': len(events), 'signals': len(signal_rows), 'output': str(OUT)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
