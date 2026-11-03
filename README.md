# Quant Strategy Portfolio

Three independent strategy projects spanning fundamental valuation reversion, trend-driven rotation, and cross-exchange funding carry. This repository is the single entry point for reviewing the two equity-strategy codebases and the FundingCarry V4 portfolio project.

## Strategy projects

### 1. U.S. Moat Discovery — Fundamental Quality/Value Reversion

A low-frequency strategy for a 14-company U.S. quality universe. It combines operating quality, growth, free-cash-flow conversion, leverage, company-specific valuation anchors, and a moat-health overlay. The reversion thesis is valuation convergence gated by business quality, not price-only statistical mean reversion.

Code and research notes: [us-moat-discovery](https://github.com/mason1111199999-arch/quant-strategy-case-studies/tree/main/us-moat-discovery)

### 2. ChiNext + Yangtze Power — Trend-Driven Defensive Rotation

Uses the ChiNext Index's 20/45/60-day moving averages, 20-day momentum, and volume confirmation to switch between ChiNext ETF 159915 and Yangtze Power 600900. Includes two-day entry confirmation, a 45-day trend exit, and a 40-trading-day cooldown.

Code and research notes: [chinext-yangtze-rotation](https://github.com/mason1111199999-arch/quant-strategy-case-studies/tree/main/chinext-yangtze-rotation)

### 3. FundingCarry V4 — Cross-Exchange Funding-Rate Carry

Compares 7/14/30-day cumulative funding across multiple perpetual-futures venues and includes a Gate CrossEx V4 cockpit for two-leg preflight checks, position monitoring, and funding-ledger analysis. The project author reports live-market operation through bearish and range-bound conditions; performance has not been independently audited and bull-market behavior remains untested. The public portfolio copy disables order submission.

Code and project overview: [fundingcarry-portfolio](https://github.com/mason1111199999-arch/fundingcarry-portfolio)

## Resume-ready summary

**中文：**独立设计并实现三类系统化策略项目：美股基本面质量与估值回归、A股趋势驱动的防守轮动、跨交易所资金费率套利。项目涵盖基本面评分、市场状态识别、组合仓位规则、多交易所数据采集、双腿交易预检、持仓监控与风险约束。

**English:** Designed and implemented research projects across three systematic strategy families: U.S. fundamental quality/value reversion, China A-share trend rotation, and cross-exchange funding-rate carry. Work spans factor scoring, regime signals, portfolio rules, multi-venue data collection, paired-trade preflight checks, position monitoring, and risk controls.

## Backtest and operating caveats

The U.S. moat strategy manual reports proxy-backtest CAGR of 31.30% over ten years and 30.08% over five years, with maximum drawdowns of -35.06% and -33.45%, respectively, through 2026-10-15. These are model results, not audited live returns. The ChiNext NAV comparison omits transaction costs, taxes, and slippage. FundingCarry's live-market experience is self-reported and not independently audited. No result is a guarantee of future performance.

## Repository contents

- us-moat-discovery: V2.0 scoring/backtest dependencies and strategy manual.
- chinext-yangtze-rotation: read-only signal module and operating notes.
- FundingCarry V4 is maintained in the linked fundingcarry-portfolio repository.

Price/fundamental caches, generated reports, account settings, runtime state, and deployment files are excluded. No open-source license is included; all rights remain with the author unless a license is added separately.
