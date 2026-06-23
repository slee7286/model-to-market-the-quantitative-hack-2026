# MT5 Crypto Bot Runbook: Guarded Live Trading on MetaTrader 5

This guide is the operational runbook for the **guarded live runner** that actually places orders on MetaTrader 5. It is the live-trading companion to `docs/run_bot_and_live_orders.md`, which covers dry-run operation and readiness. As with that document, `rules.md` is the highest-priority source of truth. If any blueprint, strategy doc, or implementation note conflicts with `rules.md`, follow `rules.md` and document the conflict before changing code.

Live trading is **off by default and fails closed**. The shared `BotConfig` rejects `TRADE_MODE=live` outright (`src/mt5_crypto_bot/config.py`). The only path that can call MT5 `order_check` and `order_send` is the separate script `scripts/run_bot_live.py`, and it refuses to run unless every approval gate below passes. Never use `scripts/run_bot_dry_run.py` for live trading; it is dry-run only.

> Do not place live orders unless the user has given an explicit, separate go-live approval. Configuring the gates described here **is** that approval action; do not perform these steps speculatively.

## 1. Safety Contract

| Area | Required Behavior |
| --- | --- |
| Instruments | Trade only `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, and `XRP/USD`. |
| Live runner | Use `scripts/run_bot_live.py` only. Never repurpose `scripts/run_bot_dry_run.py`. |
| Approval | Live orders require `LIVE_APPROVED=true` **and** a valid `config/LIVE_APPROVED.json`. |
| Data freshness | Live cycles always enforce freshness and never use fixture fallback. |
| Bounded runtime | `--minutes` is required and positive; the approval file may cap it via `max_minutes`. |
| Polling | Minimum cycle interval is 5 seconds; 15 seconds is the default and recommended value. |
| Broker sequence | Every order calls `order_check` before `order_send`; a failed check never sends. |
| Secrets | Never commit `.env`, MT5 credentials, or `config/LIVE_APPROVED.json`. |
| Rules priority | `rules.md` overrides blueprints, strategy docs, prompts, and external docs. |

Competition-relevant risk limits from `rules.md` (enforced by the same `RiskEngine` used in dry-run):

| Rule Area | Rule Threshold | Bot Internal Guard |
| --- | ---: | ---: |
| Max leverage | 30x account max; penalties begin above 28x | Blocks projected gross leverage above 27x |
| Margin usage | penalties begin above 90% | Blocks projected margin usage above 90% |
| Single-instrument exposure | penalty above 90% for 30 minutes | Tracks time over threshold and blocks added exposure after the soft window |
| Net directional exposure | penalty above 95% for 30 minutes | Tracks time over threshold and blocks added exposure after the soft window |
| API abuse | safe harbor at or below 500 requests/second | Default live cadence is 15 seconds |

## 2. What The Live Runner Does

`scripts/run_bot_live.py` calls `run_live_session` in `src/mt5_crypto_bot/live.py`, which loops over `run_live_cycle` until the bounded `--minutes` window expires. Each cycle performs this pipeline:

1. **Validate live approval first.** `_require_live_approval` constructs an `ExecutionEngine(trade_mode="live")` and calls `require_live_approval()` before any data is collected. This checks `LIVE_APPROVED=true`, the existence and validity of `config/LIVE_APPROVED.json`, the approval `scope` against the requested symbols, and `max_minutes` against `--minutes`. If any check fails, the cycle raises `LiveTradingApprovalError` and **no MT5 order APIs are touched**.
2. **Collect fresh, real MT5 data.** `collect_market_account_once` reads bars, ticks, symbol metadata, account state, and positions. There is no fixture fallback in this path.
3. **Compute features** with `require_data=True` (freshness enforced).
4. **Generate `momo_v1` signals** with `enforce_freshness=True` and `latest_only=True`, recording the active strategy version (`approved_by="guarded_live"`).
5. **Run retry suppression, then risk checks.** The session-level `OrderRetryGuard` skips exact duplicate order intents that already failed risk/execution in the same run. Changed signals, changed prices, changed volumes, or expired cooldowns are evaluated normally. Remaining intents go through `RiskEngine` and the kill-switch file. Only approved orders proceed.
6. **Execute approved orders** through `_execute_live_approved_orders`: initialize and log into MT5, then for each approved order call `order_check`, and only on a passing retcode call `order_send`. Every request and result is stored.
7. **Reconcile** by reading open positions (`positions_get`) and recent deal history (`history_deals_get`) into the database. These are read-only.
8. After the session, print cycle counts and the last-cycle summary as JSON.

If there are no risk-approved orders in a cycle, the execution step returns an empty result and MT5 trading APIs are not called for that cycle. If an unchanged failed intent is suppressed, terminal output shows a `SUPPRESS` line with the prior failure reason and the `suppressed_until_utc` timestamp instead of creating another duplicate risk-check/order attempt.

## 3. Project Setup

Use Python 3.11 or 3.12 (`pyproject.toml` declares `>=3.11,<3.13`). Live trading runs on the **Windows MT5 host**.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[mt5]"
```

