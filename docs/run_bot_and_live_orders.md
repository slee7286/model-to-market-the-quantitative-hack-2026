# MT5 Crypto Bot Runbook: Dry Run, Readiness, and Live-Order Procedure

This guide is the operational runbook for the current MT5 crypto bot. It follows `rules.md` as the highest-priority source of truth. If any previous blueprint or implementation note conflicts with `rules.md`, use `rules.md` and document the conflict before changing code.

The current repository is complete for end-to-end dry-run operation and includes a separate guarded live runner. `scripts/run_bot_dry_run.py` remains dry-run only. `scripts/run_bot_live.py` is the only local script that can call MT5 `order_check` and `order_send`, and it fails closed unless `LIVE_APPROVED=true` and `config/LIVE_APPROVED.json` are both present. `BotConfig` still rejects `TRADE_MODE=live`; keep `.env` as `TRADE_MODE=dry_run`.

## 1. Safety Contract

| Area | Required Behavior |
| --- | --- |
| Instruments | Trade only `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, and `XRP/USD`. |
| Default mode | Use dry-run or paper mode unless running the separate guarded live runner after approval. |
| Live orders | Do not place live orders unless the user explicitly gives a separate go-live approval and both live gates are present. |
| Polling | Keep polling conservative. The dry-run runner enforces a minimum of 5 seconds; 15 seconds is the recommended default. |
| Rules priority | `rules.md` overrides `mt5_crypto_trading_blueprint.md`, strategy docs, prompts, and external docs. |
| Learning loop | Do not allow autonomous online learning to change live trading behavior without validation and human approval. |
| Secrets | Never commit `.env`, MT5 credentials, API keys, tokens, passwords, account numbers, or approval artifacts. |

Competition-relevant risk limits from `rules.md`:

| Rule Area | Rule Threshold | Bot Internal Guard |
| --- | ---: | ---: |
| Max leverage | 30x account max; penalties begin above 28x | Blocks projected gross leverage above 8x |
| Margin usage | penalties begin above 90% | Blocks new risk above 60% projected margin usage |
| Single-instrument exposure | penalty above 90% for 30 minutes | Blocks above 75% projected share |
| Net directional exposure | penalty above 95% for 30 minutes | Blocks above 85% projected share |
| API abuse | safe harbor at or below 500 requests/second, still reviewable if abusive | Default dry-run cadence is 15 seconds |

## 2. What The Bot Does Now

The end-to-end dry-run runner is:

```powershell
python scripts/run_bot_dry_run.py --once
```

One cycle performs this pipeline:

1. Load configuration from `.env` and process environment variables.
2. Attempt read-only MT5 collection if MT5 and `config/symbol_map.json` are available.
3. If MT5 setup is unavailable and fallback is allowed, seed deterministic synthetic fixture data.
4. Store market/account snapshots in SQLite.
5. Compute completed-bar feature snapshots.
6. Generate `momo_v1` strategy signals.
7. Create order intents for eligible entry signals.
8. Run risk checks before any execution record is created.
9. Record approved orders as dry-run execution rows.
10. Print a human-readable summary showing what would have traded and why.

The dry-run script explicitly does not call MT5 `order_check` or `order_send`.

## 3. Project Setup

Use Python 3.11 or 3.12 if available, because `pyproject.toml` declares `>=3.11,<3.13`.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

For development tests, install the optional dev dependency group:

```powershell
python -m pip install -e ".[dev]"
```

For MT5 integration on Windows:

```powershell
python -m pip install -e ".[mt5]"
```

If `pytest` is not installed, use `unittest`:

```powershell
python -m unittest tests.test_dry_run tests.test_execution tests.test_config_and_schemas
```

## 4. Environment Variables

Create `.env` locally. This file is gitignored and must stay local.

```dotenv
TRADE_MODE=dry_run
TARGET_SYMBOLS=BAR/USD,BTC/USD,ETH/USD,SOL/USD,XRP/USD
DATABASE_URL=sqlite:///data/trading.db

