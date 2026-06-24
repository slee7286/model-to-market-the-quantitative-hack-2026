# MT5 FX/Crypto Bot

Dry-run-first, guarded-live-capable Python bot for the Model to Market hackathon trading competition.

The active integrated build is constrained to these `rules.md`-allowed FX and crypto instruments only. Metals are allowed by the rulebook, but intentionally excluded from this implementation:

- `AUD/USD`
- `EUR/CHF`
- `EUR/GBP`
- `EUR/USD`
- `GBP/USD`
- `USD/CAD`
- `USD/CHF`
- `USD/JPY`
- `BAR/USD` as HBAR/Hedera per `information.md`
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

Dry-run remains the default path. Guarded live trading now exists as a separate runner, `scripts/run_bot_live.py`, but it fails closed unless `LIVE_APPROVED=true` and `config/LIVE_APPROVED.json` are both present. Do not run live orders without explicit go-live approval.

## Current Scope

The project currently includes the Python package scaffold, typed configuration and schemas, read-only MT5 connection verification, read-only symbol discovery/metadata bootstrap, SQLite storage, live/read-only market data collection, deterministic feature engineering, an offline backtester, the `momo_v1` strategy engine, a pre-trade risk engine, dry-run execution, guarded live execution, reconciliation helpers, and an offline analytics/retuning proposal loop.

The main remaining work is deployment and presentation: sponsor integrations, read-only demo/report generation, readiness review, post-round/final reports, and the final Northflank 24/7 path so the bot can operate without the laptop staying open.

## Setup

Use Python 3.11 on the Windows MT5 host:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[dev]
```

For later MT5 read-only prompts on Windows, install the optional MT5 extra:

```powershell
pip install -e .[dev,mt5]
```

Create a local `.env` from `.env.example` and fill credentials only on the local machine. Do not commit `.env`, MT5 credentials, API keys, tokens, passwords, or account numbers.

## Verification

Run the basic scaffold tests:

```powershell
python -m unittest discover -s tests
```

After installing the `dev` extra, the same tests can be run with pytest:

```powershell
python -m pytest
```

The tests verify that the package and scaffold modules import without requiring MT5 credentials or a MetaTrader terminal.

## MT5 Connection Verification

After installing MT5 on the Windows host and adding local credentials to `.env`, run:

```powershell
python scripts/verify_mt5_connection.py
```

Required local `.env` values:

```text
MT5_PATH=C:\Path\To\terminal64.exe
MT5_LOGIN=your-local-login
MT5_PASSWORD=your-local-password
MT5_SERVER=your-broker-server
TRADE_MODE=dry_run
```

The script uses read-only MT5 calls only: initialize, login, terminal info, account info, last error, and shutdown. It prints sanitized terminal/account status, masks the account login, and never prints the MT5 password.

## Symbol Discovery And Broker Mapping

After the read-only MT5 connection check succeeds, discover the broker's FX and crypto symbol names:

```powershell
python scripts/bootstrap_symbols.py
```

The bootstrap script uses read-only MT5 calls only: initialize, login, `symbols_get`,
`symbol_info`, last error, and shutdown. It only attempts to map these canonical
symbols:

- `AUD/USD`
- `EUR/CHF`
- `EUR/GBP`
- `EUR/USD`
- `GBP/USD`
- `USD/CAD`
- `USD/CHF`
- `USD/JPY`
- `BAR/USD` as HBAR/Hedera per `information.md`
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

It writes:

- `config/symbol_map.json`
- `data/symbol_metadata.json`

`config/symbol_map.json` preserves canonical labels internally and stores the broker
symbol separately. Only entries with `"status": "confirmed"` are usable by later
bot phases. If a symbol is `"ambiguous"`, `"missing"`, or `"invalid_manual"`, inspect
MT5 Market Watch and edit only the relevant broker symbol value, for example:

```json
{
  "canonical_to_broker": {
    "AUD/USD": "AUDUSD",
    "BAR/USD": "HBARUSD",
    "BTC/USD": "BTCUSD",
    "EUR/CHF": "EURCHF",
    "EUR/GBP": "EURGBP",
    "EUR/USD": "EURUSD",
    "ETH/USD": "ETHUSD",
    "GBP/USD": "GBPUSD",
    "SOL/USD": "SOLUSD",
    "USD/CAD": "USDCAD",
    "USD/CHF": "USDCHF",
    "USD/JPY": "USDJPY",
    "XRP/USD": "XRPUSD"
  }
}
```

Then rerun:

```powershell
python scripts/bootstrap_symbols.py
```

The rerun validates that each manual broker symbol is available from MT5 and still
conservatively matches the intended canonical instrument before writing metadata.
Do not add symbols outside the integrated FX/crypto allow-list above.

## Local Storage

The MVP audit store is local SQLite and does not require Northflank:

```python
from mt5_crypto_bot.storage import SQLiteStore