The `[mt5]` extra installs the `MetaTrader5` package, which is required for live execution. If it is missing, `load_mt5_module()` raises `MT5DependencyError` and no MT5 calls are made.

## 4. Environment Variables

Create `.env` locally (gitignored). Note that `TRADE_MODE` stays `dry_run` even for live trading. The live runner enables live execution through a separate `ExecutionEngine(trade_mode="live")` override, and `build_mt5_credentials` actually *requires* `TRADE_MODE=dry_run` in shared config. The `LIVE_APPROVED` flag, not `TRADE_MODE`, is the live gate.

```dotenv
TRADE_MODE=dry_run
TARGET_SYMBOLS=BAR/USD,BTC/USD,ETH/USD,SOL/USD,XRP/USD
DATABASE_URL=sqlite:///data/trading.db

# Required for live execution: real MT5 terminal credentials.
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
MT5_TIMEOUT_MS=60000
```

The live gate is set **only in the runtime shell**, never committed to `.env`:

```powershell
$env:LIVE_APPROVED = "true"
```

Do not put secrets in Markdown files, screenshots, commit messages, issue comments, or shared terminal output.

## 5. MT5 Connection And Symbol Map

Live execution requires a confirmed broker symbol map. `build_mt5_order_request` raises if a broker symbol is missing (`require_broker_symbol=True` in the live path).

1. Install and log into the competition account in the MetaTrader 5 terminal.
2. Add MT5 credentials and `MT5_PATH` to `.env`.
3. Verify the read-only connection:

```powershell
python scripts/verify_mt5_connection.py
```

4. Bootstrap the symbol map:

```powershell
python scripts/bootstrap_symbols.py
```

5. Confirm every enabled target symbol in `config/symbol_map.json` maps to a real broker symbol. The live runner loads this map with `load_confirmed_symbol_map(..., target_symbols=...)`, which fails if any requested symbol is unmapped.

## 6. The Live Approval File

Create `config/LIVE_APPROVED.json`. It is gitignored and must never be committed.

```json
{
  "live_approved": true,
  "approved_by": "slee7",
  "approved_at_utc": "2026-06-22T12:00:00Z",
  "scope": ["BAR/USD", "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"],
  "max_minutes": 30,
  "notes": "Explicit user-approved guarded live session."
}
```

| Field | Effect |
| --- | --- |
| `live_approved` / `approved` | Either must be truthy (`true`, `1`, `yes`, `on`, `approved`), or approval fails. |
| `scope` | If present, must include every requested symbol; otherwise `LiveTradingApprovalError`. |
| `max_minutes` | If present, `--minutes` must not exceed it; otherwise `LiveTradingApprovalError`. |
| `approved_by`, `approved_at_utc`, `notes` | Audit metadata; not enforced but recommended. |

The default path is `config/LIVE_APPROVED.json`; override with `--approval-file`.

## 7. Running A Live Session

Required arguments are `--minutes`. All other flags have safe defaults.

Minimal bounded live session (15-second cadence):

```powershell
$env:LIVE_APPROVED = "true"
python scripts/run_bot_live.py --minutes 30
```

Explicit poll interval and a kill switch:

```powershell
python scripts/run_bot_live.py --minutes 30 --poll-seconds 15 --kill-switch-file config/KILL_SWITCH
```

Restrict to a subset of approved symbols:

```powershell
python scripts/run_bot_live.py --minutes 15 --symbols "BTC/USD,ETH/USD"
```

Key flags (`scripts/run_bot_live.py`):

