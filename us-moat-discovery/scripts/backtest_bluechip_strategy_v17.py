#!/usr/bin/env python3
"""V1.7 wrapper: V1.6 plus dedicated TSM valuation with a 10% moat premium."""
from __future__ import annotations

import json
from pathlib import Path

import backtest_bluechip_strategy_v16 as v16
import historical_entry_check_tsm as tsm_model


ROOT = v16.ROOT
OUT = ROOT / "reports/bluechip-strategy-10y-v17"
VERSION = "V1.7"
BASE_SCORE_V16 = v16.score_v16


def score_tsm(fin: dict, when: str, price: float, tnx: float, history: list[float]) -> dict:
    raw = tsm_model.score(when, price, tnx, fin, history)
    ratio = raw.get("price_fv_with_moat_premium")
    q = raw.get("Q")
    q4 = raw.get("q4", 0)
    quality_ok = q is not None and q >= 35 and q4 >= 3
    primary = bool(quality_ok and ratio is not None and ratio <= tsm_model.PARAMS["primary_ceiling"])
    reserve = bool(quality_ok and ratio is not None and ratio <= tsm_model.PARAMS["reserve_ceiling"])
    hold = bool(quality_ok and ratio is not None and ratio <= 1.55)
    multiple = raw.get("current_pe")
    e, percentile = v16.v14.percentile_score(multiple, history)
    a = v16.dedicated_a(ratio)
    s = q + (e or 0) + (a or 0) if q is not None and e is not None and a is not None else None
    return {
        **raw,
        "eligible": primary or reserve, "primary": primary, "reserve": reserve, "hold": hold,
        "multiple": multiple, "E": e, "A": a, "S": s, "percentile": percentile,
        "price_fv": ratio, "category": "semiconductor",
        "reason": "primary" if primary else "reserve" if reserve else "tsm_quality_or_valuation_gate",
        "v17_moat_premium": tsm_model.PARAMS["moat_premium"],
    }


def score_v17(ticker: str, fin: dict, when: str, price: float, tnx: float, history: list[float]) -> dict:
    if ticker == "TSM":
        return score_tsm(fin, when, price, tnx, history)
    return BASE_SCORE_V16(ticker, fin, when, price, tnx, history)


def main() -> None:
    v16.OUT = OUT
    v16.VERSION = VERSION
    v16.DEDICATED_TICKERS = {"COST", "ASML", "TSM"}
    v16.score_v16 = score_v17
    v16.main()
    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["config"]["version"] = VERSION
    summary["config"]["valuation_update"] = "COST专属估值；ASML专属估值+15%护城河溢价；TSM专属周期EPS估值+10%护城河溢价；其他9家公司沿用V1.5规则"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = OUT / "report.md"
    report = report_path.read_text(encoding="utf-8").replace("V1.6", "V1.7")
    report = report.replace("其他10家公司沿用V1.5行业估值规则。", "TSM使用周期调整后EPS-DCF/历史PE/EV-EBIT模型并加入10%护城河溢价；其他9家公司沿用V1.5行业估值规则。")
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps({"output": str(OUT), "strategy": summary["strategy"], "equal_weight_buy_hold": summary["equal_weight_buy_hold"], "spy_buy_hold": summary["spy_buy_hold"], "events": len(summary["events"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
