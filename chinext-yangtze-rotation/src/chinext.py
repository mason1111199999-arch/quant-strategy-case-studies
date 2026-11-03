"""Read-only long-only Chinext trend monitor with Yangtze Power defense."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any


ENTRY_CONFIRMATION_DAYS = 2
EXIT_CONFIRMATION_DAYS = 1
COOLDOWN_DAYS = 40


@dataclass(frozen=True)
class ChinextBar:
    day: str
    open: float
    close: float
    volume: float


class ChinextService:
    """Fetch daily public data and persist a signal snapshot; never places orders."""

    def __init__(
        self,
        status_path: Path = Path("reports/hyperliquid-lowfreq/chinext-status.json"),
        history_path: Path = Path("reports/hyperliquid-lowfreq/chinext-history.json"),
        nav_history_path: Path | None = None,
    ) -> None:
        self.status_path = status_path
        self.history_path = history_path
        self.nav_history_path = nav_history_path or history_path.with_name("chinext-nav-history.json")
        self._latest: dict[str, Any] | None = None
        self._historical_regime: dict[str, Any] | None = None

    @staticmethod
    def _fetch_bars(secid: str, code: str) -> list[ChinextBar]:
        query = urllib.parse.urlencode(
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "1",
                "beg": "20160101",
                "end": "20991231",
                "lmt": "5000",
                # Eastmoney may cache the daily endpoint after the close;
                # force a fresh response so the dashboard can pick up the
                # current trading day's index and volume bar.
                "_": str(int(datetime.now(UTC).timestamp() * 1000)),
            }
        )
        url = "http://60.push2his.eastmoney.com/api/qt/stock/kline/get?" + query
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        try:
            raw = urllib.request.urlopen(request, timeout=20).read().decode("utf-8")
            payload = json.loads(raw)
            rows = (payload.get("data") or {}).get("klines") or []
            # Eastmoney can occasionally serve a stale cached last bar after
            # the Shenzhen close. Force the public fallback in that case.
            shanghai_now = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai"))
            # Before the Shenzhen close, Eastmoney may expose a partial bar
            # for today.  Exclude it so the strategy always uses the latest
            # completed trading day.
            if shanghai_now.weekday() < 5 and (shanghai_now.hour, shanghai_now.minute) < (15, 30):
                if rows:
                    last_day = str(rows[-1]["day"] if isinstance(rows[-1], dict) else rows[-1].split(",")[0])
                    if last_day == shanghai_now.date().isoformat():
                        rows = rows[:-1]
            if (
                rows
                and shanghai_now.weekday() < 5
                and (shanghai_now.hour, shanghai_now.minute) >= (15, 30)
                and str(rows[-1]["day"] if isinstance(rows[-1], dict) else rows[-1].split(",")[0])
                < shanghai_now.date().isoformat()
            ):
                raise OSError("东方财富最新日线尚未更新")
        except (OSError, ValueError):
            # Some hosted environments cannot reach the Eastmoney node. Keep
            # the same daily/index-volume semantics with Sina as a public
            # read-only fallback.
            market, symbol = ("sh" if secid.startswith("1.") else "sz"), secid.split(".", 1)[1]
            fallback_query = urllib.parse.urlencode(
                {"symbol": market + symbol, "scale": "240", "ma": "no", "datalen": "1400"}
            )
            fallback_url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20data=/CN_MarketDataService.getKLineData?" + fallback_query
            fallback_request = urllib.request.Request(fallback_url, headers={"Referer": "https://finance.sina.com.cn/"})
            fallback_raw = urllib.request.urlopen(fallback_request, timeout=20).read().decode("utf-8")
            match = re.search(r"var data=\((.*)\);", fallback_raw, re.DOTALL)
            rows = json.loads(match.group(1)) if match else []
        if not rows:
            raise RuntimeError(f"{code} 东方财富历史行情为空")
        bars = [
            ChinextBar(
                day=str(row["day"] if isinstance(row, dict) else row.split(",")[0]),
                open=float(row["open"] if isinstance(row, dict) else row.split(",")[1]),
                close=float(row["close"] if isinstance(row, dict) else row.split(",")[2]),
                volume=float(row["volume"] if isinstance(row, dict) else row.split(",")[5]),
            )
            for row in rows
        ]
        if len(bars) < 60:
            raise RuntimeError(f"创业板 ETF 历史数据不足：{len(bars)} 根")
        return bars

    @staticmethod
    def _mean(values: list[float], period: int) -> float:
        return sum(values[-period:]) / period

    @staticmethod
    def _regime_states(index_bars: list[ChinextBar]) -> list[bool]:
        closes = [bar.close for bar in index_bars]
        volumes = [bar.volume for bar in index_bars]
        bullish: list[bool] = []
        below_ma45: list[bool] = []
        for i in range(len(index_bars)):
            if i < 60:
                bullish.append(False)
                below_ma45.append(False)
                continue
            fast = ChinextService._mean(closes[: i + 1], 20)
            medium = ChinextService._mean(closes[: i + 1], 45)
            slow = ChinextService._mean(closes[: i + 1], 60)
            vol_avg = ChinextService._mean(volumes[: i + 1], 20)
            bullish.append(
                closes[i] > fast
                and closes[i] > medium
                and closes[i] > slow
                and closes[i] > closes[i - 20]
                and volumes[i] >= 0.8 * vol_avg
            )
            below_ma45.append(closes[i] < medium)

        states: list[bool] = []
        in_attack = False
        cooldown = 0
        for i in range(len(index_bars)):
            if i >= EXIT_CONFIRMATION_DAYS and all(
                below_ma45[j]
                for j in range(i - EXIT_CONFIRMATION_DAYS + 1, i + 1)
            ):
                if in_attack:
                    cooldown = COOLDOWN_DAYS
                in_attack = False
            if cooldown > 0:
                cooldown -= 1
            if (
                i >= ENTRY_CONFIRMATION_DAYS - 1
                and cooldown == 0
                and all(
                    bullish[j]
                    for j in range(i - ENTRY_CONFIRMATION_DAYS + 1, i + 1)
                )
            ):
                in_attack = True
            states.append(in_attack)
        return states

    def _calculate(
        self,
        index_bars: list[ChinextBar],
        etf_bars: list[ChinextBar],
        defensive_bars: list[ChinextBar],
    ) -> dict[str, Any]:
        etf_by_day = {bar.day: bar for bar in etf_bars}
        defensive_by_day = {bar.day: bar for bar in defensive_bars}
        bars = [bar for bar in index_bars if bar.day in etf_by_day and bar.day in defensive_by_day]
        if len(bars) < 60:
            raise RuntimeError("创业板指数与 ETF 共同交易日数据不足")
        closes = [bar.close for bar in bars]
        volumes = [bar.volume for bar in bars]
        ma15 = self._mean(closes, 15)
        ma45 = self._mean(closes, 45)
        ma60 = self._mean(closes, 60)
        volume_ma20 = self._mean(volumes, 20)
        momentum20 = closes[-1] / closes[-21] - 1
        # State machine: two-day entry confirmation, one-day exit confirmation,
        # and a 40-trading-day re-entry cooldown after switching to defense.
        in_attack = self._regime_states(bars)[-1]

        if in_attack:
            target = 100
            signal = "BUY / 持有 ETF"
            reason = "连续两日满足站上 20/45/60 日均线、20 日动量和指数成交量条件"
        else:
            target = 0
            signal = "DEFENSE / 持有长江电力"
            reason = "趋势确认不足，或处于退出后的40个交易日冷静期；防守仓位持有长江电力"
        generated_at = datetime.now(UTC).isoformat()
        return {
            "ok": True,
            "instrument": "159915.SZ",
            "name": "易方达创业板 ETF",
            "defensive_instrument": "600900.SH",
            "defensive_name": "长江电力",
            "signal_instrument": "399006.SZ 创业板指",
            "strategy": "创业板指数：站上20/45/60日均线 + 20日动量 + 指数成交量 + 两日确认；跌破45日线即退出；退出后40日冷静期",
            "latest_day": bars[-1].day,
            "generated_at": generated_at,
            "close": round(closes[-1], 4),
            "etf_close": round(etf_by_day[bars[-1].day].close, 4),
            "defensive_close": round(defensive_by_day[bars[-1].day].close, 4),
            "ma15": round(ma15, 4),
            "ma45": round(ma45, 4),
            "ma60": round(ma60, 4),
            "momentum20_pct": round(momentum20 * 100, 2),
            "volume_vs_ma20_pct": round((volumes[-1] / volume_ma20 - 1) * 100, 2),
            "target_position_pct": target,
            "signal": signal,
            "reason": reason,
            "read_only": True,
            "data_source": "Eastmoney public daily kline · 指数信号 / ETF 做多 / 长江电力防守",
        }

    def refresh(self) -> dict[str, Any]:
        index_bars = self._fetch_bars("0.399006", "399006 创业板指")
        etf_bars = self._fetch_bars("0.159915", "159915 创业板 ETF")
        defensive_bars = self._fetch_bars("1.600900", "600900 长江电力")
        snapshot = self._calculate(
            index_bars,
            etf_bars,
            defensive_bars,
        )
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        history: list[dict[str, Any]] = []
        try:
            loaded = json.loads(self.history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = [item for item in loaded if isinstance(item, dict)]
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        point = {
            "date": snapshot["latest_day"],
            "close": snapshot["close"],
            "target_position_pct": snapshot["target_position_pct"],
            "signal": snapshot["signal"],
        }
        history = [item for item in history if item.get("date") != point["date"]]
        history.append(point)
        self.history_path.write_text(json.dumps(history[-500:], ensure_ascii=False, indent=2), encoding="utf-8")
        # Persist the complete strategy NAV curve as a separate file.  The
        # small history file above is intentionally retained for the existing
        # signal-status API and must not be overloaded with thousands of NAV
        # points.
        self._historical_regime = self._build_historical_regime(
            index_bars,
            etf_bars,
            defensive_bars,
        )
        self._latest = snapshot
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        if self._latest is not None:
            return self._latest
        try:
            loaded = json.loads(self.status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._latest = loaded
                return loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"ok": False, "error": "创业板 ETF 策略尚未完成首次行情刷新", "read_only": True}

    def history(self) -> list[dict[str, Any]]:
        try:
            loaded = json.loads(self.history_path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def historical_regime(self) -> dict[str, Any]:
        """Return the daily ETF/defense regime timeline for the dashboard."""
        if self._historical_regime is not None:
            return self._historical_regime
        index_bars = self._fetch_bars("0.399006", "399006 创业板指")
        etf_bars = self._fetch_bars("0.159915", "159915 创业板 ETF")
        defensive_bars = self._fetch_bars("1.600900", "600900 长江电力")
        return self._build_historical_regime(index_bars, etf_bars, defensive_bars)

    def _build_historical_regime(
        self,
        index_bars: list[ChinextBar],
        etf_bars: list[ChinextBar],
        defensive_bars: list[ChinextBar],
    ) -> dict[str, Any]:
        """Build and persist the regime and NAV histories from daily bars."""
        etf_by_day = {bar.day: bar for bar in etf_bars}
        defensive_by_day = {bar.day: bar for bar in defensive_bars}
        bars = [
            bar
            for bar in index_bars
            if bar.day in etf_by_day and bar.day in defensive_by_day
        ]
        states = self._regime_states(bars)
        points = [
            {
                "date": bar.day,
                "regime": "ETF" if states[i] else "DEFENSE",
                "index_close": round(bar.close, 4),
                "etf_close": round(etf_by_day[bar.day].close, 4),
                "defensive_close": round(defensive_by_day[bar.day].close, 4),
            }
            for i, bar in enumerate(bars)
        ]
        transitions = [
            {
                "date": points[i]["date"],
                "regime": points[i]["regime"],
            }
            for i in range(1, len(points))
            if points[i]["regime"] != points[i - 1]["regime"]
        ]
        nav_start = "2021-04-23"
        start_index = next((i for i, point in enumerate(points) if point["date"] >= nav_start), None)
        nav_points: list[dict[str, Any]] = []
        if start_index is not None:
            nav = etf_buy_hold = defensive_buy_hold = 1.0
            nav_points.append({"date": points[start_index]["date"], "strategy": nav, "etf": etf_buy_hold, "defensive": defensive_buy_hold})
            for i in range(start_index + 1, len(points)):
                previous = points[i - 1]
                current = points[i]
                instrument = "etf" if previous["regime"] == "ETF" else "defensive"
                nav *= current[instrument + "_close"] / previous[instrument + "_close"]
                etf_buy_hold *= current["etf_close"] / previous["etf_close"]
                defensive_buy_hold *= current["defensive_close"] / previous["defensive_close"]
                nav_points.append({"date": current["date"], "strategy": nav, "etf": etf_buy_hold, "defensive": defensive_buy_hold})
        self._historical_regime = {
            "ok": True,
            "strategy": "增强版：两日入场确认、单日跌破45日线退出、40个交易日冷静期",
            "data_source": "Eastmoney public daily kline · 指数信号 / ETF 做多 / 长江电力防守",
            "start_date": points[0]["date"],
            "end_date": points[-1]["date"],
            "points": points,
            "transitions": transitions,
            "nav_start_date": nav_start,
            "nav_points": nav_points,
        }
        self.nav_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.nav_history_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "strategy": self._historical_regime["strategy"],
                    "data_source": self._historical_regime["data_source"],
                    "nav_start_date": nav_start,
                    "start_date": nav_points[0]["date"] if nav_points else None,
                    "end_date": nav_points[-1]["date"] if nav_points else None,
                    "points": nav_points,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return self._historical_regime