| Flag | Default | Purpose |
| --- | --- | --- |
| `--minutes` | required | Positive bounded live runtime. Must be less than or equal to approval `max_minutes`. |
| `--poll-seconds` | 15 | Seconds between cycles. Minimum 5. |
| `--env-file` | `.env` | Local dotenv path. |
| `--database-url` | from config | SQLite database URL/path. |
| `--symbol-map` | `config/symbol_map.json` | Confirmed canonical-to-broker map. |
| `--symbols` | `TARGET_SYMBOLS` | Comma-separated allowed canonical symbols. |
| `--bar-count` | `DEFAULT_BAR_COUNT` | Recent bars requested per symbol. |
| `--tick-backfill-minutes` | none | Optional recent tick backfill. |
| `--include-depth` | off | Optional read-only market depth collection. |
| `--kill-switch-file` | `config/KILL_SWITCH` | Blocks new exposure when present. |
| `--approval-file` | `config/LIVE_APPROVED.json` | Live approval artifact. |
| `--log-level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |

## 8. Expected Output And Exit Codes

A successful session prints:

```text
Guarded live run completed.
Cycles: ...
Signals stored: ...
Order intents generated: ...
Risk checks stored: ...
Risk-approved live orders: ...
MT5 order_send results recorded: ...
Last cycle summary:
{ ... JSON ... }
```

`MT5 order_send results recorded` is the count of orders for which `order_send` returned a result (`sent_to_mt5` in the execution summary).

Exit codes from `main`:

| Code | Condition | Output Channel |
| --- | --- | --- |
| 0 | Session completed. | stdout |
| 2 | `ValidationError` (shared config tried to use live). | stderr |
| 2 | `LiveTradingApprovalError` (approval gate failed; no MT5 order calls made). | stderr |
| 2 | `ValueError` / `LiveRunError` (bad args, stale data, missing symbol map, etc.). | stderr |

When approval fails, the script prints `No order_check or order_send calls were made.` to confirm the fail-closed behavior.

## 9. Execution Status Semantics

Live execution records are written by `ExecutionEngine._execute_live_guarded` with `trade_mode='live'`:

| `status` | Meaning |
| --- | --- |
| `rejected` | `order_check` failed; `order_send` was not called. `order_check_called=true`, `order_send_called=false`. |
| `filled` | `order_send` returned `TRADE_RETCODE_DONE`/`PLACED`. |
| `partial` | `order_send` returned `TRADE_RETCODE_DONE_PARTIAL`. |
| `failed` | `order_send` returned any other retcode. |

A passing `order_check` is one whose retcode is `0`, `TRADE_RETCODE_DONE` (10009), or `TRADE_RETCODE_PLACED` (10008). Stored result payloads always include `order_check` and, when sent, `order_send` raw broker responses for audit.

## 10. Database Tables

Default database: `data/trading.db`. Live runs reuse the same schema as dry-run, plus reconciliation tables that matter for live:

| Table | Purpose |
| --- | --- |
| `orders` | Order intents and execution results. Live rows have `trade_mode='live'` and statuses from section 9. |
| `positions_snapshots` | Open positions read via `positions_get` after each execution batch. |
| `fills` | Deal history read via `history_deals_get`. |
| `risk_checks` | Pre-trade risk decisions (blocked and approved). |
| `signals` | Strategy decisions per symbol. |
| `account_snapshots` | Equity, balance, margin, leverage, drawdown. |

Inspect recent live orders:

```powershell
python -c "import sqlite3; con=sqlite3.connect('data/trading.db'); rows=con.execute(\"select client_order_id,symbol,status,trade_mode,requested_volume from orders where trade_mode='live' order by updated_at_utc desc limit 10\").fetchall(); [print(r) for r in rows]"
```

## 11. Kill Switch

The kill switch blocks new exposure during a live session without stopping the process:

```powershell
New-Item -ItemType File -Path config/KILL_SWITCH -Force
```

Remove it after review:

```powershell
Remove-Item -LiteralPath config/KILL_SWITCH
```

Use the kill switch before editing risk code, investigating anomalous fills, or whenever live behavior looks unsafe. To stop entirely, interrupt the process; the bounded `--minutes` window also ends the session automatically.

## 12. Testing And Verification

Run the execution and live-runner tests before any live session:

```powershell
python -m unittest tests.test_execution tests.test_live
```

Key safety assertions:

| Test | Verifies |
| --- | --- |
| `tests.test_execution::test_live_mode_without_approval_does_not_call_order_check_or_order_send` | Missing `LIVE_APPROVED` blocks before any MT5 order call. |
| `tests.test_execution::test_live_approval_requires_valid_file_payload` | Invalid approval JSON fails closed. |
| `tests.test_execution::test_live_order_check_rejection_does_not_call_order_send` | A failed `order_check` never calls `order_send`. |
| `tests.test_execution::test_live_order_check_then_order_send_records_live_result` | A passing check then send records a `live` `filled` row with tickets. |
| `tests.test_live::test_live_cycle_without_approval_does_not_touch_mt5` | An unapproved live cycle raises before initializing MT5. |

These tests use a `FakeMT5`/`FakeLiveMT5` double; they never connect to a real terminal.

## 13. Go-Live Operating Procedure

Before the session:

1. Pull the latest code and confirm `git status --short` is clean or understood.
2. Confirm `.env` has real MT5 credentials and `TRADE_MODE=dry_run`.
3. Verify the connection and symbol map:

```powershell
python scripts/verify_mt5_connection.py
python scripts/bootstrap_symbols.py
```

4. Run a final dry-run sanity cycle on live data:

```powershell
python scripts/run_bot_dry_run.py --once --no-fixture-fallback
```

5. Run the safety tests in section 12.
6. Create `config/LIVE_APPROVED.json` with the correct `scope` and `max_minutes`.
7. Set the runtime gate: `$env:LIVE_APPROVED = "true"`.

During the session:

1. Watch the console summary and the `Last cycle summary` JSON.
2. Keep `--poll-seconds` at 15 unless there is a documented reason to change it.
3. Reconcile `positions_snapshots` and `fills` against the MT5 terminal.
4. Create `config/KILL_SWITCH` immediately if behavior looks unsafe.

After the session:

1. Inspect `orders` rows with `trade_mode='live'` and their `order_check`/`order_send` payloads.
2. Review blocked risk checks and any `rejected`/`failed` executions.
3. Remove `config/KILL_SWITCH` and unset `LIVE_APPROVED` if no longer trading.
4. Revoke or archive `config/LIVE_APPROVED.json` so the next run requires fresh approval.

## 14. Troubleshooting

| Symptom | Likely Cause | Action |
| --- | --- | --- |
| `live execution requires LIVE_APPROVED=true ...` | Runtime gate not set | `$env:LIVE_APPROVED = "true"` in the same shell. |
| `live execution requires approval file ...` | `config/LIVE_APPROVED.json` missing | Create the approval file described in section 6. |
| `live approval file must contain live_approved=true or approved=true` | Approval payload not truthy | Set `live_approved: true` in the file. |
| `live approval scope does not include requested symbols: ...` | `--symbols` outside approval `scope` | Widen `scope` or narrow `--symbols`. |
| `requested live runtime ... exceeds approval max_minutes=...` | `--minutes` too large | Lower `--minutes` or raise `max_minutes`. |
| `Live-run configuration validation failed` | `.env` set `TRADE_MODE=live` | Keep shared config `dry_run`; live is enabled only by the runner. |
| `broker symbol is required ...` | Symbol map missing/unmapped | Run `scripts/bootstrap_symbols.py`; confirm `config/symbol_map.json`. |
| `MetaTrader5 is not installed ...` | `[mt5]` extra missing or non-Windows host | `pip install -e ".[mt5]"` on the Windows MT5 host. |
| `MT5 initialize/login failed` | Wrong `MT5_PATH`, server, or terminal state | Re-run `scripts/verify_mt5_connection.py`; check terminal login. |
| `poll_seconds must be >= 5` | Cadence too aggressive | Use `--poll-seconds 15`. |

## 15. Live Readiness Checklist

The build is ready for a guarded live session when all of these hold:

| Criterion | Status To Verify |
| --- | --- |
| Separate runner | Using `scripts/run_bot_live.py`, not `scripts/run_bot_dry_run.py`. |
| Approval gates | `LIVE_APPROVED=true` set and `config/LIVE_APPROVED.json` valid, scoped, and time-bounded. |
| Connection | `scripts/verify_mt5_connection.py` succeeds. |
| Symbol map | Every requested symbol mapped in `config/symbol_map.json`. |
| Safety tests | `python -m unittest tests.test_execution tests.test_live` passes. |
| Broker sequence | Live execution calls `order_check` before `order_send`; failed checks never send. |
| Audit trail | Successful sends stored as `trade_mode='live'` rows with `order_check`/`order_send` payloads. |
| Rules compliance | Only the five allowed crypto instruments are requested and traded. |
| Bounded + interruptible | `--minutes` set, kill switch path available. |

## 16. Northflank 24/7 Note

The local guarded runner depends on the Windows MetaTrader 5 terminal through the `MetaTrader5` Python package. Do not assume this local terminal path can run directly inside a normal Northflank Linux container.

For 24/7 operation without the laptop, use Prompt 23 in `codex_mt5_crypto_implementation_playbook.md` and the plan in `docs/northflank_24_7_deployment.md`. The preferred route is a Northflank worker plus a verified MT5 cloud bridge, such as MetaApi with a custom provisioning profile and `servers.dat` for the private MT5 server. The fallback route is Northflank Postgres/dashboard/analytics while an always-on Windows MT5 host runs execution.
