# Strategy Research: MT5 Crypto Bot

Run ID: `20260622_003841`

This document translates local competition constraints and external crypto quant research into a testable strategy plan for the allowed instruments only:

- `BAR/USD` - Hedera/HBAR per `information.md`
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

No trading code was implemented in this phase. All unattended execution paths must remain dry-run, paper, read-only, or test/report generation unless a separate live-approval workflow is explicitly completed.

Current status update: later prompts have implemented the dry-run pipeline, offline analytics, and a separate guarded live runner. This research document remains the justification for `momo_v1`, but operational live trading must follow `docs/run_live_trading.md` and cannot run without `LIVE_APPROVED=true` plus `config/LIVE_APPROVED.json`.

## Rules-First Constraints

`rules.md` is the highest-priority source of truth. The relevant implications for strategy design are:

- Trade only `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, and `XRP/USD`.
- Avoid forced liquidation. The platform stop-out level is 30% margin level.
- Stay materially below penalty zones:
  - margin usage penalty starts above 90% for 30 minutes;
  - leverage penalty starts above 28x for 30 minutes;
  - single-instrument exposure penalty starts above 90% of gross exposure;
  - net directional exposure penalty starts above 95% of gross exposure.
- Use 15-minute account-equity returns when evaluating Sharpe.
- Avoid API abuse. The rules safe harbor is 500 requests per second, but this project should poll far below that.
- Do not use fully autonomous live retuning. Parameter changes may be proposed offline only and must require human approval before activation.

The blueprint's strategy direction, volatility-managed crypto momentum with strict guardrails, is consistent with `rules.md`. Any later implementation must keep internal caps below the rule penalty thresholds.

## Source Table

| Source | Link | Concrete Takeaway | Strategy Implication |
| --- | --- | --- | --- |
| Local competition rules | `rules.md` | Ranking rewards return, drawdown, Sharpe, and risk discipline; risk penalties target sustained high margin, leverage, concentration, API abuse, and forced liquidation. | Strategy can be directional, but internal risk caps must sit well below penalty thresholds and every signal/risk decision must be auditable. |
| Local event information | `information.md` | MT5 is the API-capable channel; `BAR/USD` means HBAR/Hedera; crypto symbols and contract metadata must be verified in MT5. | Keep canonical symbols internally, discover broker symbol names later, and trade HBAR only after spread/liquidity/contract metadata are verified. |
| Local blueprint | `mt5_crypto_trading_blueprint.md` | Recommends volatility-scaled multi-asset crypto momentum with BTC regime, alt relative strength, ATR exits, spread filters, and strict risk caps. | Use the blueprint as the baseline, refined by research below. |
| Liu, Tsyvinski, Wu, "Common Risk Factors in Cryptocurrency" | https://economics.yale.edu/research/common-risk-factors-cryptocurrency | Cryptocurrency market, size, and momentum factors help explain cross-sectional crypto returns. | Cross-sectional relative strength is defensible for BTC, ETH, SOL, XRP, and cautiously BAR/HBAR if liquidity is acceptable. |
| Barroso and Santa-Clara, "Momentum Has Its Moments" | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2041429 | Momentum risk is time-varying; scaling exposure by realized volatility can materially reduce crash behavior in traditional momentum. | Volatility-manage signal exposure instead of using fixed leverage. |
| Grobys et al., "Cryptocurrency momentum has (not) its moments" | https://link.springer.com/article/10.1007/s11408-025-00474-9 | Crypto momentum can suffer severe idiosyncratic crashes; volatility management helps, but tail risk remains large. | Use momentum only with volatility scaling, symbol caps, spread filters, and drawdown guards. Do not assume momentum is a free lunch. |
| Wen, Bouri, Xu, Zhao, "Intraday return predictability in the cryptocurrency markets" | https://ideas.repec.org/a/eee/ecofin/v62y2022ics1062940822000833.html | Intraday momentum and reversal appear in BTC and other active crypto, including Ethereum and Ripple; patterns vary with jumps, FOMC, liquidity, and regimes. | M5/M15/H1 features are justified, but intraday reversal should be a challenger or exit filter until validated. |
| Zarattini, Pagani, Barbon, "Catching Crypto Trends" | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907 | Donchian channel trend ensembles with volatility-based sizing show strong net-of-fees results in a liquid crypto rotation universe. | Add Donchian breakout features and keep a Donchian ensemble as a primary challenger strategy. |
| "Momentum Trading in Cryptocurrencies: A Comparative Study of Time-Series and Cross-Sectional Strategies" | https://www.zurnalai.vu.lt/BATP/en/article/view/44540 | A multi-horizon EMA framework on BTC, ETH, XRP, SOL and other large crypto finds economically meaningful momentum effects, using volatility normalization and both time-series and cross-sectional structures. | Multi-horizon EMA distances and volatility-normalized ranking should be implemented directly. |
| Drogen, Hoffstein, Otte, "Cross-sectional Momentum in Cryptocurrency Markets" | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4322637 | Short-term crypto winners over roughly 30 days tend to outperform over the next week; long-only momentum can outperform Bitcoin. | Use relative-strength ranking as a position-size modifier, not as the only signal during a short hackathon window. |
| Wei et al., "Cryptocurrencies and Lucky Factors" | https://kar.kent.ac.uk/id/document/3410597 | Many technical/fundamental rules fail after data-snooping controls; only a small subset of short-term moving-average style signals is robust. | Keep features simple, test out-of-sample, and avoid a large parameter search. |
| "Order Book Liquidity on Crypto Exchanges" | https://www.mdpi.com/1911-8074/18/3/124 | Intraday liquidity and order book variation affect trading PnL and can support better timing. | Spread and liquidity filters are mandatory; order-book features are useful only if MT5 depth is stable. |
| "Explainable Patterns in Cryptocurrency Microstructure" | https://arxiv.org/html/2602.00776v1 | Compact top-of-book and trade-flow features can have short-horizon predictive value across crypto assets. | Use order-book imbalance as an optional confirmation filter, never as the core MVP. |
| Official MetaTrader 5 Python integration docs | https://www.mql5.com/en/docs/python_metatrader5 | MT5 Python exposes account, symbol, bar, tick, market-depth, order-check, order, position, and history functions. | Later implementation should use read-only functions for discovery/collection and guard any future `order_send` behind explicit live approval. |

## Instrument-Specific Implications

| Instrument | Research Read | Practical Role | Starting Risk Treatment |
| --- | --- | --- | --- |
| `BTC/USD` | BTC is the best-studied crypto asset and a natural proxy for the crypto market factor. Intraday research is strongest for BTC. | Market regime anchor and core traded instrument. BTC trend should gate alt risk-on/risk-off exposure. | Highest liquidity assumption, but still cap symbol leverage around 2.0x and require spread <= 8 bps until broker data proves otherwise. |
| `ETH/USD` | ETH is included in intraday and EMA-momentum evidence and usually carries high crypto beta. | Liquid alt/core momentum asset. Trade ETH when its own momentum confirms BTC regime or when relative strength vs BTC is strong. | Cap symbol leverage around 2.0x; reduce exposure when ETH and BTC signals are redundant and net directional exposure is already high. |
| `SOL/USD` | Contemporary EMA research includes Solana. SOL is typically higher beta and can trend sharply. | High-beta momentum sleeve for broad risk-on conditions. | Cap lower than BTC/ETH, around 1.25x to 1.5x; require BTC regime confirmation, wider ATR stops, and stricter drawdown response. |
| `XRP/USD` | Ripple/XRP appears in intraday and EMA evidence, but it is event-sensitive and can jump on regulatory or headline risk. | Opportunistic momentum sleeve with strong risk controls. | Cap around 1.0x to 1.25x; require spread and jump filters, and avoid increasing exposure after extreme one-bar moves. |
| `BAR/USD` | Local notes define BAR as HBAR/Hedera. The reviewed academic evidence does not directly validate HBAR. | Small, optional idiosyncratic alt sleeve only if MT5 metadata confirms reasonable spread, volume step, and depth. | Cap around 0.5x to 0.75x initially; block trading if spread is wide, depth is missing, or mapping is ambiguous. |

## Candidate Strategy Comparison

| Candidate | Evidence Base | Pros | Cons | Decision |
| --- | --- | --- | --- | --- |
| Volatility-managed multi-horizon momentum with BTC regime and alt relative strength | Liu/Tsyvinski/Wu, Grobys et al., EMA study, Drogen/Hoffstein/Otte, blueprint | Directly testable on the five instruments; simple; explainable; aligns with return objective while controlling risk. | Momentum can crash; alt signals can be highly correlated; needs spread/slippage discipline. | Recommended MVP. |
| Donchian channel trend ensemble with volatility sizing | Zarattini/Pagani/Barbon | Robust trend-following structure; less fragile than one EMA crossover; easy to backtest. | May be slower on short hackathon windows; can whipsaw during range-bound crypto. | Challenger 1 and component feature in MVP. |
| Intraday momentum/reversal timing | Wen/Bouri/Xu/Zhao | Fits M5/M15 competition cadence; can improve entries/exits and Sharpe. | Regime-sensitive; jump/liquidity effects can flip from momentum to reversal. | Challenger 2; use as exit/no-trade filter until validated. |
| Order-book imbalance and liquidity timing | Order-book liquidity and microstructure papers; MT5 depth docs | Can reduce bad fills and add short-horizon confirmation. | MT5 depth availability is unknown; high-frequency usage risks complexity and API noise. | Optional filter only; not required for MVP. |
| Pure EMA crossover | General technical-analysis and EMA evidence | Fastest to implement. | Too generic; vulnerable to whipsaw and overfitting if used alone. | Use EMA distances/slopes as features, not as standalone strategy. |
| News/sentiment or LLM-assisted trading | Sponsor tools can classify text cheaply in batch | Good demo and event-risk context for XRP/SOL/BAR. | Latency/noise; LLM outputs must not control live orders. | Stretch/shadow only, never blocking execution. |
| ML classifier on engineered features | Microstructure and forecasting literature | Potentially captures nonlinear interactions. | Overfit risk and build time; needs labeled data. | Stretch challenger after deterministic MVP is stable. |

## Recommended MVP Strategy

Use a volatility-managed multi-horizon crypto momentum strategy with:

- BTC regime filter;
- alt relative-strength ranking against BTC;
- EMA and Donchian trend confirmation;
- ATR-based stops and take-profit levels;
- spread/liquidity filters;
- strict leverage, margin, concentration, and drawdown caps.

This is the best fit because it is supported by the crypto factor literature, implementable from MT5 OHLCV/tick data, explainable to judges, and compatible with `rules.md` risk discipline. It also avoids relying on fragile live LLM decisions or high-frequency order-book trading.

### Starting Signal Cadence

- Collect M1 and M5 bars; compute signals only on completed M5 bars.
- Track M15 account-equity returns for competition-aligned Sharpe.
- Use H1 features for regime and volatility context.
- Poll ticks conservatively, for example every 5 seconds in dry-run collection. This is far below the `rules.md` safe-harbor level.

### Starting MVP Signal Formula

For each symbol at a completed M5 bar:

```text
trend_score =
    0.25 * zscore(return_15m)
  + 0.25 * zscore(return_1h)
  + 0.20 * zscore(ema20_minus_ema80_over_atr)
  + 0.15 * donchian_breakout_score
  + 0.10 * zscore(ema20_slope)
  + 0.05 * volume_confirmation
