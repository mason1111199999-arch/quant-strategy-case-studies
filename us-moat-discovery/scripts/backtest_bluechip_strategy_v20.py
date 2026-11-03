#!/usr/bin/env python3
"""V2.0: V1.9 moat-health overlay with LLY/VRTX and semi-monthly review.

The base company-specific quality/valuation rules are kept unchanged.  This
version adds a deliberately slow-moving, point-in-time health overlay using
financial leading proxies that exist consistently in the historical dataset:
revenue growth, operating-margin change, FCF trend, leverage and Q score.

M0 = normal; M1 = one or more warnings, so new buys are frozen; M2 = two
quarter-end confirmations with at least two warnings, so an existing position
is capped at half its prior weight; M3 = a structural/severe break, so the
position is exited.  This is a backtestable proxy layer, not a substitute for
manual review of customer retention, market share, order book or regulation.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, timedelta

import backtest_bluechip_strategy_v16 as v16
import backtest_bluechip_strategy_v17 as v17


ROOT = v16.ROOT
OUT = ROOT / "reports/bluechip-strategy-10y-v20"
VERSION = "V2.0"
BASE_SCORE = v17.score_v17
HEALTH_STATE: dict[str, dict] = {}
QUARTER_END_MONTHS = {3, 6, 9, 12}

# The dashboard now reviews every ten trading days (roughly half a month),
# rather than waiting for month-end.  This keeps the execution rule explicit
# and avoids pretending that a calendar-month signal is a daily signal.
REVIEW_TRADING_DAYS = 10
SPGI_MOBILITY_SEPARATION_DATE = "2026-07-01"
SPGI_POSTSPIN_FILING_DATE = "2026-10-28"

# Latest reported continuing-operations facts after the Mobility spin. These
# are refreshed when SPGI files its next quarterly report; market price and
# Treasury yield remain point-in-time inputs to the valuation calculation.
SPGI_POSTSPIN = {
    "revenue_2023_m": 11013.0,
    "revenue_2024_m": 12599.0,
    "revenue_2025_m": 13589.0,
    "fcf_9m_2026_m": 3299.0,
    "net_income_9m_2026_m": 3827.0,
    "diluted_shares_9m_2026_m": 294.9,
    "debt_2026_m": 13062.0,
    "cash_2026_m": 2441.0,
    "operating_profit_9m_2026_m": 5627.0,
    "disposition_gains_9m_2026_m": 386.0,
    "adjusted_eps_2026_low": 17.70,
    "adjusted_eps_2026_high": 17.95,
}


def spgi_postspin_score(price: float, tnx: float) -> dict:
    """Score SPGI using post-spin facts and expose the cross-check conflict.

    The legacy FCF model is preserved for comparability. Its annualized 9M
    continuing FCF is noisy, so the independent EPS-guidance cross-check is
    displayed and a large model gap blocks a new-entry signal instead of
    allowing a single fragile fair-value estimate to drive the rotation.
    """
    facts = SPGI_POSTSPIN
    revenue_growth = (facts["revenue_2025_m"] / facts["revenue_2023_m"]) ** 0.5 - 1
    fcf_conversion = facts["fcf_9m_2026_m"] / facts["net_income_9m_2026_m"]
    adjusted_operating_profit = facts["operating_profit_9m_2026_m"] - facts["disposition_gains_9m_2026_m"]
    annualized_adjusted_operating_profit = adjusted_operating_profit * 4 / 3
    net_debt = facts["debt_2026_m"] - facts["cash_2026_m"]
    leverage = net_debt / annualized_adjusted_operating_profit
    c0 = facts["fcf_9m_2026_m"] * 4 / 3 / facts["diluted_shares_9m_2026_m"]
    discount_rate = max(0.09, tnx + 0.05)
    dcf = v17.v16.v14.v11.dcf_value(c0, 0.07, 0.07, discount_rate)
    fcf_multiple_anchor = c0 * 28.0
    fair_value = 0.5 * dcf + 0.5 * fcf_multiple_anchor if dcf is not None else None
    ratio = price / fair_value if price and fair_value else None
    eps_mid = (facts["adjusted_eps_2026_low"] + facts["adjusted_eps_2026_high"]) / 2
    eps_scenario_low = eps_mid * 25.0
    eps_scenario_high = eps_mid * 28.0
    eps_scenario_mid = eps_mid * 26.5
    model_gap = abs(eps_scenario_mid / fair_value - 1) if fair_value else None

    q1, q2, q3, q4 = 20, 15, 10, 10
    quality = q1 + q2 + q3 + q4
    # E uses the strategy's neutral fallback because post-spin comparable
    # valuation history has not yet reached the model's eight-observation bar.
    historical_multiple_score = 10
    valuation_score = (
        20 if ratio is not None and ratio <= 0.90 else
        15 if ratio is not None and ratio <= 1.05 else
        10 if ratio is not None and ratio <= 1.20 else
        5 if ratio is not None and ratio <= 1.45 else
        0 if ratio is not None else None
    )
    total = quality + historical_multiple_score + valuation_score if valuation_score is not None else None
    hold = bool(quality >= 35 and ratio is not None and ratio <= 1.55 and q4 >= 3)
    conflict = model_gap is not None and model_gap >= 0.20

    current_pe = price / eps_mid if price and eps_mid else None
    note = (
        f"财务口径截至2026-09-30；当前价格${price:.2f}。Q={quality}/60：利润率20/20（3Q调整后经营利润率53.5%，同比+60bp；全年指引+75–100bp）；"
        f"持续经营收入增长15/15（2023–2025备考收入CAGR {revenue_growth:.1%}）；FCF转化10/15（9M FCF/持续经营净利润 {fcf_conversion:.1%}）；"
        f"杠杆10/10（净债务${net_debt/1000:.2f}bn / 年化剔除处置收益经营利润${annualized_adjusted_operating_profit/1000:.2f}bn = {leverage:.2f}x）。"
        f"E={historical_multiple_score}/20为历史估值中性占位（分拆后可比样本不足8期，不是实测百分位）；A={valuation_score}/20；S={total}/100。"
        f"持续经营9M FCF年化${c0:.2f}/股；FCF-DCF（增长上限7%、折现率{discount_rate:.2%}、终值增长2.5%）${dcf:.2f}，"
        f"与28x FCF锚${fcf_multiple_anchor:.2f}各半，模型FV${fair_value:.2f}，价格/FV {ratio:.2f}x。"
        f"独立交叉检验：2026调整后EPS指引中值${eps_mid:.3f}；按25–28x情景估值${eps_scenario_low:.2f}–${eps_scenario_high:.2f}，中点${eps_scenario_mid:.2f}，"
        f"当前约{current_pe:.1f}x指引EPS。EPS情景倍数是模型假设、不是公司指引。两模型中值差异{model_gap:.0%}，因此暂缓新增买入，等待后续持续经营FCF及可比估值历史积累。"
    )
    return {
        "eligible": False if conflict else bool(hold and valuation_score is not None and ratio <= 1.45),
        "primary": False,
        "reserve": False,
        "hold": hold,
        "reason": "估值交叉验证分歧较大：暂缓新增，现有仓位按健康度规则复核" if conflict else "SPGI post-spin valuation",
        # The existing dashboard recognizes this status and reveals the
        # accompanying valuation note in the SPGI row and holding card.
        "data_quality_status": "manual_review_required" if conflict else "postspin_reported",
        "valuation_note": note,
        "Q": quality, "q1": q1, "q2": q2, "q3": q3, "q4": q4,
        "E": historical_multiple_score, "A": valuation_score, "S": total,
        "leverage": leverage, "growth": revenue_growth, "margin_delta": 0.6,
        "c0": c0, "fcf_history": [c0], "FV": fair_value,
        "multiple": price / c0 if price and c0 else None,
        "percentile": None, "price_fv": ratio,
        "fv_method": "post-spin continuing FCF DCF + 28x FCF anchor; EPS-guidance cross-check",
        "metric": "annualized continuing FCF/share",
        "category": "information", "macro_status": "green" if tnx < .045 else "yellow" if tnx < .060 else "red",
        "spgi_eps_guidance_mid": eps_mid,
        "spgi_eps_scenario_low": eps_scenario_low,
        "spgi_eps_scenario_high": eps_scenario_high,
        "spgi_model_gap_pct": model_gap * 100 if model_gap is not None else None,
    }

TICKERS = [
    "PG", "V", "MCD", "WM", "KO", "COST", "MA", "CNI", "SPGI", "TSM", "ASML", "AAPL",
    "LLY", "VRTX",
]
UNIVERSE = TICKERS


def semi_month_dates(dates: list[str]) -> set[str]:
    """Return the last available trading day of each calendar half-month.

    The first signal is the last trading day on or before the 15th.  The
    second is the last trading day of a completed month.  A partial current
    month is intentionally not promoted to a new signal, so a daily refresh
    does not accidentally turn this into a daily strategy.
    """
    if not dates:
        return set()
    ordered = sorted(dates)
    by_month: dict[str, list[str]] = defaultdict(list)
    for day in ordered:
        by_month[day[:7]].append(day)
    output: set[str] = set()
    latest_month = ordered[-1][:7]
    latest_day = date.fromisoformat(ordered[-1]).day
    for month, values in by_month.items():
        first_half = [day for day in values if date.fromisoformat(day).day <= 15]
        # Do not create a signal for an incomplete current month before the
        # first half-month checkpoint has actually occurred.
        if first_half and not (month == latest_month and latest_day < 15):
            output.add(first_half[-1])
        last = date.fromisoformat(values[-1])
        if last.day >= 25 or (last + timedelta(days=1)).month != last.month:
            output.add(values[-1])
    if not output:
        output.add(ordered[0])
    return output


def install_v20_universe() -> None:
    """Extend V1.9's dictionaries without changing the older reports."""
    v14 = v16.v14
    v14.EXTRA_CIKS.update({
        "LLY": ("0000059478", "USD", 0.10),
        "VRTX": ("0000875320", "USD", 0.08),
    })
    v14.TICKERS = TICKERS
    v14.CATEGORY.update({"LLY": "pharma", "VRTX": "pharma"})
    v14.GROWTH_BANDS.update({
        "LLY": [(0.12, 15), (0.06, 10), (0.00, 5)],
        "VRTX": [(0.08, 15), (0.04, 10), (0.00, 5)],
    })
    v14.FCF_BANDS.update({
        "LLY": [(0.95, 15), (0.75, 10), (0.50, 5)],
        "VRTX": [(0.90, 15), (0.70, 10), (0.50, 5)],
    })
    v14.LEVERAGE_BANDS.update({"LLY": (1.5, 3.0, 4.5), "VRTX": (1.0, 2.0, 3.5)})
    v14.RATE_RULES["pharma"] = (0.095, 0.055)
    v14.TARGET_MULTIPLE.update({"LLY": 28.0, "VRTX": 24.0})
    v14.GROWTH_CAPS.update({"LLY": 0.10, "VRTX": 0.08})
    v14.PRIMARY_RATIO["pharma"] = 1.20
    v14.RESERVE_RATIO["pharma"] = 1.35
    # V1.5's entry bands are category-specific; pharma gets the same bounded
    # premium treatment as information/platform rather than an unlimited
    # growth-stock exception.
    import backtest_bluechip_strategy_v15 as v15_module
    v15_module.PRIMARY_RATIO["pharma"] = 1.25
    v15_module.RESERVE_RATIO["pharma"] = 1.45
    if not hasattr(v14, "_v20_original_build_financials"):
        v14._v20_original_build_financials = v14.build_financials_v14
    v14.build_financials_v14 = build_financials_v20
    v16.TICKERS = TICKERS
    v16.DEDICATED_TICKERS = {"COST", "ASML", "TSM"}
    v17.TICKERS = TICKERS
    # v16.main calls the base module's month-end helper.  Replace only for
    # this V2.0 process; V1.9 and earlier reports remain unchanged.
    v16.base.month_end_dates = semi_month_dates


