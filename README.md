# MT5 Crypto Bot

Dry-run-first Python scaffold for the Model to Market hackathon crypto bot.

The project is constrained to the allowed crypto instruments from `rules.md`:

- `BAR/USD` as HBAR/Hedera per `information.md`
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

Live trading is not enabled in this scaffold. All execution work defaults to `dry_run` until a separate future live-approval workflow is implemented and explicitly approved.

## Current Scope

The project currently includes the dry-run-first package scaffold, typed configuration and schemas, a read-only MT5 connection verification script, read-only symbol discovery/metadata bootstrap, local SQLite storage, and a read-only market data collector. It intentionally does not implement order construction, execution, or live trading.

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

After the read-only MT5 connection check succeeds, discover the broker's crypto symbol names:

```powershell
python scripts/bootstrap_symbols.py
```

The bootstrap script uses read-only MT5 calls only: initialize, login, `symbols_get`,
`symbol_info`, last error, and shutdown. It only attempts to map these canonical
symbols:

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
    "BAR/USD": "HBARUSD",
    "BTC/USD": "BTCUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
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
Do not add symbols outside the five allowed crypto instruments.

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

- reads only the allowed canonical crypto symbols configured in `TARGET_SYMBOLS`;
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
below the `rules.md` API-abuse safe harbor. With all five symbols enabled, the
default cycle is tens of MT5 read-only calls every five seconds, far below 500
requests per second.

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
  features.py
  strategy.py
  risk.py
  execution.py
  analytics.py
  retune.py
  reporting.py
  logging_setup.py
scripts/
  verify_mt5_connection.py
  bootstrap_symbols.py
  run_data_collector.py
tests/
  test_package_import.py
```

Later prompts will fill these modules in order:

1. Features, backtesting, strategy, risk, and dry-run execution.

## Safety Notes

- `rules.md` is the highest-priority source of truth.
- Only the five allowed crypto instruments may be enabled.
- API polling must stay comfortably below the safe-harbor threshold in `rules.md`.
- Sponsor integrations are optional and must not sit in the blocking execution path.
- LLM outputs may summarize or propose ideas, but must not directly control live trading.
- No fully autonomous live parameter changes are allowed.
