# ChiNext + Yangtze Power — Trend-Driven Defensive Rotation

A read-only daily regime strategy that uses the ChiNext Index (399006) to select between long exposure to E Fund ChiNext ETF (159915) and defensive exposure to Yangtze Power (600900). It never places orders.

## Signal logic

- Enter the ETF regime after two consecutive completed daily bars satisfy all conditions: index close above its 20/45/60-day averages, above its close 20 trading days earlier, and volume at least 80% of the 20-day average.
- Exit to Yangtze Power when the index closes below its 45-day average.
- After exit, wait 40 trading days before re-entry; the two-day entry confirmation still applies.
- Use index data for signals and matched ETF/defensive daily bars for historical regime NAV.

## Operation and limitations

`src/chinext.py` fetches public daily bars from Eastmoney, with Sina as fallback, and persists a signal snapshot/history when `ChinextService.refresh()` is called. It has no third-party Python dependencies. The default output paths are relative to the process working directory.

The NAV comparison does not model transaction costs, taxes, or execution slippage. This folder contains the strategy module, not the original multi-strategy dashboard or live service configuration.