with SQLiteStore("sqlite:///data/trading.db") as store:
    store.initialize_schema()
```

The schema is created idempotently and includes symbol metadata, bars, ticks,
order-book snapshots, signals, risk checks, orders, fills, position snapshots,
account snapshots, and strategy versions. Duplicate bar and tick writes upsert on
their natural keys, so collector reruns do not duplicate market data.

High-volume bar/tick Parquet archiving is optional:

```powershell
pip install -e .[data]
```

If `pyarrow` is absent, the Parquet writer skips cleanly and the SQLite audit
store remains fully usable.

## Market Data Collector

After `config/symbol_map.json` contains confirmed broker mappings, run one
read-only collection cycle:

```powershell
python scripts/run_data_collector.py --once
```

Run a bounded polling session:

```powershell
python scripts/run_data_collector.py --minutes 10 --poll-seconds 5
```

The collector:

- reads only the allowed canonical FX/crypto symbols configured in `TARGET_SYMBOLS`;
- requires each broker mapping to be present and confirmed when bootstrap status
  data is available;
- collects M1 and M5 bars with `copy_rates_from`;
- collects latest ticks with `symbol_info_tick`;
- optionally backfills recent ticks with `copy_ticks_from`;
- optionally samples market depth with `market_book_add`, `market_book_get`, and
  `market_book_release`;
- stores metadata, bars, ticks, spreads, and depth rows in SQLite;
- never calls `order_check` or `order_send`.

Optional flags:

```powershell
python scripts/run_data_collector.py `
  --minutes 10 `
  --poll-seconds 5 `
  --tick-backfill-minutes 2 `
  --include-depth `
  --parquet
```

`--poll-seconds` is constrained to at least 5 seconds to keep polling comfortably
below the `rules.md` API-abuse safe harbor. With all 13 symbols enabled, the
default cycle is tens of MT5 read-only calls every five seconds, far below 500
requests per second.

## Feature Snapshots And Backtests

Export feature snapshots from the local SQLite store:

```powershell
python scripts/export_feature_snapshots.py --latest-only
```

Run the offline backtester on collected local data:

```powershell
python scripts/backtest.py
```

If no local market database or organizer CSV history is available, run a clearly
marked synthetic fixture smoke test:

```powershell
python scripts/backtest.py --fixture
```

The backtester never connects to MT5 and never places orders. It compares the
frozen MVP strategy (`momo_v1`) against volatility-managed momentum, Donchian
trend ensemble, and intraday reversal challengers. Reports are written under
`reports/backtests/` and include return, max drawdown, non-annualized 15-minute
Sharpe approximation, trade count, exposure, turnover, estimated spread/slippage
costs, symbol PnL, and side PnL.

Fixture reports validate mechanics only and are not live-trading evidence.
Challengers remain shadow candidates until real backtest and dry-run evidence
are reviewed in a future human-approved workflow.

## Dry-Run Strategy Engine

After symbol metadata and market data have been collected into the local SQLite
store, run one dry-run strategy cycle:

```powershell
python scripts/run_strategy_once.py
```

The strategy script:

- computes latest M5 feature snapshots from stored bars, ticks, and optional depth;
- applies the frozen `momo_v1` thresholds, BTC regime gates, spread caps, shock
  filter, stale-data gates, and volatility-scaled target leverage;
