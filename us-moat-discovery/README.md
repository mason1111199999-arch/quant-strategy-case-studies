# U.S. Moat Discovery — Quality/Value Mean-Reversion

A low-frequency rotation research strategy for a 14-company U.S.-listed quality universe. Its valuation-reversion thesis is conditional: buy/hold decisions require business-quality and balance-sheet gates, valuation below company-specific fair-value ranges, and no structural moat-health exit signal. It is not a price-only statistical mean-reversion strategy.

## Research workflow

1. Use point-in-time SEC filing data and completed daily prices.
2. Score operating quality, growth/FCF conversion, and leverage.
3. Compare price with company-specific DCF/FCF/earnings valuation anchors and historical valuation ranges.
4. Apply M0–M3 financial-proxy health states to freeze new buys, reduce exposure, or exit.
5. Rebalance on a semi-monthly schedule and execute signals on the next trading day in the backtest model.

## Files

- `STRATEGY_MANUAL.md`: full rule set, operating assumptions, and limitations.
- `src/hyperliquid_lowfreq/moat_strategy.py`: dashboard snapshot/report adapter; not the scoring engine.
- `scripts/backtest_bluechip_strategy_v20.py`: current V2.0 scoring and portfolio/backtest layer.
- V1.0/V1.1/V1.4–V1.7 and historical valuation checks: imported foundations required by V2.0.

## Reproduction

The backtest modules fetch public prices and SEC filing data and write outputs under `reports/`. Install Pillow for the legacy chart/report helper imported by the base script. Run from this folder with `PYTHONPATH=scripts python scripts/backtest_bluechip_strategy_v20.py`. Network access to Yahoo Finance and SEC data endpoints is required. The inherited SEC User-Agent contains a placeholder contact and should be replaced with a valid contact before repeated automated requests.

## Reported model results

The strategy manual reports a ten-year CAGR of 31.30% and maximum drawdown of -35.06%, and a five-year CAGR of 30.08% and maximum drawdown of -33.45%, as of 2026-10-15. These are proxy-backtest results, not independently audited live performance. Detailed methodology and caveats are in `STRATEGY_MANUAL.md`.

This project contains no personal portfolio holdings, account credentials, cached data, reports, dashboard runtime state, or deployment configuration.
