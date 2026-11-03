#!/usr/bin/env python3
"""V1.5 balanced version of the company-specific valuation-gated backtest.

V1.4 showed that the hard price gate was directionally right but calibrated
too tightly.  This wrapper keeps V1.4's point-in-time data and valuation
models, then applies the revised, category-specific fair-value bands:
quality still matters, valuation still blocks clearly expensive buys, but a
high-moat company can be bought at a reasonable premium to modeled FV.
"""
from __future__ import annotations

import json
from pathlib import Path

import backtest_bluechip_strategy_v14 as v14


ROOT = v14.ROOT
OUT = ROOT / "reports/bluechip-strategy-10y-v15"

# Wider, but still finite, price/FV ceilings.  They are entry ceilings, not
# permission to buy any price; the quality and score gates remain active.
PRIMARY_RATIO = {
    "consumer": 1.15, "payments": 1.25, "information": 1.25,
    "infrastructure": 1.25, "rail": 1.25, "platform": 1.25,
    "semiconductor": 1.35, "conglomerate": 1.25,
}
RESERVE_RATIO = {
    "consumer": 1.35, "payments": 1.45, "information": 1.45,
    "infrastructure": 1.45, "rail": 1.45, "platform": 1.45,
    "semiconductor": 1.55, "conglomerate": 1.45,
}

BASE_SCORE_COMPANY = v14.score_company

# Relax the debt proxy only where the old operating-income denominator was too
# punitive.  The report labels these as proxies; this is not a claim that
# leverage is irrelevant.
v14.LEVERAGE_BANDS.update({
    "CNI": (3.0, 4.5, 6.5),
    "WM": (4.0, 6.0, 8.0),
    "KO": (4.0, 6.0, 8.0),
})


def relaxed_a(ratio: float | None, category: str) -> int | None:
    if ratio is None:
        return None
    reserve = RESERVE_RATIO[category]
    return 20 if ratio <= 0.90 else 15 if ratio <= 1.05 else 10 if ratio <= 1.20 else 5 if ratio <= reserve else 0


def score_v15(ticker: str, fin: dict, when: str, price: float, tnx: float, history: list[float]) -> dict:
    score = BASE_SCORE_COMPANY(ticker, fin, when, price, tnx, history)
    category = score.get("category", v14.CATEGORY.get(ticker, "consumer"))
    ratio = score.get("price_fv")
    a = relaxed_a(ratio, category)
    e = score.get("E")
    q = score.get("Q")
    s = q + (e or 0) + (a or 0) if q is not None and e is not None and a is not None else None
    q4 = score.get("q4", 0)
    risk_ok = q4 >= 3 and score.get("leverage") is not None
    # V1.5 uses Q>=40, S>=60, E>=5, A>=5 for the acceptable reserve zone.
    # The primary zone is preferred but both zones count as qualifying names,
    # so the user's one-name=100% / multiple-names=equal-weight rule remains.
    primary = bool(risk_ok and q is not None and q >= 40 and s is not None and s >= 65 and e is not None and e >= 5 and a is not None and a >= 10 and ratio is not None and ratio <= PRIMARY_RATIO[category])
    reserve = bool(risk_ok and q is not None and q >= 40 and s is not None and s >= 60 and e is not None and e >= 5 and a is not None and a >= 5 and ratio is not None and ratio <= RESERVE_RATIO[category])
    eligible = primary or reserve
    hold = bool(risk_ok and q is not None and q >= 35 and s is not None and s >= 55 and a is not None and a >= 5 and ratio is not None and ratio <= min(1.55, RESERVE_RATIO[category] + 0.10))
    score.update({"primary": primary, "reserve": reserve, "eligible": eligible, "hold": hold, "A": a, "S": s, "price_fv": ratio, "reason": "primary" if primary else "reserve" if reserve else "v15_quality_or_valuation_gate", "category": category, "v15_reserve_ceiling": RESERVE_RATIO[category]})
    return score


def main() -> None:
    v14.OUT = OUT
    v14.CHART_VERSION = "V1.5"
    v14.score_company = score_v15
    v14.main()
    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["config"]["version"] = "V1.5-balanced-company-specific-valuation-gated"
    summary["config"]["buy_rule"] = "Q>=40, S>=60, E>=5, A>=5 and price/FV<=category reserve ceiling; primary zone remains tighter"
    summary["config"]["valuation_gate"] = "V1.5 keeps a hard price/FV gate but widens reasonable-premium bands; all primary+reserve qualifiers are held equally"
    summary["config"]["calibration_note"] = "V1.4 price/FV ceilings were widened after the first run left only four names with any entry"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = OUT / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("V1.4", "V1.5").replace("13股专属估值门槛", "13股平衡估值门槛")
    report = report.replace("V1.4策略", "V1.5 Strategy")
    report += "\n\n## V1.5调整说明\n\n在保留估值硬门槛的前提下，放宽各行业合理溢价区间；E门槛由10降为5；CNI、WM、KO改用较不惩罚性的杠杆代理区间。TSM/ASML仍使用周期调整后FCF估值，后续可继续用EPS周期模型交叉验证。\n"
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps({"output": str(OUT), "strategy": summary["strategy"], "equal_weight_buy_hold": summary["equal_weight_buy_hold"], "spy_buy_hold": summary["spy_buy_hold"], "events": len(summary["events"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