- stores every generated `Signal` in SQLite with `strategy_version` and full
  feature JSON;
- returns only in-memory `OrderIntent` objects for possible later risk checks;
- never imports MT5, never contacts MT5, and never places orders.

Useful offline options:

```powershell
python scripts/run_strategy_once.py --symbols BTC/USD,ETH/USD
python scripts/run_strategy_once.py --all-snapshots --no-freshness-check
```

`--no-freshness-check` is for offline fixture inspection only. Normal dry-run use
should keep freshness checks enabled so stale bars or ticks produce `BLOCK`
signals rather than entry intents.

## Risk Engine

Raw strategy `OrderIntent` objects are not execution-approved. Pass them through
the risk engine first:

```python
from mt5_crypto_bot.risk import RiskEngine, load_risk_context_from_store

context = load_risk_context_from_store("sqlite:///data/trading.db")
result = RiskEngine().check_order_intents(order_intents, context, store=store)
approved_orders = result.approved_orders
```

The risk engine stores every `RiskCheck` in SQLite and blocks missing or unsafe
state by default. It checks:

- allowed FX/crypto symbols only;
- stale ticks and stale strategy feature timestamps;
- spread bps against the frozen symbol caps;
- gross leverage, per-symbol leverage, margin usage, concentration, and net
  directional exposure;
- PnL-sprint drawdown handling, where drawdown alone no longer blocks entries;
- minimum stop distance;
- broker volume min/max/step;
- local kill switch state.

The current aggressive competition profile blocks projected gross leverage above
`28x` and projected margin usage above `90%`. This sits below the 30x account
maximum; on a 30x account the margin guard can bind before the full 28x gross
target is reached. The
strategy no longer uses low per-symbol leverage clamps; concentration and net
direction are still tracked against the `rules.md` time-based discipline bands,
while drawdown and Sharpe are not optimization targets for the qualification
PnL sprint.

When there is exactly one fresh high-conviction entry, the strategy may emit a metadata-tagged discipline-ballast
order first: roughly 11% opposite-direction exposure in another eligible
low-spread symbol, paired with an 89% main leg. This preserves intended gross
exposure while reducing single-instrument and net-direction concentration; it
does not bypass freshness, spread, margin, leverage, stop-distance, kill-switch,
or live-approval checks.

Broker `volume_max` metadata is treated as a maximum submitted order size, not
as a maximum aggregate position size. If a target position or settlement close
requires more than the broker's per-order maximum, the bot splits it into
multiple order intents capped at `volume_max` instead of shrinking the desired
holding to 100 units.

Print current MT5 account and risk state with read-only MT5 calls:

```powershell
python scripts/print_risk_state.py
```

This command reads account info, positions, symbol metadata, and latest ticks.
It does not call `order_check`, does not call `order_send`, and still requires
`TRADE_MODE=dry_run`.

## Dry-Run Execution Engine

Execution accepts only risk-approved orders. The normal boundary is the
`RiskApprovedOrder` object returned by the risk engine:

```python
from mt5_crypto_bot.execution import ExecutionEngine

execution = ExecutionEngine()
result = execution.execute_approved_order(approved_order, store=store)
```

In `dry_run`, the engine:

- persists the approved `OrderIntent` in SQLite;
- builds and stores the future MT5 request payload for auditability;
- records an `ExecutionResult` with status `dry_run`;
- sets filled volume to zero;
- does not call MT5 `order_check`;
- does not call MT5 `order_send`.

The module also contains guarded live execution helpers and read-only reconciliation helpers for `positions_get` and `history_deals_get`. The shared `BotConfig` still rejects `TRADE_MODE=live`; live execution is enabled only inside the separate guarded runner after approval gates pass.

## Guarded Live Runner

Live execution uses a separate command:

```powershell
python scripts/run_bot_live.py --minutes 30 --poll-seconds 15 --kill-switch-file config/KILL_SWITCH
```

It will not place orders unless all live gates pass:

