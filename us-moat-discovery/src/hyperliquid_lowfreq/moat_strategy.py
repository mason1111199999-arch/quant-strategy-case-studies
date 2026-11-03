from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any


UNIVERSE = ["PG", "V", "MCD", "WM", "KO", "COST", "MA", "CNI", "SPGI", "TSM", "ASML", "AAPL", "LLY", "VRTX"]
DISPLAY_NAMES = {
    "PG": "宝洁",
    "V": "Visa",
    "MCD": "麦当劳",
    "WM": "WM废物管理",
    "KO": "可口可乐",
    "COST": "Costco",
    "MA": "Mastercard",
    "CNI": "加拿大国家铁路",
    "SPGI": "标普全球",
    "TSM": "台积电",
    "ASML": "阿斯麦",
    "AAPL": "苹果",
    "LLY": "礼来",
    "VRTX": "Vertex 制药",
}


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _split(value: Any) -> list[str]:
    return [item for item in str(value or "").split(";") if item]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _drawdown(values: list[float]) -> tuple[float, str | None]:
    peak = values[0] if values else 0.0
    worst = 0.0
    worst_date: str | None = None
    for value in values:
        peak = max(peak, value)
        current = value / peak - 1.0 if peak else 0.0
        if current < worst:
            worst = current
    return worst, worst_date


def _cagr(nav: float, start: date, end: date) -> float:
    years = max((end - start).days / 365.25, 1 / 365.25)
    return nav ** (1 / years) - 1 if nav > 0 else 0.0