def _latest_annual(rows: list[dict]) -> dict[str, dict]:
    """Keep the latest filing for each fiscal end in a derived series."""
    output: dict[str, dict] = {}
    for row in rows:
        end = row.get("end")
        if not end:
            continue
        if end not in output or row.get("filed", "") > output[end].get("filed", ""):
            output[end] = row
    return output


def build_financials_v20(ticker: str, facts: dict) -> dict[str, list[dict]]:
    """Add transparent fallbacks for LLY's changing SEC presentation tags."""
    v14 = v16.v14
    original = getattr(v14, "_v20_original_build_financials", v14.build_financials_v14)
    output = original(ticker, facts)
    if ticker != "LLY":
        return output

    # Lilly stopped reporting a consistently annual OperatingIncomeLoss tag in
    # the current Company Facts feed.  Pretax income minus non-operating
    # income/expense is a point-in-time operating-income proxy, with the same
    # filing date and fiscal end, so the model does not use future disclosures.
    if not output.get("operating_income"):
        pretax = _latest_annual(v14._extended_records(
            facts,
            ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
            True,
        ))
        nonop = _latest_annual(v14._extended_records(facts, ["NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"], True))
        derived = []
        for end, row in pretax.items():
            other = nonop.get(end)
            if other is None:
                continue
            derived.append({"end": end, "filed": max(row.get("filed", ""), other.get("filed", "")), "val": float(row["val"]) - float(other["val"]), "tag": "derived_operating_income", "unit": "USD"})
        output["operating_income"] = sorted(derived, key=lambda item: (item["end"], item["filed"]))

    # Current Lilly filings use PaymentsToAcquireOtherPropertyPlantAndEquipment
    # for capex rather than the generic tag used by older issuers.
    if not output.get("capex"):
        output["capex"] = v14._extended_records(
            facts,
            ["PaymentsToAcquireOtherPropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
            True,
        )
    return output

# Watch thresholds are intentionally moderate.  They are used only by the
# health overlay and do not replace the original valuation gates.
GROWTH_WATCH = {
    "PG": 0.00, "KO": 0.00, "MCD": 0.00, "COST": 0.02,
    "V": 0.03, "MA": 0.03, "SPGI": 0.03,
    "WM": 0.00, "CNI": 0.00,
    "TSM": -0.05, "ASML": -0.05, "AAPL": 0.02,
    "LLY": 0.05, "VRTX": 0.02,
}
GROWTH_SEVERE = {
    "PG": -0.04, "KO": -0.04, "MCD": -0.04, "COST": -0.03,
    "V": -0.03, "MA": -0.03, "SPGI": -0.03,
    "WM": -0.04, "CNI": -0.04,
    "TSM": -0.15, "ASML": -0.15, "AAPL": -0.06,
    "LLY": -0.10, "VRTX": -0.08,
}


def fcf_decay(ticker: str, score: dict) -> bool:
    """Flag a meaningful deterioration against the preceding FCF history."""
    raw_values = score.get("fcf_history") or []
    values = [float(v) for v in raw_values if v is not None]
    if len(values) < 3:
        # A missing/short series is a data-availability issue, not evidence of
        # moat decay.  The underlying Q score still handles insufficient data.
        return False
    previous = sorted(values[:-1])
    median_previous = previous[len(previous) // 2]
    if median_previous <= 0:
        return values[-1] < median_previous
    tolerance = 0.60 if ticker in {"TSM", "ASML"} else 0.75
    return values[-1] < median_previous * tolerance


def health_flags(ticker: str, score: dict) -> tuple[dict[str, str], int]:
    """Return warning flags and a count of severe warnings."""
    flags: dict[str, str] = {}
    growth = score.get("growth")
    margin_delta = score.get("margin_delta")
    leverage = score.get("leverage")
    q = score.get("Q")
    if growth is not None and growth < GROWTH_WATCH[ticker]:
        flags["growth"] = f"{growth:.1%} below watch threshold {GROWTH_WATCH[ticker]:.1%}"
    if margin_delta is not None and margin_delta < -3.0:
        flags["margin"] = f"operating-margin delta {margin_delta:.1f}pp"
    if fcf_decay(ticker, score):
        flags["fcf"] = "FCF proxy below recent-history tolerance"
    if score.get("q4", 0) == 0 and leverage is not None:
        flags["balance"] = f"leverage proxy {leverage:.2f} outside risk band"
    if q is not None and q < 40:
        flags["quality"] = f"Q score {q} below 40"

    severe = 0
    growth_severe = growth is not None and growth < GROWTH_SEVERE[ticker]
    margin_severe = margin_delta is not None and margin_delta < -6.0
    fcf_severe = fcf_decay(ticker, score)
    balance_severe = score.get("q4", 0) == 0 and leverage is not None
    # A weak multi-year growth proxy by itself is not a structural-break
    # signal: it can reflect a cycle, acquisition timing or a pandemic base.
    # Require corroboration from another operating or balance-sheet proxy.
    if growth_severe and (margin_delta is not None and margin_delta < -3.0 or fcf_severe or balance_severe):
        severe += 1
    if margin_severe:
        severe += 1
    # FCF deterioration is a useful watch signal, but on its own it can be
    # driven by working capital, acquisition timing or a temporary cycle.  It
    # may corroborate weak growth, but cannot by itself create an M3 exit.
    if balance_severe:
        severe += 1
    return flags, severe


def moat_health(ticker: str, score: dict, when: str) -> dict:
    """Update the slow health state only at quarter ends."""
    state = HEALTH_STATE.setdefault(ticker, {
        "last_quarter": None,
        "last_snapshot": None,
        "bad_quarters": 0,
        "severe_quarters": 0,
    })
    flags, severe = health_flags(ticker, score)
    current_quarter = when[:4] + "-" + when[5:7] if date.fromisoformat(when).month in QUARTER_END_MONTHS else None
    snapshot = json.dumps({
        "Q": score.get("Q"),
        "q1": score.get("q1"),
        "q2": score.get("q2"),
        "q3": score.get("q3"),
        "q4": score.get("q4"),
        "growth": score.get("growth"),
        "margin_delta": score.get("margin_delta"),
        "leverage": score.get("leverage"),
        "fcf_history": score.get("fcf_history"),
    }, sort_keys=True, default=str)
    # SEC annual/quarterly facts can remain unchanged across several monthly
    # signals.  Do not count the same financial snapshot as a new confirmation.
    snapshot_changed = False
    if current_quarter and current_quarter != state["last_quarter"] and snapshot != state["last_snapshot"]:
        snapshot_changed = True
        state["last_quarter"] = current_quarter
        state["last_snapshot"] = snapshot
        state["bad_quarters"] = state["bad_quarters"] + 1 if len(flags) >= 2 else 0
        state["severe_quarters"] = state["severe_quarters"] + 1 if severe >= 1 else 0
    hard_redline = severe >= 2 and (score.get("Q") or 0) < 30
    # M3 is a current confirmed break.  If later quarter-end evidence clears
    # the severe streak, the name can recover to M0/M1/M2 and be re-evaluated.
    if hard_redline or state["severe_quarters"] >= 2:
        status = "M3"
    elif state["bad_quarters"] >= 2:
        status = "M2"
    elif flags:
        status = "M1"
    else:
        status = "M0"
    health_score = max(0, 25 - 5 * len(flags) - 5 * severe)
    return {
        "moat_status": status,
        "moat_score": health_score,
        "moat_flags": flags,
        "moat_flag_count": len(flags),
        "moat_severe_count": severe,
        "moat_bad_quarters": state["bad_quarters"],
        "moat_severe_quarters": state["severe_quarters"],
        "moat_hard_redline": hard_redline,
        "moat_snapshot_changed": snapshot_changed,
    }


def score_v19(ticker: str, fin: dict, when: str, price: float, tnx: float, history: list[float]) -> dict:
    score = dict(BASE_SCORE(ticker, fin, when, price, tnx, history))
    # Keep the decision-date market price in the dashboard payload for every
    # company, not only the dedicated valuation models.
    score["price_usd"] = price
    if ticker == "SPGI" and SPGI_MOBILITY_SEPARATION_DATE <= when < SPGI_POSTSPIN_FILING_DATE:
        # The split-adjusted continuing-operations facts were not available to
        # the strategy before the October 28 filing. Keep this historical
        # interval marked for review instead of backfilling future facts.
        score.update({
            "eligible": False,
            "primary": False,
            "reserve": False,
            "hold": True,
            "reason": "SPGI Mobility 分拆后、首份可比持续经营报告提交前：暂停新增；既有仓位不因口径缺口自动退出",
            "data_quality_status": "manual_review_required",
            "valuation_note": "Mobility 于 2026-07-01 分拆；策略只从 2026-10-28 提交的持续经营季度报告开始恢复估值，避免将之后的财务数据倒灌至历史信号。",
            "Q": None,
            "q1": None,
            "q2": None,
            "q3": None,
            "q4": 0,
            "E": None,
            "A": None,
            "S": None,
            "leverage": None,
            "growth": None,
            "margin_delta": None,
            "c0": None,
            "fcf_history": [],
            "FV": None,
            "multiple": None,
            "percentile": None,
            "price_fv": None,
            "fv_method": "等待分拆后持续经营现金流数据",
        })
    elif ticker == "SPGI" and when >= SPGI_POSTSPIN_FILING_DATE:
        score.update(spgi_postspin_score(price, tnx))
    health = moat_health(ticker, score, when)
    score.update(health)
    status = health["moat_status"]
    if status == "M1":
        score.update({"eligible": False, "primary": False, "reserve": False, "reason": "moat_health_watch"})
    elif status == "M2":
        score.update({"eligible": False, "primary": False, "reserve": False, "hold": False, "reason": "moat_decay_confirmed"})
    elif status == "M3":
        score.update({"eligible": False, "primary": False, "reserve": False, "hold": False, "reason": "moat_structural_break"})
    if ticker == "SPGI" and when >= SPGI_POSTSPIN_FILING_DATE and score.get("spgi_model_gap_pct", 0) >= 20:
        # A populated score is shown, but conflicting valuation methods do not
        # create an automatic entry signal. Existing positions still follow
        # the same moat-health and hold rules as other holdings.
        score.update({"eligible": False, "primary": False, "reserve": False})
        score["reason"] = "估值交叉验证分歧较大：暂缓新增，现有仓位按健康度规则复核"
    return score


def weighted_target_weights_v19(current, scores, fail_counts, extreme_counts):
    """V1.8 tiered sizing plus M2 cap and M3 exit."""
    buy = {ticker for ticker, score in scores.items() if score.get("eligible")}
    retained = set()
    for ticker in current:
        score = scores[ticker]
        extreme = score.get("E") == 0 and score.get("A") == 0
        extreme_counts[ticker] = extreme_counts.get(ticker, 0) + 1 if extreme else 0
        if extreme_counts[ticker] >= 3 or score.get("moat_status") == "M3":
            continue
        fail = not score.get("hold", False)
        fail_counts[ticker] = fail_counts.get(ticker, 0) + 1 if fail else 0
        if fail_counts[ticker] < 2:
            retained.add(ticker)
    active = buy | retained
    frozen = {
        ticker for ticker in current
        if scores[ticker].get("q4", 0) == 0 and scores[ticker].get("moat_status") not in {"M2", "M3"}
    }
    frozen_weights = {ticker: current[ticker] for ticker in frozen}
    remaining = max(0.0, 1.0 - sum(frozen_weights.values()))
    live = active - frozen
    if not live:
        return frozen_weights, active, frozen

    units = {}
    for ticker in live:
        score = scores[ticker]
        units[ticker] = 1.2 if score.get("primary") else 1.0 if score.get("reserve") else 0.8
    total_units = sum(units.values())
    target = dict(frozen_weights)
    for ticker, unit in units.items():
        target[ticker] = remaining * unit / total_units

    # M2 is a confirmed decay signal: reduce the existing position to at most
    # half its previous weight and redistribute the released capital.
    caps = {ticker: current[ticker] * 0.5 for ticker in live if scores[ticker].get("moat_status") == "M2" and ticker in current}
    for _ in range(12):
        excess = sum(max(0.0, target[ticker] - cap) for ticker, cap in caps.items())
        for ticker, cap in caps.items():
            target[ticker] = min(target[ticker], cap)
        if excess <= 1e-12:
            break
        receivers = [ticker for ticker in live if ticker not in caps]
        if not receivers:
            break
        receiver_units = sum(units[ticker] for ticker in receivers)
        for ticker in receivers:
            target[ticker] += excess * units[ticker] / receiver_units
    return target, active, frozen


def write_moat_health_log(path) -> None:
    source = OUT / "monthly_signals.csv"
    rows = []
    with source.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            scores = json.loads(row["detail"])
            for ticker, score in scores.items():
                rows.append({
                    "signal_date": row["signal_date"],
                    "ticker": ticker,
                    "moat_status": score.get("moat_status", "M0"),
                    "moat_score": score.get("moat_score", 25),
                    "moat_flag_count": score.get("moat_flag_count", 0),
                    "moat_severe_count": score.get("moat_severe_count", 0),
                    "moat_bad_quarters": score.get("moat_bad_quarters", 0),
                    "moat_severe_quarters": score.get("moat_severe_quarters", 0),
                    "moat_flags": json.dumps(score.get("moat_flags", {}), ensure_ascii=False),
                })
    fields = list(rows[0]) if rows else ["signal_date", "ticker"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    install_v20_universe()
    HEALTH_STATE.clear()
    v16.OUT = OUT
    v16.VERSION = VERSION
    v16.DEDICATED_TICKERS = {"COST", "ASML", "TSM"}
    v16.score_v16 = score_v19
    v16.v14.target_weights = weighted_target_weights_v19
    v16.main()

    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["config"]["version"] = "V2.0-moat-health-semi-monthly"
    summary["config"]["universe"] = TICKERS
    summary["config"]["review"] = "每半个月复核（上半月末与月末交易日），下一交易日执行"
    summary["config"]["review_trading_days"] = REVIEW_TRADING_DAYS
    summary["config"]["valuation_update"] = "COST专属EPS-DCF/历史PE/FCF收益率模型；ASML专属周期调整后EPS-DCF/历史PE/EV-EBIT模型+15%护城河溢价；TSM专属周期EPS估值+10%护城河溢价；LLY/VRTX使用医药行业DCF+FCF倍数代理；其他公司沿用V1.5行业估值规则"
    summary["config"]["moat_health_overlay"] = {
        "M0": "无健康度预警",
        "M1": "出现一个或多个财务领先代理预警，冻结新买入",
        "M2": "连续两个季末出现至少两个预警，现有仓位上限为上一期的一半，并进入原有两次失败退出缓冲",
        "M3": "连续两个季末出现严重预警，或触发极端质量/杠杆断裂，立即退出",
        "proxy_metrics": "收入增长趋势、经营利润率变化、FCF历史趋势、杠杆风险、Q质量分",
        "manual_operating_metrics": "客户留存/市场份额/订单簿/开发者生态/监管等尚未进入历史回测，保留为人工复核项",
    }
    summary["config"]["position_sizing"] = "V1.8分层仓位：主买入1.2、储备买入1.0、仅满足持有0.8并归一化；M2现有仓位最多保留上一期一半；M3立即退出"
    summary["data"]["moat_health_source"] = "SEC Company Facts历史财务数据构造的领先代理；非标准经营指标暂未有一致的历史时点数据"
    summary["data"]["moat_confirmation_basis"] = "仅在季度末且财务健康快照发生变化时计入一次连续确认，避免重复使用同一年度披露；估值与入池信号每半个月复核"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_moat_health_log(OUT / "moat_health_log.csv")

    report_path = OUT / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("V1.9 comprehensive backtest", "V2.0 moat-health semi-monthly backtest")
    report = report.replace(
        "COST使用专属EPS-DCF/历史PE/FCF收益率模型；ASML使用周期调整后EPS-DCF/历史PE/EV-EBIT模型，并加入15%护城河溢价。ASML按1.10美元/欧元做估值换算；其他10家公司沿用V1.5行业估值规则。",
        "COST使用专属EPS-DCF/历史PE/EV-EBIT模型；ASML使用周期调整后EPS-DCF/历史PE/EV-EBIT模型并加入15%护城河溢价；TSM使用周期EPS估值并加入10%护城河溢价；LLY/VRTX使用医药行业DCF+FCF倍数代理。ASML按1.10美元/欧元做估值换算；其余公司沿用V1.5行业估值规则。",
    )
    report += (
        "\n\n## V2.0护城河健康度与半月复核\n\n"
        "V2.0保留V1.8的行业专属估值、Q/E/A评分和分层仓位，并新增 LLY、VRTX 两个医药护城河候选。估值与入池信号每半个月复核（上半月末与月末交易日），下一交易日执行；健康度状态仍只在季度末的新财务快照上累计确认。\n\n"
        "M1出现财务领先代理预警时冻结新买入；M2连续两个季末确认至少两个预警时，将现有仓位压到上一期的一半；M3连续两个季末出现严重预警或触发硬红线时退出。\n\n"
        "历史回测中的健康度指标是收入增长、经营利润率变化、FCF趋势、杠杆和Q分的财务代理，客户留存、市场份额、订单簿、开发者生态等经营指标尚未具备一致的历史时点数据，需在实盘看板中人工补充。\n"
    )
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps({
        "output": str(OUT),
        "strategy": summary["strategy"],
        "equal_weight_buy_hold": summary["equal_weight_buy_hold"],
        "spy_buy_hold": summary["spy_buy_hold"],
        "events": len(summary["events"]),
        "moat_health_log": str(OUT / "moat_health_log.csv"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