- `LIVE_APPROVED=true` is set in the runtime shell;
- `config/LIVE_APPROVED.json` exists, is valid JSON, and contains `live_approved=true` or `approved=true`;
- the approval scope includes every requested symbol;
- requested runtime does not exceed approval `max_minutes`, when provided;
- broker symbol mapping and fresh MT5 data are available;
- risk checks approve an order;
- `order_check` succeeds before `order_send`.

The dry-run runner is never converted into live mode. Keep `.env` as `TRADE_MODE=dry_run` even when using the live runner; the live runner uses its own explicit approval gate.

See [docs/run_live_trading.md](docs/run_live_trading.md) for the full procedure.

## End-To-End Dry Run

Run one full dry-run bot cycle:

```powershell
python scripts/run_bot_dry_run.py --once
```

Run a bounded dry-run session:

```powershell
python scripts/run_bot_dry_run.py --minutes 30 --poll-seconds 15
```

Each cycle attempts read-only MT5 market/account collection when a confirmed
`config/symbol_map.json` and local MT5 setup are available. If MT5 setup is
missing, it falls back safely: first to existing stored data, then to a clearly
labelled synthetic fixture for offline smoke validation. The fallback exists so
the full audit path can still be tested without credentials or live access.

The end-to-end runner:

- collects latest read-only market, account, and position state where available;
- computes completed-M5 feature snapshots;
- stores `Signal` rows from `momo_v1`;
- creates strategy `OrderIntent` objects only in memory;
- stores mandatory `RiskCheck` decisions;
- records risk-approved orders as `dry_run` execution rows in SQLite;
- does not call MT5 `order_check`;
- does not call MT5 `order_send`.

For a deterministic offline smoke run:

```powershell
python scripts/run_bot_dry_run.py --once --fixture
```

Use `--no-fixture-fallback` when you want missing MT5 setup or missing symbol
mapping to fail instead of using the non-live fixture fallback.

## Offline Analytics And Improvement Loop

Generate an offline analytics report from the local SQLite audit store:

```powershell
python scripts/run_analytics.py
```

Write a deterministic report suffix for an unattended run:

```powershell
python scripts/run_analytics.py --run-id 20260622_161444
```

The analytics script reads local storage only. It never connects to MT5, never
calls `order_check`, never calls `order_send`, and never changes the live or
dry-run strategy automatically.

Run the full continuous-improvement packet after a live or dry-run session:

```powershell
python scripts/run_continuous_improvement.py
```

For a fast during-run/advisory snapshot, skip the heavier backtest pass:

```powershell
python scripts/run_continuous_improvement.py --skip-backtest --run-id cycle_snapshot
```

The continuous-improvement loop combines analytics, threshold recommendation,
champion/challenger evaluation, inactive strategy proposals, and a review-only
candidate `.env` snippet under `reports/continuous_improvement/`. It does not
modify `.env`, does not open MT5, and does not promote a candidate automatically.

The guarded live runner can also trigger the loop:

```powershell
python scripts/run_bot_live.py --minutes 30 --poll-seconds 15 --improvement-after-run
```

During a longer run, use lightweight read-only snapshots every N cycles:

```powershell
python scripts/run_bot_live.py --minutes 120 --poll-seconds 15 --improvement-every-cycles 20
```

Live sessions include an exact-intent retry guard. If the same order intent has
already failed a risk check, live precheck, `order_check`, or `order_send`, later
cycles suppress that unchanged duplicate for a cooldown window and print a
`SUPPRESS` line instead of repeatedly creating new risk checks or broker
attempts. A new feature bar, changed price/volume/stops, or expired cooldown is
treated as a fresh intent.

Reports are written under `reports/analytics/` and include:

- return, max drawdown, and non-annualized 15-minute Sharpe;
- trade count, real fill count, and dry-run order count;
- symbol attribution and side attribution;
- signal bucket performance;
- spread and slippage proxy diagnostics;
- strategy block, risk reject, and order reject reasons;
- champion/challenger shadow evaluation when stored bars are available;
- inactive coarse-grid parameter proposals.

Parameter proposals use only coarse grids around the frozen `momo_v1`
parameters:

- `entry_threshold`: `1.0`, `1.25`, `1.5`
- `exit_threshold`: `0.05`, `0.10`, `0.15`, `0.25`, `0.35`, `0.5`, `0.75`
- `atr_stop_multiple`: `1.2`, `1.6`, `2.0`
- `take_profit_multiple`: `1.8`, `2.4`, `3.0`

The proposal loop does not automatically increase `risk_per_trade`, gross
leverage, symbol leverage, or margin usage. Proposed rows are stored in
`strategy_versions` with `active=0`, no approver, and no approval timestamp.

Current finals live baseline is `ENTRY_THRESHOLD=1.25` and
`EXIT_THRESHOLD=0.15` with `DYNAMIC_EXIT_LEVELS=true`. The live sizing envelope
is a 28x gross-leverage cap with a 90% margin-usage cap. Fresh BAR and XRP
entries are disabled, fresh SOL shorts are disabled, and near-cap sizing is
reserved for strong same-direction signal alignment. Live approval gates remain
unchanged.

Manual approval workflow:

1. Review the inactive `strategy_versions` rows created by analytics.
2. Backtest the candidate on non-fixture history and compare it with `momo_v1`.
3. Run a bounded dry-run/shadow session and inspect risk blocks and spread costs.
4. Reject any candidate that increases leverage, margin usage, risk per trade,
   or symbol caps without explicit human approval.
5. Promote a candidate only through a separate manual approval change that marks
   exactly one strategy version active.

## Project Layout

```text
src/mt5_crypto_bot/
  __init__.py
  app.py
  constants.py
  config.py
  schemas.py
  mt5_client.py
  symbols.py
  storage.py
  data_collector.py
  dry_run.py
  live.py
  features.py
  strategy.py
  risk.py
  execution.py
  analytics.py
  continuous_improvement.py
  retune.py
  thresholds.py
  reporting.py
  logging_setup.py
scripts/
  verify_mt5_connection.py
  bootstrap_symbols.py
  run_data_collector.py
  export_feature_snapshots.py
  backtest.py
  run_analytics.py
  run_continuous_improvement.py
  recommend_thresholds.py
  run_strategy_once.py
  run_bot_live.py
  run_bot_dry_run.py
  run_bot_live.py
  print_risk_state.py
tests/
  test_package_import.py
  test_features.py
  test_backtest.py
  test_strategy.py
  test_risk.py
  test_execution.py
  test_analytics.py
  test_continuous_improvement.py
  test_thresholds.py
  test_live.py
```

## Northflank 24/7 Plan

Local MT5 Python execution depends on the Windows MetaTrader 5 terminal, so a normal Northflank Linux container should not be assumed to run the local terminal directly. The final deployment step should use one of these routes:

| Route | Use When | Notes |
| --- | --- | --- |
| Northflank support services | Need Postgres, dashboard, reports, analytics, and logs while execution remains on a Windows MT5 host | Lowest implementation risk; does not remove laptop dependency unless the Windows host is cloud/always-on. |
| Northflank worker plus MT5 cloud bridge | Need true 24/7 operation without laptop | Use a verified bridge such as MetaApi with a custom MT5 provisioning profile and `servers.dat` for the private competition server. Keep the same live approval gates and mocked tests. |

The final playbook prompt is Prompt 23: Northflank 24/7 deployment. It should add any cloud bridge adapter, Northflank deployment docs, secret-group plan, Postgres setup, worker/dashboard commands, health checks, kill-switch behavior, and mocked tests without running live orders. See [docs/northflank_24_7_deployment.md](docs/northflank_24_7_deployment.md) for the current plan.

## Safety Notes

- `rules.md` is the highest-priority source of truth.
- Only the 13 active FX/crypto instruments may be enabled.
- API polling must stay comfortably below the safe-harbor threshold in `rules.md`.
- Sponsor integrations are optional and must not sit in the blocking execution path.
- LLM outputs may summarize or propose ideas, but must not directly control live trading.
- No fully autonomous live parameter changes are allowed.
- `scripts/run_bot_live.py` is the only local live runner and must remain approval-gated.