# Optional, needed only for read-only MT5 collection.
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=
MT5_PATH=
MT5_TIMEOUT_MS=60000
```

Do not put secrets in Markdown files, screenshots, commit messages, issue comments, or terminal output that will be shared.

## 5. MT5 Read-Only Setup

The bot can run offline with fixture data, but real competition operation needs MT5 read-only collection first.

User actions:

1. Install MetaTrader 5 on Windows.
2. Log into the simulated competition account in the MT5 terminal.
3. Add MT5 credentials and terminal path to `.env`.
4. Verify the connection:

```powershell
python scripts/verify_mt5_connection.py
```

5. Bootstrap the broker symbol map:

```powershell
python scripts/bootstrap_symbols.py
```

6. Inspect `config/symbol_map.json`. Every enabled target symbol must map to a confirmed broker symbol before relying on live MT5 data.

If `config/symbol_map.json` is missing, `python scripts/run_bot_dry_run.py --once` will safely fall back to fixture data unless `--no-fixture-fallback` is supplied.

## 6. Dry-Run Commands

Deterministic offline smoke run:

```powershell
python scripts/run_bot_dry_run.py --once --fixture
```

Default one-cycle run:

```powershell
python scripts/run_bot_dry_run.py --once
```

Thirty-minute bounded session:

```powershell
python scripts/run_bot_dry_run.py --minutes 30 --poll-seconds 15
```

Force failure instead of fixture fallback when MT5 setup is incomplete:

```powershell
python scripts/run_bot_dry_run.py --once --no-fixture-fallback
```

Use a separate test database:

```powershell
python scripts/run_bot_dry_run.py --once --fixture --database-url sqlite:///data/dry_run_test.db
```

Optional market-depth collection is read-only, but should remain off unless needed:

```powershell
python scripts/run_bot_dry_run.py --minutes 30 --poll-seconds 15 --include-depth
```

## 7. Expected Output

A successful one-cycle run prints:

```text
End-to-end dry-run completed.
Cycles: 1
Signals stored: ...
Order intents generated: ...
Risk checks stored: ...
Risk-approved dry-run orders: ...
Dry-run execution records: ...
No MT5 order_check or order_send calls are made by this script.
```

The last-cycle summary includes:

| Field | Meaning |
| --- | --- |
| `data_mode` | `mt5_live_read_only`, `stored_data`, `synthetic_fixture_fallback`, or `synthetic_fixture_forced`. |
| `collection.request_count` | Number of read-only MT5 collection requests. Fixture mode uses zero MT5 requests. |
| `strategy.signals` | Signals generated for the five allowed instruments. |
| `strategy.order_intents` | Raw strategy intents before risk checks. |
| `risk.risk_checks` | Risk checks persisted to the database. |
| `risk.approved_orders` | Orders approved for dry-run recording. |
| `execution.dry_run` | Approved orders recorded as simulated execution rows. |

Signal lines show the symbol, decision, direction, score, target leverage, target volume, and reason.

Risk lines show whether each order intent passed and why blocked orders were blocked.

Execution lines show dry-run order records and confirm `sent_to_mt5=false` and `order_send_called=false`.

## 8. Database Tables

Default database:

```text
data/trading.db
```

Important tables:

| Table | Purpose |
| --- | --- |
| `symbol_metadata` | Broker metadata such as point, digits, spread, volume limits, and filling mode. |
| `bars` | OHLCV bars from MT5 or deterministic fixture data. |
| `ticks` | Latest tick data and synthetic fixture ticks. |
| `order_book_snapshots` | Optional read-only market depth snapshots when enabled. |
| `account_snapshots` | Equity, balance, margin, leverage, drawdown, and account state. |
| `positions_snapshots` | Open-position snapshots from MT5 or fixture state. |
| `signals` | Strategy decisions for each target symbol. |
| `risk_checks` | Pre-trade risk decisions. |
| `orders` | Order intents and dry-run execution results. Dry-run execution records are rows with `status='dry_run'`. |
| `fills` | Future/live reconciliation data from MT5 history reads. |
| `strategy_versions` | Strategy parameter/version audit records. |

Inspect core counts:

```powershell
python -c "import sqlite3; con=sqlite3.connect('data/trading.db'); tables=('signals','risk_checks','orders','account_snapshots'); [print(t, con.execute(f'select count(*) from {t}').fetchone()[0]) for t in tables]"
```

Inspect recent dry-run orders:

```powershell
python -c "import sqlite3; con=sqlite3.connect('data/trading.db'); rows=con.execute(\"select client_order_id,symbol,status,requested_volume,requested_price from orders order by updated_at_utc desc limit 10\").fetchall(); [print(r) for r in rows]"
```

## 9. Strategy And Risk Behavior

The current strategy is `momo_v1`, frozen in `docs/strategy_design_freeze.md`:

```text
Volatility-managed multi-horizon crypto momentum
with BTC regime filtering, alt beta-adjusted relative strength,
EMA and Donchian trend confirmation, ATR exits,
spread/liquidity filters, and strict rules-aligned risk caps.
```

Instrument treatment:

| Symbol | Role | Starting Cap |
| --- | --- | ---: |
| `BTC/USD` | Regime anchor and core trend instrument | 2.00x hard symbol cap |
| `ETH/USD` | Liquid high-beta core alt | 2.00x hard symbol cap |
| `SOL/USD` | Higher-beta momentum sleeve | 1.50x hard symbol cap |
| `XRP/USD` | Event-sensitive alt | 1.25x hard symbol cap |
| `BAR/USD` | HBAR/Hedera idiosyncratic sleeve | 0.75x hard symbol cap |

Activation gates block entries when required data is missing or stale:

1. Confirmed broker symbol mapping.
2. Allowed canonical symbol.
3. Complete symbol metadata.
4. At least 96 completed M5 bars for the traded symbol.
5. At least 96 completed M5 bars for `BTC/USD` before alt signals are tradable.
6. Fresh M5 bar and fresh tick.
7. Finite spread below symbol cap.
8. Portfolio risk checks pass.
9. Local kill switch is inactive for new exposure.

## 10. Kill Switch

The dry-run runner accepts:

```powershell
python scripts/run_bot_dry_run.py --minutes 30 --poll-seconds 15 --kill-switch-file config/KILL_SWITCH
```

Create the file to block new exposure:

```powershell
New-Item -ItemType File -Path config/KILL_SWITCH -Force
```

Remove it after review:

```powershell
Remove-Item -LiteralPath config/KILL_SWITCH
```

The kill switch should be used before editing risk code during an active session.

## 11. Testing And Verification

Run focused smoke tests:

```powershell
python scripts/run_bot_dry_run.py --once --fixture
python scripts/run_bot_dry_run.py --once
python -m unittest tests.test_dry_run tests.test_execution tests.test_config_and_schemas
```

Run broader tests:

```powershell
python -m unittest discover -s tests
```

Expected safety assertions:

| Check | Expected Result |
| --- | --- |
| `scripts/run_bot_dry_run.py --once` | Completes without live orders. |
| `tests.test_dry_run` | Verifies signals, risk checks, and dry-run orders are stored. |
| `tests.test_execution` | Verifies dry-run does not call `order_check` or `order_send`. |
| `tests.test_config_and_schemas` | Verifies invalid symbols and `TRADE_MODE=live` fail fast. |

## 12. Dry-Run Operating Procedure

Before a hackathon session:

1. Pull the latest code and confirm `git status --short` is clean or understood.
2. Confirm `.env` exists locally and has `TRADE_MODE=dry_run`.
3. Run:

```powershell
python scripts/run_bot_dry_run.py --once --fixture
```

4. If using MT5 data, run:

```powershell
python scripts/verify_mt5_connection.py
python scripts/bootstrap_symbols.py
python scripts/run_bot_dry_run.py --once --no-fixture-fallback
```

5. Start the bounded session:

```powershell
python scripts/run_bot_dry_run.py --minutes 30 --poll-seconds 15
```

During the session:

1. Watch the console summary.
2. Keep `poll-seconds` at 15 unless there is a documented reason to change it.
3. Do not disable freshness checks except for offline fixture inspection.
4. Do not edit `.env` to use live mode.
5. Create `config/KILL_SWITCH` if behavior looks unsafe.

After the session:

1. Export or inspect database rows.
2. Review blocked risk checks.
3. Compare signal reasons with realized market movement.
4. Retune only offline, then rerun tests and dry-run validation.

## 13. Live-Order Procedure

Live orders are available only through the separate guarded runner:

```powershell
python scripts/run_bot_live.py --minutes 30 --poll-seconds 15
```

Do not use `scripts/run_bot_dry_run.py` for live trading. It remains dry-run only.

The live runner fails closed unless all of these are true:

1. `rules.md` has been reviewed and the target symbols are only `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, and `XRP/USD`.
2. MT5 credentials, broker symbol map, symbol metadata, bars, ticks, and account state are live and fresh.
3. `LIVE_APPROVED=true` is set in the runtime environment.
4. `config/LIVE_APPROVED.json` exists and contains `live_approved=true` or `approved=true`.
5. The requested `--minutes` is positive and, if the approval file has `max_minutes`, does not exceed it.
6. The approval-file `scope`, when present, includes every requested symbol.