```

For altcoins:

```text
relative_score = zscore(asset_return_1h - beta_to_btc * btc_return_1h)
final_score = 0.75 * trend_score + 0.25 * relative_score
```

For BTC:

```text
final_score = trend_score
```

Optional, only when MT5 depth is verified:

```text
if book_imbalance confirms signal direction:
    final_score += 0.10 * sign(final_score)
if book_imbalance contradicts signal direction:
    final_score -= 0.10 * sign(final_score)
```

### Entry And Exit Rules

Long entry:

- `final_score >= 1.25`;
- M5 close is above EMA80;
- spread is below the symbol cap;
- realized volatility is not in an extreme shock band;
- for ETH/SOL/XRP/BAR, BTC regime is not strongly negative;
- all risk checks pass.

Short entry:

- `final_score <= -1.25`;
- M5 close is below EMA80;
- spread is below the symbol cap;
- realized volatility is not in an extreme shock band;
- for ETH/SOL/XRP/BAR, BTC regime is not strongly positive;
- all risk checks pass.

Exit or reduce:

- absolute `final_score <= 0.35`;
- price crosses back through EMA20 against the position;
- ATR stop, trailing stop, take-profit, or time stop triggers;
- spread becomes abnormal;
- data becomes stale;
- drawdown, leverage, margin, or kill-switch guard triggers.

### Starting Parameters

These are research-driven starting values, not live-approved production settings.

| Parameter | Starting Value | Notes |
| --- | --- | --- |
| Signal timeframe | M5 completed bars | Avoids tick-level overtrading and API noise. |
| Confirmation timeframes | M15 and H1 | Aligns with competition Sharpe and regime detection. |
| EMA spans | 20 and 80 M5 bars | Simple intraday trend backbone. |
| Donchian lookbacks | 12, 24, 48 M5 bars | 1h, 2h, and 4h trend breakout ensemble. |
| ATR span | 14 M5 bars | Used for stops, shock filters, and normalization. |
| Realized vol spans | 12, 24, 48 M5 bars | Used for volatility scaling and abnormal-vol filters. |
| Entry threshold | 1.25 | Coarse-grid candidate: 1.0, 1.25, 1.5. |
| Exit threshold | 0.50 | Updated from 0.35 after 2026-06-23 overnight evidence favored faster exits; coarse-grid candidate: 0.25, 0.35, 0.5. |
| ATR stop | 1.6x ATR | Coarse-grid candidate: 1.2, 1.6, 2.0. |
| Take profit | 2.4x ATR | Coarse-grid candidate: 1.8, 2.4, 3.0. |
| Trailing stop | 1.2x ATR | Activate only after unrealized profit exceeds 1 ATR. |
| Time stop | 180 minutes | Prevent stale intraday positions. |
| Max spread BTC/ETH | 8 bps | Must be verified with MT5 metadata and live ticks. |
| Max spread SOL/XRP | 15 bps | Raise only if live data proves it is needed and still profitable. |
| Max spread BAR/HBAR | 25 bps | Block BAR if observed spreads are unstable or depth is thin. |

## Exact Features To Implement

All features must be timestamped and computed only from data available at or before the signal bar close.

Price and return features:

- close-to-close returns over 1, 3, 12, and 48 M5 bars;
- M15 return and H1 return;
- rolling z-scores of 15m and 1h returns by symbol;
- rolling high/low distance over 12, 24, and 48 M5 bars.

Trend features:

- EMA8, EMA20, EMA55, EMA80 on M5 closes;
- EMA20 minus EMA80 normalized by ATR;
- EMA20 slope over 3 and 6 M5 bars;
- Donchian upper/lower breakout scores for 12, 24, and 48 M5 bars;
- binary trend regime: above/below EMA80.

Volatility and risk features:

- ATR14 on M5 bars;
- realized volatility over 12, 24, and 48 M5 bars;
- volatility percentile or z-score per symbol;
- shock flag when one-bar absolute return exceeds a high percentile or an ATR multiple;
- drawdown from latest account equity snapshots once available.

Liquidity and execution features:

- bid/ask spread in price units and bps;
- tick age and data freshness;
- tick volume z-score;
- symbol metadata: digits, point, contract size, tick size, tick value, volume min/max/step, trade mode, filling mode;
- optional top-of-book imbalance if `market_book_get()` works reliably.

BTC regime and relative-strength features:

- BTC M5/M15/H1/H4 returns;
- BTC EMA20/EMA80 trend state;
- rolling beta of each alt to BTC using M5 or M15 returns;
- beta-adjusted 1h relative strength: `asset_return_1h - beta_to_btc * btc_return_1h`;
- cross-sectional rank of final scores across BTC, ETH, SOL, XRP, BAR/HBAR.

Risk engine inputs:

- current gross leverage;
- per-symbol notional exposure;
- net directional exposure;
- margin usage;
- max drawdown;
- stale-data status;
- kill-switch status;
- pending/open position age.

## Backtest Plan

The first backtester should be lightweight and conservative.

1. Data:
   - use organizer historical data when available;
   - otherwise use MT5-collected M1/M5 data from dry-run collection;
   - include only the five allowed canonical instruments and verified broker symbol mappings.
2. Bar timing:
   - compute features on completed bars only;
   - generate signal at M5 bar close;
   - assume execution at next bar open or next available bid/ask, not the same bar close.
3. Costs:
   - charge half-spread on entry and exit at minimum;
   - add slippage bps by symbol, higher for SOL/XRP/BAR than BTC/ETH;
   - reject simulated trades when spread exceeds the cap.
4. Strategy set:
   - MVP: volatility-managed multi-horizon momentum with BTC regime and relative strength;
   - Challenger 1: Donchian channel ensemble with volatility sizing;
   - Challenger 2: intraday momentum/reversal timing layer;
   - optional Challenger 3: order-book-filtered momentum if depth data exists.
5. Metrics:
   - total return;
   - maximum drawdown;
   - non-annualized 15-minute Sharpe approximation;
   - trade count;
   - exposure and turnover;
   - symbol-level PnL;
   - side-level PnL;
   - spread/slippage cost estimate;
   - risk-discipline estimate against `rules.md`.
6. Validation:
   - use coarse parameter grids only;
   - perform walk-forward or train/validation split if enough data exists;
   - reject parameter sets that rely on one symbol, very low trade counts, or high concentration;
   - document any deviation from this research before implementation.

If crypto history is incomplete, backtests are provisional. The fallback is fixture-level correctness tests plus live dry-run/shadow validation on newly collected MT5 data.

## Live Validation Plan

This plan is read-only or dry-run. It must not place orders.

1. Symbol validation:
   - discover exact MT5 broker symbols for the five canonical labels;
   - manually review ambiguous mappings;
   - store digits, point, contract size, tick size/value, volume min/max/step, spread, trade mode, filling mode, and margin fields.
2. Data validation:
   - collect M1/M5 bars and latest ticks for at least 30-60 minutes;
   - verify timestamps are UTC-aware and bars are complete;
   - measure spread bps distributions by symbol;
   - check whether `market_book_add/get/release` returns stable 5-level depth.
3. Dry-run signal validation:
   - run the feature and strategy cycle on completed M5 bars;
   - store every signal, including no-trade decisions;
   - compare signal direction to next 1, 3, and 6 M5 bar returns;
   - track whether risk checks block trades for spread, stale data, leverage, concentration, or drawdown.
4. Poll-rate validation:
   - keep the collector and bot loop at seconds-to-minutes cadence, not high-frequency cadence;
   - log approximate request counts per cycle;
   - stay comfortably below the 500 requests/second safe-harbor threshold.
5. Readiness gate:
   - do not move beyond dry-run until later prompts produce tests, symbol metadata, backtest/shadow reports, risk checks, execution dry-run proof, and a live readiness review.

## Risks Mapped To `rules.md`

| Risk | Rule Impact | Mitigation |
| --- | --- | --- |
| Forced liquidation | Immediate elimination. | Keep internal gross leverage cap <= 8x normal mode, margin usage <= 60%, and stop new risk above drawdown guards. |
| Margin usage above 90%/95%/98% | Risk discipline penalties and compliance review. | Internal cap 60%; warn above 50%; block new trades and reduce risk if breached. |
| Leverage above 28x/29x/near 30x | Risk discipline penalties and compliance review. | Internal cap 8x normal mode; no automated increase; any higher finals stretch requires human approval. |
| Single-instrument concentration above 90% | Risk discipline penalty. | Internal cap 75% of gross exposure, with lower symbol leverage caps for SOL/XRP/BAR. |
| Net directional exposure above 95% | Risk discipline penalty. | Internal cap 85%; reduce correlated BTC/ETH/SOL same-direction stacking. |
| Momentum crash or idiosyncratic jump | Drawdown, Sharpe, and survival risk. | Volatility scaling, ATR stops, shock filters, time stops, drawdown guard, symbol caps. |
| Wide spreads and poor liquidity | Return drag and false signal profitability. | Spread bps filters, tick freshness checks, optional order-book confirmation, conservative sizing. |
| API abuse or anomalous request patterns | Red-line disqualification risk. | M5 signal cadence, conservative tick polling, no high-frequency loops, request count logging. |
| Ambiguous broker symbol mapping | Trading unsupported or wrong instrument. | Canonical allow-list plus explicit broker mapping; ambiguous mappings require manual confirmation. |
| MT5 disconnect | Positions remain open without client-side auto-flatten. | Reconnect and reconcile before new trades; stop new risk while state is unknown. |
| Overfitting and data snooping | Live performance failure. | Small feature set, coarse grids, walk-forward validation, out-of-sample checks, no automatic promotion. |
| LLM or sponsor integration error | Unsafe or unauditable decisions. | Sponsor tools are optional and outside execution; LLM output can summarize or propose, never directly trade. |
| Secret leakage | Compliance and security risk. | Never commit `.env`, API keys, credentials, passwords, tokens, account numbers, or full account details. |

## Research Conclusion

The recommended MVP is:

```text
Volatility-managed multi-horizon crypto momentum
with BTC regime filtering, alt relative-strength ranking,
EMA and Donchian trend confirmation, ATR exits,
spread/liquidity filters, and strict rules-aligned risk caps.
```

This strategy is strong enough to pursue return, simple enough to implement and test quickly, and disciplined enough to respect `rules.md`. Donchian trend following, intraday reversal/momentum timing, order-book imbalance, and news/sentiment analysis should be kept as challengers or filters until backtests and dry-run data justify promotion.
