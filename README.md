# Systematic Strategy Case Studies

Two independent, source-backed research projects prepared for a quantitative-strategy résumé portfolio. The first is a fundamental valuation-reversion framework; the second is a market-regime trend strategy with a defensive rotation. Both are read-only research systems and do not place orders.

## Projects

### 1. U.S. Moat Discovery — Quality/Value Mean-Reversion

A low-frequency rotation strategy for a 14-company U.S.-listed quality universe. It combines point-in-time fundamental quality, company-specific valuation anchors, and a slow-moving moat-health overlay. The mean-reversion thesis is valuation convergence toward an estimated fair-value range, gated by business quality and financial health; it is not a price-only statistical mean-reversion system.

### 2. ChiNext + Yangtze Power — Trend-Driven Defensive Rotation

A daily index-regime strategy that holds ChiNext ETF 159915 when a confirmed multi-timeframe trend and volume condition is met, and rotates to Yangtze Power 600900 when the trend breaks. Two-day entry confirmation and a 40-trading-day cooldown aim to reduce regime whipsaw.

## Resume-ready descriptions

**中文**

- 设计并实现美股质量价值轮动研究框架：结合时点可得的财报数据、公司差异化估值锚、护城河健康度预警和分层仓位规则，形成“质量筛选—估值均值回归—风险退出”的闭环。
- 构建创业板趋势驱动的防守轮动策略：以创业板指多周期均线、动量和成交量识别风险状态，在创业板ETF与长江电力之间切换，并加入连续确认、退出规则和冷静期。

**English**

- Built a point-in-time U.S. quality/value rotation research system combining company-specific fair-value anchors, fundamental quality gates, moat-health monitoring, and risk-aware portfolio allocation.
- Developed a trend-regime rotation strategy for China A-shares, combining multi-horizon moving averages, momentum, and volume confirmation to switch between a ChiNext ETF and a defensive utility holding.

## Portfolio evidence and caveats

The U.S. strategy manual reports a ten-year CAGR of 31.30% and maximum drawdown of -35.06%, with a five-year CAGR of 30.08% and maximum drawdown of -33.45%, as of 2026-10-15. These are model backtest figures from the project's documented proxy methodology, not independently audited live returns or a guarantee of future performance. The ChiNext strategy's NAV comparison is computed from matched daily ETF/defensive bars and does not model transaction costs, taxes, or execution slippage.

## Repository layout

- `us-moat-discovery/` — current V2.0 scoring/backtest dependencies and the strategy manual.
- `chinext-yangtze-rotation/` — read-only signal module and operating notes.

Price/fundamental caches, generated reports, account settings, runtime state, and deployment files are excluded. No open-source license is included; all rights remain with the author unless a license is added separately.