Required live gate:

```powershell
$env:LIVE_APPROVED = "true"
```

Local approval file:

```json
{
  "live_approved": true,
  "approved_by": "slee7",
  "approved_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "scope": ["BAR/USD", "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"],
  "max_minutes": 30,
  "notes": "Explicit user-approved guarded live session."
}
```

The approval file path is:

```text
config/LIVE_APPROVED.json
```

It is gitignored and should never be committed.

Live runner behavior:

| Requirement | Reason |
| --- | --- |
| No fixture fallback | Live execution must use real, fresh MT5 data only. |
| Freshness checks always enabled | Prevents orders from stale bars or ticks. |
| Confirmed symbol map required | Avoids routing to the wrong broker symbol. |
| `order_check` before `order_send` | Catches invalid volume, filling mode, margin, or stop levels. |
| Store every request and result | Auditability for hackathon review. |
| Bounded runtime | Prevents unattended live exposure. |
| Kill switch support | Allows immediate blocking of new exposure. |
| Conservative polling | Avoids abusive request patterns. |

Do not convert `scripts/run_bot_dry_run.py` into a live runner. Keep dry-run and live execution as separate commands so the default path remains safe.

## 14. Model Improvement Loop

The MVP learning loop is offline and approval-based:

1. Collect market data, signals, risk checks, and dry-run execution records.
2. Review performance attribution by symbol, side, signal score, spread, and risk-block reason.
3. Retune thresholds or caps offline.
4. Backtest or dry-run the challenger parameter set.
5. Compare challenger against the current champion on return, max drawdown, 15-minute Sharpe, trade count, and risk discipline.
6. Promote only after human approval.

