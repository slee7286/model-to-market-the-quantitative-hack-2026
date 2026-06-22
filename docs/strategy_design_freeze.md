# Strategy Design Freeze

Run ID: `20260622_031000`

This document freezes the first code-ready strategy specification for the MT5 crypto bot. It is documentation only: no strategy, risk, or execution code is implemented in this phase.

Current status update: the strategy described here has since been implemented through dry-run execution and offline analytics, and a separate guarded live runner now exists. The freeze itself remains the source for `momo_v1` strategy/risk intent. Any live session must still use `scripts/run_bot_live.py` with `LIVE_APPROVED=true` and `config/LIVE_APPROVED.json`; unattended automation must not create those gates or place live orders.

The strategy is constrained to the allowed crypto instruments only:

- `BAR/USD` as HBAR/Hedera per `information.md`
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

`rules.md` remains the highest-priority source of truth. All unattended execution must remain dry-run, paper, read-only, or test/report generation. Guarded live execution is available only through the separate approval-gated live runner.

## Data Availability Audit

Prompt 08 was intended to run after the collector had some data. In this unattended run, the collector implementation exists, but confirmed broker mappings and collected market data are not present yet.

| Artifact | Expected Path | Status | Strategy Impact |
| --- | --- | --- | --- |
| Confirmed symbol map | `config/symbol_map.json` | Missing | No symbol may be traded or collected until exact MT5 broker symbols are confirmed. |
| Symbol metadata JSON | `data/symbol_metadata.json` | Missing | Contract size, tick size, point, spread, volume step, filling mode, and margin fields are unknown. |
| SQLite market database | `data/trading.db` | Missing | No observed bars, ticks, spread distribution, depth, account snapshots, or positions are available for empirical calibration. |
| Backtest reports | `reports/backtests/` | Missing | Strategy remains research-backed and code-ready, but not backtest-validated in this workspace. |
| Order book snapshots | `order_book_snapshots` table | No database present | Order-book imbalance must remain a shadow-only optional filter. |

Safe fallback: freeze the research-backed MVP with explicit activation gates. No strategy output may become an order intent until broker mapping, metadata, recent bars, and current tick freshness checks are available.

## Activation Gates

The strategy engine may emit `HOLD` or `BLOCK` signals with missing data, but it must not emit entry order intents unless all of these gates pass:

1. `config/symbol_map.json` exists and every enabled target symbol has `status=confirmed`.
2. The symbol is one of `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, or `XRP/USD`.
3. Latest symbol metadata exists for the symbol and includes at least `broker_symbol`, `digits`, `point`, `trade_contract_size`, `volume_min`, `volume_max`, `volume_step`, `trade_mode`, and `filling_mode`.
4. At least 96 completed M5 bars exist for the traded symbol.
5. At least 96 completed M5 bars exist for `BTC/USD` before any altcoin signal is tradable.
6. The latest completed M5 bar is not older than 15 minutes.
7. The latest tick is not older than 120 seconds.
8. Current bid and ask are present and produce a finite spread in basis points.
9. Current spread is below the symbol cap in this document.
10. The risk engine confirms projected leverage, margin usage, concentration, net direction, volume rounding, stop distance, stale data, and kill-switch checks.

If any gate fails, the strategy decision is `BLOCK` with a reason. Existing future positions, if live trading is ever separately approved, should still be eligible for risk-reduction exits when state is reliable.

## Selected MVP Variant

Freeze `momo_v1` as:

```text
Volatility-managed multi-horizon crypto momentum
with BTC regime filtering, alt beta-adjusted relative strength,
EMA and Donchian trend confirmation, ATR exits,
spread/liquidity filters, and strict rules-aligned risk caps.
```

This matches `docs/strategy_research.md` and the blueprint direction. Because no live metadata is available in this workspace, there is no empirical reason to replace the research-backed MVP with a different strategy.

## Explicit Deviations From Blueprint

| Area | Freeze Decision | Reason |
| --- | --- | --- |
| BAR/HBAR risk cap | Hard cap `0.75x` symbol leverage, normal cap `0.50x`, and block until spread data is acceptable. | Research has limited direct HBAR support and no local metadata is present. This is stricter than the blueprint and rules-compliant. |
| Signal formula weights | Use the research document's balanced M15/H1, EMA, Donchian, slope, and volume weights. | Prompt 01 refined the blueprint formula using external research. |
| Gross leverage stretch | MVP hard cap remains `8.0x`; no 10x-12x stretch is enabled by this freeze. | No collected data or dry-run evidence exists to justify higher leverage. |
| Order-book imbalance | Shadow-only optional filter until stable `market_book_get()` data exists. | Depth availability is unverified. This avoids fragile or high-frequency behavior. |

## Instrument Roles

| Symbol | Role | Trading Bias | Starting Treatment |
| --- | --- | --- | --- |
| `BTC/USD` | Regime anchor and core instrument | Trade own trend both long and short. Use as market state for all alts. | Highest liquidity assumption, max symbol leverage `2.0x`, spread cap `8 bps`. |
| `ETH/USD` | Liquid high-beta core alt | Trade momentum when own signal confirms or relative strength is strong. | Max symbol leverage `2.0x`; reduce stacking with BTC when net directional exposure is high. |
| `SOL/USD` | Higher-beta momentum sleeve | Trade only with no strong opposing BTC regime. | Normal cap `1.25x`, hard cap `1.50x`, spread cap `15 bps`. |
| `XRP/USD` | Event-sensitive alt | Trade momentum only with tighter jump and regime filters. | Normal cap `1.00x`, hard cap `1.25x`, spread cap `15 bps`; no exposure increase after shock bars. |
| `BAR/USD` | HBAR/Hedera idiosyncratic sleeve | Trade only if mapping, metadata, spread, and volume are acceptable. | Normal cap `0.50x`, hard cap `0.75x`, spread cap `25 bps`; block if depth/spread is unstable. |

## Timeframes And Cadence

| Item | Starting Value | Rationale |
| --- | --- | --- |
| Collection timeframes | M1 and M5 bars | M1 supports execution context; M5 is the primary signal timeframe. |
| Signal timeframe | Completed M5 bars only | Avoids tick-level noise and look-ahead bias. |
| Confirmation timeframes | M15 and H1, derived from completed M5 bars | Aligns with competition 15-minute Sharpe and higher-level regime detection. |
| Signal cadence | Once per completed M5 bar per symbol | Prevents overtrading and keeps request patterns conservative. |
| Minimum feature warmup | 96 completed M5 bars | Covers EMA80, 48-bar Donchian, beta, volatility, and z-score windows. |
| Tick freshness limit | 120 seconds | Blocks stale entries without requiring high-frequency polling. |
| Bar freshness limit | Latest completed M5 no older than 15 minutes | Blocks strategy decisions during collection gaps. |
| Collector polling | Minimum 5 seconds if running live collection | Far below the `rules.md` 500 requests/second safe harbor. |

## Feature Specification

All features must be computed only from data available at or before the completed signal bar close. Rolling high/low and Donchian breakout thresholds must use windows shifted by one bar where needed so the current close is not included in the breakout boundary.

### Price And Return Features

| Feature | Starting Definition | Rationale |
| --- | --- | --- |
| `ret_1_m5` | Close-to-close return over 1 M5 bar | Immediate momentum and shock detection. |
| `ret_3_m5` | Close-to-close return over 3 M5 bars | 15-minute competition-aligned momentum. |
| `ret_12_m5` | Close-to-close return over 12 M5 bars | 1-hour trend input. |
| `ret_48_m5` | Close-to-close return over 48 M5 bars | 4-hour trend context. |
| `z_ret_3_m5` | Rolling z-score of `ret_3_m5` over 96 bars, min 48 | Normalizes signal strength by recent behavior. |
| `z_ret_12_m5` | Rolling z-score of `ret_12_m5` over 96 bars, min 48 | Main momentum input. |
| `high_low_position_48` | `(close - rolling_low_48) / (rolling_high_48 - rolling_low_48)` | Detects range position and breakout pressure. |

### Trend Features

| Feature | Starting Definition | Rationale |
| --- | --- | --- |
| `ema_8`, `ema_20`, `ema_55`, `ema_80` | Exponential moving averages on M5 closes | Multi-horizon trend backbone. |
| `ema20_minus_ema80_over_atr` | `(ema_20 - ema_80) / ATR14` | Trend distance normalized by current volatility. |
| `z_ema20_minus_ema80_over_atr` | Rolling z-score over 96 bars, min 48 | Makes trend distance comparable across symbols. |
| `ema20_slope_6_over_atr` | `(ema_20 - ema_20.shift(6)) / ATR14` | Direction and persistence of trend. |
| `z_ema20_slope_6_over_atr` | Rolling z-score over 96 bars, min 48 | Normalizes slope signal. |
| `donchian_12`, `donchian_24`, `donchian_48` | `+1` if close breaks above prior upper channel, `-1` if below prior lower channel, else range score from `-0.5` to `+0.5` | Captures 1h, 2h, and 4h breakouts without a single brittle lookback. |
| `donchian_ensemble` | Average of `donchian_12`, `donchian_24`, and `donchian_48` | Smooths trend signal. |

### Volatility And Shock Features

| Feature | Starting Definition | Rationale |
| --- | --- | --- |
| `atr_14` | Average true range over 14 M5 bars | Stop, take-profit, normalization, and shock input. |
| `rv_12_m5` | Std dev of M5 returns over 12 bars | 1-hour realized volatility. |
| `rv_24_m5` | Std dev of M5 returns over 24 bars | 2-hour realized volatility. |
| `rv_48_m5` | Std dev of M5 returns over 48 bars | Main volatility-scaling input. |
| `rv_1h_equiv` | `rv_48_m5 * sqrt(12)` | Converts M5 return volatility to a 1-hour equivalent. |
| `shock_flag` | `abs(ret_1_m5) > max(3 * rv_48_m5, shock_floor)` | Blocks entries immediately after abnormal jumps. |

Shock floors:

| Symbol Group | `shock_floor` | Rationale |
| --- | --- | --- |
| `BTC/USD`, `ETH/USD` | `0.0075` | Liquid core assets should not need new entries after abrupt 0.75% M5 moves. |
| `SOL/USD`, `XRP/USD` | `0.0125` | Allows higher beta while still blocking jump chasing. |
| `BAR/USD` | `0.0150` | HBAR may be noisier, but risk cap remains small. |

### Liquidity And Execution Features

| Feature | Starting Definition | Rationale |
| --- | --- | --- |
| `spread` | `ask - bid` from latest tick | Direct trading cost input. |
| `spread_bps` | `(ask - bid) / ((ask + bid) / 2) * 10000` | Comparable spread filter across symbols. |
| `tick_age_seconds` | Current UTC time minus latest tick UTC time | Stale data block. |
| `volume_zscore` | Rolling z-score of M5 tick volume over 96 bars, min 48 | Confirms trend participation. Neutral `0` if unavailable. |
| `metadata_complete` | Required symbol metadata fields all present | Prevents sizing when contract specs are unknown. |
| `book_imbalance` | `(top5_bid_volume - top5_ask_volume) / (top5_bid_volume + top5_ask_volume)` | Optional shadow filter only when depth is stable. |

### BTC Regime And Relative Strength

| Feature | Starting Definition | Rationale |
| --- | --- | --- |
| `btc_trend_score` | BTC trend score using the same formula as BTC MVP signal before regime gates | Single crypto market anchor. |
| `btc_regime` | `risk_on` if `btc_trend_score >= 0.75`, `risk_off` if `<= -0.75`, else `neutral` | Simple and auditable regime state. |
| `beta_to_btc` | Rolling 96-bar covariance of alt and BTC M5 returns divided by BTC variance, min 48, clipped to `[-2.5, 3.5]` | Avoids mistaking BTC beta for independent alt strength. |
| `relative_ret_12_m5` | `asset_ret_12_m5 - beta_to_btc * btc_ret_12_m5` | 1-hour beta-adjusted alt strength. |
| `relative_score` | Rolling z-score of `relative_ret_12_m5` over 96 bars, min 48 | Alt ranking and sizing modifier. |
| `cross_sectional_rank` | Rank of final raw score across active symbols | Used for exposure prioritization when risk budget is scarce. |

## Signal Formula

For every enabled symbol on each completed M5 bar:

```text
trend_score =
    0.25 * z_ret_3_m5
  + 0.25 * z_ret_12_m5
  + 0.20 * z_ema20_minus_ema80_over_atr
  + 0.15 * donchian_ensemble
  + 0.10 * z_ema20_slope_6_over_atr
  + 0.05 * volume_confirmation