def _metric_block(points: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [float(point[key]) for point in points]
    first = values[0]
    normalized = [value / first for value in values]
    max_drawdown, _ = _drawdown(normalized)
    start = date.fromisoformat(points[0]["date"])
    end = date.fromisoformat(points[-1]["date"])
    nav = normalized[-1]
    return {
        "ending_nav": nav,
        "ending_value_10000": nav * 10000,
        "total_return_pct": (nav - 1) * 100,
        "cagr_pct": _cagr(nav, start, end) * 100,
        "max_drawdown_pct": max_drawdown * 100,
    }


def _window_rows(points: list[dict[str, Any]], years: int) -> list[dict[str, Any]]:
    """Return a trailing calendar-year window without changing source grain."""
    if not points:
        return []
    end = date.fromisoformat(points[-1]["date"])
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        # Feb 29 is not expected in the daily close feed, but keep the helper
        # deterministic if a leap-day endpoint is ever supplied.
        start = end.replace(month=2, day=28, year=end.year - years)
    window = [point for point in points if date.fromisoformat(point["date"]) >= start]
    return window if len(window) >= 2 else points


def _parse_detail(raw: str) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _health_action(status: str, held: bool = False) -> str:
    if status == "M1":
        return "冻结新买入；" + ("现有仓位继续观察" if held else "暂不新买入")
    if status == "M2":
        return "现有仓位最多保留上一期一半"
    if status == "M3":
        return "退出，不新建仓位"
    return "正常复核"


def _health_recovery_rows(signal_rows: list[dict[str, str]], report_dir: Path) -> list[dict[str, Any]]:
    """Find M3 -> recovery transitions and any later real re-entry."""
    details = [(row.get("signal_date", ""), _parse_detail(row.get("detail", ""))) for row in signal_rows]
    rebalance_rows = _read_csv(report_dir / "rebalance_log.csv") if (report_dir / "rebalance_log.csv").exists() else []
    output: list[dict[str, Any]] = []
    for ticker in UNIVERSE:
        index = 0
        while index < len(details):
            signal_date, detail = details[index]
            if (detail.get(ticker) or {}).get("moat_status", "M0") != "M3":
                index += 1
                continue
            m3_start = signal_date
            m3_end_index = index
            while m3_end_index + 1 < len(details):
                next_detail = details[m3_end_index + 1][1].get(ticker) or {}
                if next_detail.get("moat_status", "M0") != "M3":
                    break
                m3_end_index += 1
            recovery_index = m3_end_index + 1
            next_m3_index = recovery_index
            while next_m3_index < len(details):
                if (details[next_m3_index][1].get(ticker) or {}).get("moat_status", "M0") == "M3":
                    break
                next_m3_index += 1
            if recovery_index >= len(details):
                output.append({
                    "ticker": ticker,
                    "name": DISPLAY_NAMES.get(ticker, ticker),
                    "m3_start": m3_start,
                    "m3_end": details[m3_end_index][0],
                    "recovery_date": None,
                    "recovery_status": "M3",
                    "reentry_signal_date": None,
                    "reentry_execution_date": None,
                    "reentry_status": "尚未恢复",
                })
                break

            recovery_date, recovery_detail = details[recovery_index]
            full_recovery_index = next((
                candidate for candidate in range(recovery_index, next_m3_index)
                if (details[candidate][1].get(ticker) or {}).get("moat_status", "M0") == "M0"
            ), None)
            reentry = None
            if full_recovery_index is not None:
                full_recovery_date = details[full_recovery_index][0]
                for event in rebalance_rows:
                    # The rebalance signal on the first fully recovered snapshot
                    # is actionable on the following execution date, so include
                    # the recovery signal date itself in the search window.
                    if not (full_recovery_date <= event.get("signal_date", "") < (details[next_m3_index][0] if next_m3_index < len(details) else "9999-99-99")):
                        continue
                    old = set(_split(event.get("old")))
                    new = set(_split(event.get("new")))
                    if ticker in new - old:
                        reentry = event
                        break
            output.append({
                "ticker": ticker,
                "name": DISPLAY_NAMES.get(ticker, ticker),
                "m3_start": m3_start,
                "m3_end": details[m3_end_index][0],
                "recovery_date": recovery_date,
                "recovery_status": (recovery_detail.get(ticker) or {}).get("moat_status", "M0"),
                "full_recovery_date": details[full_recovery_index][0] if full_recovery_index is not None else None,
                "reentry_signal_date": reentry.get("signal_date") if reentry else None,
                "reentry_execution_date": reentry.get("execution_date") if reentry else None,
                "reentry_status": "已重新纳入" if reentry else "尚未重新纳入",
            })
            index = recovery_index
    return output


def _latest_rebalance(report_dir: Path) -> dict[str, Any]:
    path = report_dir / "rebalance_log.csv"
    if not path.exists():
        return {}
    rows = _read_csv(path)
    if not rows:
        return {}
    row = rows[-1]
    try:
        weights = json.loads(row.get("new_weights") or "{}")
    except (TypeError, ValueError):
        weights = {}
    return {"row": row, "weights": weights if isinstance(weights, dict) else {}}


def _ticker_row(
    ticker: str,
    latest_detail: dict[str, Any],
    signal_rows: list[dict[str, str]],
    latest_signal: dict[str, str],
    current_weights: dict[str, Any],
) -> dict[str, Any]:
    detail = latest_detail.get(ticker) or {}
    latest_primary = ticker in _split(latest_signal.get("primary"))
    latest_reserve = ticker in _split(latest_signal.get("reserve"))
    latest_selected = ticker in _split(latest_signal.get("selected"))
    if latest_primary:
        status = "主买入"
    elif latest_reserve:
        status = "储备买入"
    elif ticker in current_weights or latest_selected:
        status = "持有/观察"
    else:
        status = "观察"

    primary_count = sum(ticker in _split(row.get("primary")) for row in signal_rows)
    reserve_count = sum(ticker in _split(row.get("reserve")) for row in signal_rows)
    selected_count = sum(ticker in _split(row.get("selected")) for row in signal_rows)
    price = detail.get("price_usd")
    if price is None:
        price = detail.get("price_usd_adr")
    fair_value = detail.get("fv_with_moat_premium")
    if fair_value is None:
        fair_value = detail.get("FV")
    price_fv = detail.get("price_fv_with_moat_premium")
    if price_fv is None:
        price_fv = detail.get("price_fv")
    premium = detail.get("moat_premium")
    if premium is None:
        premium = detail.get("v16_moat_premium")
    if premium is None:
        premium = detail.get("v17_moat_premium")
    moat_status = detail.get("moat_status") or "M0"
    moat_flags = detail.get("moat_flags") or {}
    if not isinstance(moat_flags, dict):
        moat_flags = {"warning": str(moat_flags)}
    current_weight_pct = (_number(current_weights.get(ticker, 0)) or 0) * 100

    return {
        "ticker": ticker,
        "name": DISPLAY_NAMES.get(ticker, ticker),
        "status": status,
        "current_weight_pct": current_weight_pct,
        "Q": detail.get("Q"),
        "price": _number(price),
        "fair_value": _number(fair_value),
        "price_fv": _number(price_fv),
        "moat_premium_pct": ((_number(premium) or 1) - 1) * 100 if premium is not None else 0,
        "fv_method": detail.get("fv_method") or detail.get("method") or "行业专属估值模型",
        "reason": detail.get("reason") or "—",
        "data_quality_status": detail.get("data_quality_status"),
        "valuation_note": detail.get("valuation_note"),
        "primary_count": primary_count,
        "reserve_count": reserve_count,
        "selected_count": selected_count,
        "latest_primary": latest_primary,
        "latest_reserve": latest_reserve,
        "latest_selected": latest_selected,
        "moat_status": moat_status,
        "moat_score": detail.get("moat_score"),
        "moat_flags": moat_flags,
        "moat_flag_names": list(moat_flags),
        "moat_flag_count": detail.get("moat_flag_count", len(moat_flags)),
        "moat_severe_count": detail.get("moat_severe_count", 0),
        "moat_bad_quarters": detail.get("moat_bad_quarters", 0),
        "moat_severe_quarters": detail.get("moat_severe_quarters", 0),
        "moat_action": _health_action(moat_status, current_weight_pct > 0),
        "moat_hold_ok": bool(detail.get("hold")),
    }


def moat_strategy_snapshot(report_root: Path) -> dict[str, Any]:
    """Return the read-only V2.0 moat-rotation dashboard payload.

    The backtest is stored under the existing report directory. The UI only
    receives the latest five years of the normalized curve, while the signal
    counts and current holdings remain auditable from the same report files.
    """
    report_root = Path(report_root)
    report_candidates = [
        report_root / "bluechip-strategy-10y-v20",
        report_root.parent / "bluechip-strategy-10y-v20",
        report_root / "bluechip-strategy-10y-v19",
        report_root.parent / "bluechip-strategy-10y-v19",
        report_root / "bluechip-strategy-10y-v18",
        report_root.parent / "bluechip-strategy-10y-v18",
    ]
    report_dir = next((candidate for candidate in report_candidates if candidate.exists()), report_candidates[0])
    summary_path = report_dir / "summary.json"
    curve_path = report_dir / "equity_curve.csv"
    signal_path = report_dir / "monthly_signals.csv"
    if not summary_path.exists() or not curve_path.exists() or not signal_path.exists():
        return {
            "ok": False,
            "error": f"护城河策略回测文件暂不可用：{report_dir}",
            "source": str(report_dir),
        }

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        curve_rows = _read_csv(curve_path)
        signal_rows = _read_csv(signal_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"读取护城河策略回测文件失败：{exc}", "source": str(report_dir)}

    if len(curve_rows) < 2 or not signal_rows:
        return {"ok": False, "error": "护城河策略回测数据不足", "source": str(report_dir)}

    parsed_curve: list[dict[str, Any]] = []
    for row in curve_rows:
        try:
            date.fromisoformat(row["date"])
            parsed_curve.append(
                {
                    "date": row["date"],
                    "strategy_value": float(row["strategy_value"]),
                    "equal_weight_value": float(row["equal_weight_value"]),
                    "spy_value": float(row["spy_value"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if len(parsed_curve) < 2:
        return {"ok": False, "error": "护城河策略净值曲线无有效数据", "source": str(report_dir)}

    curve_5y = _window_rows(parsed_curve, 5)
    curve_3y = _window_rows(parsed_curve, 3)
    anchors = {key: curve_5y[0][key] for key in ("strategy_value", "equal_weight_value", "spy_value")}
    points = []
    for row in curve_5y:
        points.append(
            {
                "date": row["date"],
                "strategy_nav": row["strategy_value"] / anchors["strategy_value"],
                "equal_nav": row["equal_weight_value"] / anchors["equal_weight_value"],
                "spy_nav": row["spy_value"] / anchors["spy_value"],
            }
        )

    curve_metrics = {
        "strategy": _metric_block(curve_5y, "strategy_value"),
        "equal_weight": _metric_block(curve_5y, "equal_weight_value"),
        "spy": _metric_block(curve_5y, "spy_value"),
    }
    curve_metrics_3y = {
        "strategy": _metric_block(curve_3y, "strategy_value"),
        "equal_weight": _metric_block(curve_3y, "equal_weight_value"),
        "spy": _metric_block(curve_3y, "spy_value"),
    }

    latest_signal = signal_rows[-1]
    latest_detail = _parse_detail(latest_signal.get("detail", ""))
    rebalance = _latest_rebalance(report_dir)
    current_weights = rebalance.get("weights", {})
    current_positions: list[dict[str, Any]] = []
    primary = set(_split(latest_signal.get("primary")))
    reserve = set(_split(latest_signal.get("reserve")))
    for ticker, raw_weight in sorted(current_weights.items(), key=lambda item: float(item[1]), reverse=True):
        weight = _number(raw_weight) or 0
        if ticker in primary:
            role, unit = "主买入", 1.2
        elif ticker in reserve:
            role, unit = "储备买入", 1.0
        else:
            role, unit = "持有", 0.8
        position_detail = latest_detail.get(ticker) or {}
        data_quality_status = position_detail.get("data_quality_status")
        current_positions.append(
            {
                "ticker": ticker,
                "name": DISPLAY_NAMES.get(ticker, ticker),
                "role": role,
                "weight_pct": weight * 100,
                "unit": unit,
                "moat_status": position_detail.get("moat_status", "M0"),
                "moat_score": position_detail.get("moat_score"),
                "moat_flags": position_detail.get("moat_flags", {}),
                "moat_action": _health_action(position_detail.get("moat_status", "M0"), True),
                "hold_ok": bool(position_detail.get("hold")),
                "data_quality_status": data_quality_status,
                "valuation_note": position_detail.get("valuation_note"),
                "valuation_action": (
                    "估值口径待复核；暂停新增，现有仓位不因数据缺口自动退出"
                    if data_quality_status == "manual_review_required"
                    else None
                ),
            }
        )

    tickers = [_ticker_row(ticker, latest_detail, signal_rows, latest_signal, current_weights) for ticker in UNIVERSE]
    health_counts = {status: sum(row["moat_status"] == status for row in tickers) for status in ("M0", "M1", "M2", "M3")}
    health_alerts = [row for row in tickers if row["moat_status"] != "M0"]
    active_health_alerts = [row for row in health_alerts if row["current_weight_pct"] > 0]
    recovery_rows = _health_recovery_rows(signal_rows, report_dir)
    recent_signals = []
    for row in reversed(signal_rows[-8:]):
        detail = _parse_detail(row.get("detail", ""))
        health_alerts_for_signal = []
        for ticker, score in detail.items():
            status = score.get("moat_status", "M0")
            if status != "M0":
                flags = score.get("moat_flags") or {}
                health_alerts_for_signal.append({
                    "ticker": ticker,
                    "status": status,
                    "flags": list(flags) if isinstance(flags, dict) else [str(flags)],
                })
        recent_signals.append(
            {
                "signal_date": row.get("signal_date"),
                "execution_date": row.get("execution_date"),
                "primary": _split(row.get("primary")),
                "reserve": _split(row.get("reserve")),
                "selected": _split(row.get("selected")),
                "frozen": _split(row.get("frozen")),
                "health_alerts": health_alerts_for_signal,
            }
        )

    strategy = summary.get("strategy", {})
    return {
        "ok": True,
        "version": summary.get("config", {}).get("version", "V2.0-moat-health-semi-monthly"),
        "as_of": latest_signal.get("signal_date") or parsed_curve[-1]["date"],
        "execution_date": latest_signal.get("execution_date"),
        "backtest_start": summary.get("config", {}).get("start"),
        "backtest_end": summary.get("config", {}).get("end"),
        "universe": UNIVERSE,
        "current_positions": current_positions,
        "current_position_count": len(current_positions),
        "latest_signal": {
            "primary": _split(latest_signal.get("primary")),
            "reserve": _split(latest_signal.get("reserve")),
            "selected": _split(latest_signal.get("selected")),
            "frozen": _split(latest_signal.get("frozen")),
            "health_alerts": [
                {
                    "ticker": row["ticker"],
                    "status": row["moat_status"],
                    "flags": row["moat_flag_names"],
                    "action": row["moat_action"],
                }
                for row in health_alerts
            ],
        },
        "tickers": tickers,
        "health_summary": {
            "counts": health_counts,
            "alert_count": len(health_alerts),
            "active_alert_count": len(active_health_alerts),
            "active_alerts": [
                {"ticker": row["ticker"], "status": row["moat_status"], "action": row["moat_action"], "flags": row["moat_flag_names"]}
                for row in active_health_alerts
            ],
            "m2_m3_count": health_counts["M2"] + health_counts["M3"],
        },
        "health_recoveries": recovery_rows,
        "recent_signals": recent_signals,
        "curve": {
            "start": points[0]["date"],
            "end": points[-1]["date"],
            "observations": len(points),
            "points": points,
            "metrics": curve_metrics,
            "metrics_3y": curve_metrics_3y,
        },
        "backtest": {
            "initial_usd": summary.get("config", {}).get("initial_usd", 10000),
            "ending_value_10y": strategy.get("ending_value"),
            "cagr_10y_pct": strategy.get("cagr_pct"),
            "max_drawdown_10y_pct": strategy.get("max_drawdown_pct"),
            "ending_value_5y": curve_metrics["strategy"]["ending_value_10000"],
            "cagr_5y_pct": curve_metrics["strategy"]["cagr_pct"],
            "max_drawdown_5y_pct": curve_metrics["strategy"]["max_drawdown_pct"],
            "ending_value_3y": curve_metrics_3y["strategy"]["ending_value_10000"],
            "cagr_3y_pct": curve_metrics_3y["strategy"]["cagr_pct"],
            "max_drawdown_3y_pct": curve_metrics_3y["strategy"]["max_drawdown_pct"],
        },
        "rules": [
            "每半个月复核（上半月末与月末交易日），下一交易日执行；一个合格标的满仓，多标的按主买入1.2、储备买入1.0、持有0.8单位归一化分配。",
            "当前持仓只要仍在持有规则内可以保留；连续两次失败才退出，避免半月信号噪声导致频繁交易。",
            "M1：出现一个或多个财务领先代理预警，冻结新买入；若仍满足持有条件，现有仓位继续观察。",
            "M2：连续两个季末且财务快照发生新变化，至少两个预警得到确认；现有仓位最多保留上一期一半。",
            "M3：利润率/增长与其他财务指标共同恶化，或杠杆硬破线连续确认；退出且不新建仓位。",
            "FCF单项下滑只作为观察信号；同一份历史披露不重复计入连续确认。M3不是证明护城河已消失，而是结构性风险退出信号。",
            "COST、ASML、TSM使用行业专属估值；ASML给予15%护城河溢价，TSM给予10%护城河溢价；LLY、VRTX使用医药行业DCF与FCF倍数交叉代理。",
            "回测含单边5bp交易成本；未计税费、滑点、汇率实时波动和极端行情执行影响。",
        ],
        "source": {
            "report_dir": str(report_dir),
            "price_source": summary.get("data", {}).get("price_source"),
            "fundamental_source": summary.get("data", {}).get("fundamental_source"),
            "curve_window": "最近五年与最近三年；各窗口从首个共同交易日归一至1.000",
            "moat_health_source": summary.get("data", {}).get("moat_health_source"),
            "moat_confirmation_basis": summary.get("data", {}).get("moat_confirmation_basis"),
            "read_only": True,
            "review_schedule": "每半个月复核（上半月末与月末交易日），下一交易日执行",
        },
    }