Generate the offline analytics package:

```powershell
python scripts/run_analytics.py
```

The report is written under `reports/analytics/` and stores coarse-grid retuning
proposals as inactive `strategy_versions` rows. The analytics script does not
activate any proposal and does not increase leverage, margin usage, risk per
trade, or symbol caps.

Never let the model self-modify live parameters based only on recent performance. Recent crypto behavior can be noisy, and automatic parameter changes can overfit or increase exposure exactly when the system is least reliable.

## 15. Troubleshooting

| Symptom | Likely Cause | Action |
| --- | --- | --- |
| `symbol map not found` | `config/symbol_map.json` has not been created | Run `scripts/bootstrap_symbols.py` after MT5 credentials are configured, or use fixture fallback for offline smoke tests. |
| `TRADE_MODE=live` validation error | Shared config intentionally rejects live config | Keep `.env` as `dry_run`; use `scripts/run_bot_live.py` only after live approval gates are present. |
| No order intents | Strategy scores are inside thresholds, data is stale, or activation gates failed | Inspect signal reasons and feature freshness. |
| Risk checks block orders | Internal leverage, concentration, margin, spread, stale-data, or stop-distance guard triggered | Inspect `risk_checks.reason`; do not loosen caps without evidence. |
| `pytest` missing | Dev dependencies are not installed | Use `python -m unittest ...` or install `python -m pip install -e ".[dev]"`. |
| MT5 import fails | `MetaTrader5` package is not installed or not on Windows | Install `python -m pip install -e ".[mt5]"` on Windows. |

## 16. Completion Checklist

The bot is ready for dry-run demos when all of these pass:

```powershell
python scripts/run_bot_dry_run.py --once --fixture
python scripts/run_bot_dry_run.py --once
python -m unittest tests.test_dry_run tests.test_execution tests.test_config_and_schemas
```

Dry-run completion criteria:

| Criterion | Status To Verify |
| --- | --- |
| Signals stored | Console summary shows `Signals stored` greater than zero. |
| Risk checks stored | Console summary shows `Risk checks stored` greater than zero when order intents exist. |
| Dry-run execution records | Console summary shows `Dry-run execution records` for approved orders. |
| No live orders | Console confirms no MT5 `order_check` or `order_send` calls. |
| Database audit trail | `signals`, `risk_checks`, and `orders` contain recent rows. |
| Rules compliance | Only the five allowed crypto instruments appear in output and database rows. |

Guarded live readiness criteria:

| Criterion | Status To Verify |
| --- | --- |
| Separate runner | Use `scripts/run_bot_live.py`, not `scripts/run_bot_dry_run.py`. |
| Approval gates | Missing `LIVE_APPROVED=true` or missing/invalid `config/LIVE_APPROVED.json` fails before MT5 order APIs. |
| Broker sequence | Live execution calls `order_check` before `order_send`. |
| Failed check behavior | Failed `order_check` stores a rejection and does not call `order_send`. |
| Audit trail | Successful fake live sends are stored as `trade_mode='live'` execution rows. |
| Rules compliance | Only the five allowed crypto instruments are requested. |