```

`volume_confirmation` is:

```text
+1.0  if volume_zscore >= 0.5 and trend_score before volume is positive
-1.0  if volume_zscore >= 0.5 and trend_score before volume is negative
 0.0  otherwise
```

For `BTC/USD`:

```text
final_score_raw = trend_score
```

For `ETH/USD`, `SOL/USD`, `XRP/USD`, and `BAR/USD`:

```text
final_score_raw = 0.75 * trend_score + 0.25 * relative_score
```

Optional order-book adjustment is disabled for MVP trading. In shadow mode only:

```text
if abs(book_imbalance) >= 0.15 and sign(book_imbalance) == sign(final_score_raw):
    shadow_final_score = final_score_raw + 0.10 * sign(final_score_raw)
elif abs(book_imbalance) >= 0.15 and sign(book_imbalance) != sign(final_score_raw):
    shadow_final_score = final_score_raw - 0.10 * sign(final_score_raw)
else:
    shadow_final_score = final_score_raw
```

## Entry Rules

Long entry is allowed only when all conditions hold:

1. `final_score_raw >= 1.25`.
2. M5 close is above `ema_80`.
3. `ema_20` slope over 6 bars is non-negative.
4. `shock_flag` is false.
5. Current `spread_bps` is at or below the symbol spread cap.
6. Latest tick and latest completed M5 bar pass freshness gates.
7. The BTC regime gate passes for the symbol.
8. The symbol is not already at or above target long exposure.
9. Stop distance is at least `max(3 * spread, 5 * point)`.
10. All portfolio and order risk checks pass.

Short entry is allowed only when all conditions hold:

1. `final_score_raw <= -1.25`.
2. M5 close is below `ema_80`.
3. `ema_20` slope over 6 bars is non-positive.
4. `shock_flag` is false.
5. Current `spread_bps` is at or below the symbol spread cap.
6. Latest tick and latest completed M5 bar pass freshness gates.
7. The BTC regime gate passes for the symbol.
8. The symbol is not already at or above target short exposure.
9. Stop distance is at least `max(3 * spread, 5 * point)`.
10. All portfolio and order risk checks pass.

BTC regime gates:

| Symbol | Long Gate | Short Gate | Rationale |
| --- | --- | --- | --- |
| `BTC/USD` | No external gate | No external gate | BTC defines the regime. |
| `ETH/USD` | Block if BTC regime is `risk_off` | Block if BTC regime is `risk_on` | Avoid fighting strong broad crypto trend. |
| `SOL/USD` | Block if BTC regime is `risk_off` | Block if BTC regime is `risk_on` | Higher beta makes regime conflict costly. |
| `XRP/USD` | Require BTC trend score `> -0.25` | Require BTC trend score `< 0.25` | Event-sensitive asset gets stricter conflict filter. |
| `BAR/USD` | Require BTC trend score `> -0.25` | Require BTC trend score `< 0.25` | Idiosyncratic/liquidity risk gets stricter conflict filter. |

## Exit And Reduction Rules

For long positions, exit or reduce when any condition is true:

- `final_score_raw <= 0.35`.
- M5 close crosses below `ema_20`.
- Stop-loss is reached.
- Take-profit is reached.
- Trailing stop is reached after activation.
- Position age exceeds 180 minutes.
- Current spread exceeds `1.5 * spread_cap_bps`.
- Latest tick age exceeds 300 seconds or latest M5 bar is older than 20 minutes.
- Drawdown, margin, leverage, concentration, net direction, or kill-switch guard triggers.

For short positions, use the symmetric conditions:

- `final_score_raw >= -0.35`.
- M5 close crosses above `ema_20`.
- Stop-loss, take-profit, trailing stop, time stop, stale data, spread, or portfolio guard triggers.

Risk-reduction exits should be allowed even when normal entry spread caps fail, but only if current state is reliable enough to size and route the reduction safely through an explicitly approved live workflow.

## Stops, Take-Profits, And Time Stops

| Parameter | Starting Value | Rationale |
| --- | --- | --- |
| ATR period | 14 M5 bars | Standard short-horizon volatility proxy. |
| Stop-loss distance | `1.6 * ATR14` | Balances whipsaw risk and loss control. |
| Take-profit distance | `2.4 * ATR14` | 1.5:1 reward-to-risk starting profile. |
| Trailing stop activation | Unrealized profit at least `1.0 * ATR14` | Avoids trailing immediately after entry. |
| Trailing stop distance | `1.2 * ATR14` | Locks gains while allowing trend continuation. |
| Minimum stop distance | `max(3 * current_spread, 5 * point)` | Avoids stops inside noise/spread. |
| Time stop | 180 minutes | Prevents stale intraday positions. |
| Price rounding | Round stops and targets to symbol `point` | Required for broker-valid requests. |

Long prices:

```text
long_stop = entry_price - 1.6 * ATR14
long_take_profit = entry_price + 2.4 * ATR14
```

Short prices:

```text
short_stop = entry_price + 1.6 * ATR14
short_take_profit = entry_price - 2.4 * ATR14
```

## Spread And Liquidity Caps

| Symbol | Entry Spread Cap | Abnormal Spread Exit Flag | Rationale |
| --- | --- | --- | --- |
| `BTC/USD` | `8 bps` | `12 bps` | Core liquid anchor. |
| `ETH/USD` | `8 bps` | `12 bps` | Core liquid alt. |
| `SOL/USD` | `15 bps` | `22.5 bps` | Higher beta and potentially wider spread. |
| `XRP/USD` | `15 bps` | `22.5 bps` | Event-sensitive; do not chase wide markets. |
| `BAR/USD` | `25 bps` | `37.5 bps` | Small capped sleeve; block if spread is unstable. |

If live data later shows a symbol's median spread is consistently above its cap, do not loosen the cap automatically. Move that symbol to shadow-only until backtests or dry-run evidence justify a documented change.

## Volatility Scaling And Position Sizing

All sizing is capped by stop-risk, symbol leverage, gross leverage, margin usage, net direction, and concentration. If metadata needed for sizing is missing, emit `BLOCK`.

Starting sizing formula:

```text
score_scale = clamp((abs(final_score_raw) - 1.25) / (2.25 - 1.25), 0.0, 1.0)
risk_fraction = 0.0025 + 0.0025 * score_scale
```

This gives a starting risk fraction from `0.25%` to `0.50%` of equity per trade before drawdown and volatility scaling.

Volatility scale:

```text
vol_scale = clamp(target_rv_1h / max(rv_1h_equiv, volatility_floor), 0.35, 1.25)
```

Drawdown scale:

```text
drawdown_scale = 1.00  if total_drawdown < 0.03
drawdown_scale = 0.50  if 0.03 <= total_drawdown < 0.05
drawdown_scale = 0.25  if 0.05 <= total_drawdown < 0.08
drawdown_scale = 0.00  if total_drawdown >= 0.08
```

Risk dollars:

```text
risk_dollars = equity * risk_fraction * vol_scale * drawdown_scale
```

Stop-based notional must be estimated with MT5 metadata. Future code should use broker metadata and, where available in a safe read-only/check path, `order_calc_profit` or equivalent risk math before any live-approved order. Never assume MT5 volume units equal coins.

Starting volatility targets:

| Symbol | `target_rv_1h` | `volatility_floor` | Rationale |
| --- | --- | --- | --- |
| `BTC/USD` | `0.0080` | `0.0025` | Core asset with meaningful intraday movement. |
| `ETH/USD` | `0.0085` | `0.0025` | Similar core liquidity, modestly higher beta. |
| `SOL/USD` | `0.0110` | `0.0035` | Higher beta and higher normal volatility. |
| `XRP/USD` | `0.0100` | `0.0035` | Event-sensitive but liquid enough for capped risk. |
| `BAR/USD` | `0.0070` | `0.0030` | Smaller sleeve; avoid levering quiet/wide markets. |

## Risk Caps

These caps are intentionally below `rules.md` penalty zones: margin penalties start above 90%, leverage penalties above 28x, single-instrument concentration above 90%, and net directional exposure above 95%.

| Risk Control | Starting Limit | Hard Behavior | Rationale |
| --- | --- | --- | --- |
| Gross leverage target | `5.0x` | Block projected exposure above `8.0x` | Far below 28x penalty zone. |
| Margin usage warning | `50%` | No new risk if projected margin usage exceeds `60%` | Far below 90% penalty zone. |
| Single-instrument share target | `65%` | Block projected share above `75%` | Below 90% penalty zone. |
| Net directional exposure target | `75%` | Block projected share above `85%` | Below 95% penalty zone. |
| Max open positions | 5 | One position per allowed symbol | Prevents duplicated exposure. |
| Normal drawdown guard | `5%` total drawdown | Defensive sizing | Preserves capital. |
| No-new-risk drawdown | `8%` total drawdown | Entry risk fraction becomes zero | Avoids drawdown spiral. |
| Hard risk-reduction drawdown | `10%` total drawdown | Reduce/flatten when live-approved mechanics exist | Survival over signal. |
| Round/daily drawdown guard | `5%` | Defensive sizing | Aligns with elimination cadence. |
| Kill switch | Local flag or config state | Block entries and allow reductions only | Manual safety override. |

Symbol leverage caps:

| Symbol | Normal Cap | Hard Cap | Rationale |
| --- | --- | --- | --- |
| `BTC/USD` | `1.75x` | `2.00x` | Anchor and likely best liquidity. |
| `ETH/USD` | `1.75x` | `2.00x` | Core alt, correlated with BTC. |
| `SOL/USD` | `1.25x` | `1.50x` | Higher beta. |
| `XRP/USD` | `1.00x` | `1.25x` | Jump/event risk. |
| `BAR/USD` | `0.50x` | `0.75x` | HBAR metadata and liquidity unknown. |

## Candidate Strategies For Shadow Mode Only

| Challenger | Shadow Signal | Promotion Requirement |
| --- | --- | --- |
| Donchian trend ensemble | Use only 12/24/48 M5 Donchian breakouts with volatility sizing and the same risk caps. | Must beat `momo_v1` after costs on backtest and dry-run shadow without worse drawdown or concentration. |
| Intraday momentum/reversal timing | Test continuation after aligned 3-bar/12-bar momentum and reversal after shock bars. | Must improve 15-minute Sharpe and not reduce return materially. |
| Order-book imbalance filter | Apply top-5 depth imbalance threshold `abs(imbalance) >= 0.15` as signal confirmation/contradiction. | MT5 depth must be stable for all active symbols and improve net-of-spread shadow results. |
| Beta-neutral alt relative strength | Rank alts by beta-adjusted 1h relative return and trade strongest/weakest with smaller net BTC exposure. | Requires enough data for stable beta and must not increase turnover or concentration excessively. |
| News/sentiment context | Batch classify headlines or reports for XRP/SOL/BAR event-risk annotations. | May inform reports or shadow flags only; LLM output must not directly control orders. |

No challenger may be promoted automatically. Promotion requires an inactive strategy version, backtest or live-shadow evidence, and human approval in a future workflow.

## Backtest And Live Shadow Requirements

Before this strategy can be considered for live readiness, later prompts should validate:

1. Feature calculations have no look-ahead bias.
2. Backtests execute at next bar open or next available bid/ask, never same-bar close.
3. Costs include at least half-spread on entry and exit plus symbol-specific slippage assumptions.
4. Reports show return, max drawdown, non-annualized 15-minute Sharpe, trade count, exposure, turnover, symbol PnL, side PnL, spread costs, and risk-discipline estimates.
5. Dry-run/live-shadow logs store every signal, block reason, risk check, and simulated execution.
6. BAR/HBAR remains disabled or capped until observed spread and metadata are acceptable.
7. Any parameter change from this freeze is documented before code or activation.

## Implementation Checklist For Later Prompts

The next implementation prompts should encode this freeze exactly:

- Compute M5, M15, and H1 features from completed bars only.
- Use `BTC/USD` as the mandatory regime anchor for all alt signals.
- Emit `BLOCK` when required symbol metadata or market data is missing.
- Preserve canonical symbols internally even if broker symbols differ.
- Enforce spread, volatility, stale-data, shock, and risk gates before order intents.
- Store all features used for each signal in JSON for auditability.
- Keep order-book imbalance disabled for MVP trading and shadow-only until verified.
- Keep every path dry-run or paper by default and never call MT5 `order_send` in unattended automation.

## Freeze Conclusion

The MVP strategy is frozen as `momo_v1`, a volatility-managed multi-horizon momentum strategy with BTC regime gating, alt relative strength, ATR exits, spread filters, and conservative risk caps. The design is specific enough to implement, but this workspace currently lacks confirmed symbol mappings, broker metadata, and collected market data. Until those artifacts exist, the correct behavior is to block tradable entries and continue with feature implementation, backtesting, mocked tests, and read-only collection tooling.
