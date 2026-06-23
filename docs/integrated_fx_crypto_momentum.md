# Integrated FX/Crypto Momentum System

This document records the active design for the unified MT5 live bot. `rules.md`
is the highest-priority source of truth.

## Active Instrument Universe

Trade only:

- FX: `AUD/USD`, `EUR/CHF`, `EUR/GBP`, `EUR/USD`, `GBP/USD`, `USD/CAD`, `USD/CHF`, `USD/JPY`
- Crypto: `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, `XRP/USD`

Metals are allowed by `rules.md` but intentionally excluded from this build.

## Strategy Summary

The active `momo_v1` strategy is a unified volatility-scaled momentum system:

- FX pairs use time-series trend/momentum only.
- `BTC/USD` uses its own trend/momentum score.
- Crypto alts use their own trend score plus BTC-relative strength and BTC regime gates.
- Entries require feature readiness, fresh ticks, fresh completed M5 bars, acceptable spread, valid symbol metadata, stop/TP construction, and risk approval.
- Oversized orders are split into per-order chunks using broker `volume_max`; total holdings are not capped at `volume_max`.
- A small opposite-direction discipline ballast leg may be emitted when one high-conviction entry would otherwise dominate the basket.

The strategy remains deterministic and auditable. There is no autonomous online learning that changes live behavior.

## Parameter Mapping

| Asset Class | Score Logic | Spread Caps | Volatility Target | Leverage Cap |
| --- | --- | --- | --- | --- |
| Major FX | Own M5/M15/H1 trend score | `2-4 bps` by pair | `0.0007-0.0012` 1h-equivalent | `28x` symbol cap, subject to portfolio/margin gates |
| BTC | Own M5/M15/H1 trend score | `8 bps` | `0.0080` 1h-equivalent | `27x` symbol cap |
| Crypto alts | Own trend plus BTC-relative/regime context | `8-25 bps` by symbol | `0.0070-0.0110` 1h-equivalent | `27x` symbol cap |

The portfolio gross cap is `28x`; projected margin usage above `90%` is still
blocked. On a 30x account the margin guard can bind before full 28x gross
exposure.

## Research Rationale

The integrated system combines two simple, defensible research-backed ideas:

1. Time-series momentum can be effective in liquid FX and crypto when scaled by realized volatility.
2. Crypto cross-sectional structure is heavily BTC-regime dependent, so BTC remains the anchor for crypto alt filters.

Implementation deliberately avoids fragile ML classifiers during the hackathon
window. The practical edge is fast, auditable execution with good spread,
freshness, liquidity, margin, and retry controls.

## Unified Live Loop

One `scripts/run_bot_live.py` session processes all 13 instruments:

1. Validate `LIVE_APPROVED=true` and `config/LIVE_APPROVED.json`.
2. Load confirmed broker mappings from `config/symbol_map.json`.
3. Collect MT5 bars, ticks, metadata, account state, positions, and optional depth.
4. Compute M5 feature snapshots.
5. Generate one latest signal per symbol.
6. Build order intents, including optional ballast and per-order chunks.
7. Suppress exact duplicate failed intents during cooldown.
8. Run risk checks with projected portfolio exposure.
9. Call `order_check` before `order_send` for every live-approved order.
10. Store orders, fills, positions, account snapshots, risk checks, and signals.

## Operational Notes

- Run `python scripts/bootstrap_symbols.py` again after this integration; old
  symbol maps may only contain the five crypto mappings.
- Update `TARGET_SYMBOLS` and `config/LIVE_APPROVED.json` scope if running all
  13 symbols.
- Keep `TRADE_MODE=dry_run`; live execution is enabled only by the guarded live
  runner and approval artifacts.
- Do not run more aggressive polling than the existing 15-second live cadence
  unless there is a measured reason and API behavior remains conservative.

