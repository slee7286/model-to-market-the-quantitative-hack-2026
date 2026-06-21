# Automated MT5 Crypto Trading Blueprint for Model to Market 2026

## 1. Executive Summary

Build a practical, hackathon-ready automated crypto trading system that trades only:

- `BAR/USD`
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

The recommended MVP is a Python-based MetaTrader 5 execution worker running on a Windows machine or Windows VPS, supported by a lightweight data store, risk engine, scheduled analysis loop, and sponsor-tool integrations for observability, batch research, and demo-quality reporting.

The strategy should be a volatility-scaled, multi-asset crypto momentum system with strict drawdown and leverage guardrails. It is simple enough to implement quickly, defensible to judges, and aligned with the competition scoring:

```text
Final Score = 70% * Return Rank
            + 15% * Drawdown Rank
            + 10% * Sharpe Rank
            + 5% * Risk Discipline
```

Because return rank dominates, the system should take risk when signals are strong. Because forced liquidation and risk penalties can eliminate or damage the score, the system must never run close to 30x leverage, never allow prolonged margin stress, and must log every decision.

The strongest hackathon architecture is:

- MT5 terminal and Python execution bot on Windows.
- Python data and strategy code using the official `MetaTrader5` package.
- SQLite or DuckDB plus Parquet for MVP storage.
- PostgreSQL on Northflank for a stronger demo and reliable central data store.
- Pydantic models for typed signals, orders, risk checks, and logs.
- Pydantic Logfire for traces and observability.
- Doubleword for low-cost batch and async LLM analysis of logs, news, and reports.
- Anthropic Claude for coding assistance, post-trade explanation, risk review, and judge-facing technical writeups.
- Human-approved model or parameter updates only. No blind online self-modification.

## 2. Hackathon Constraints and Assumptions

### Source Priority

Primary sources used:

1. `rules.md`
2. `information.md`
3. `doubleword.md`
4. Official MetaTrader 5 documentation
5. Official MT5 Python integration documentation
6. Public documentation for Doubleword, Pydantic/Logfire, Northflank, and Anthropic

If an external source conflicts with local files, the local files win.

### Competition Assumptions

| Area | Working Assumption |
| --- | --- |
| Account | Simulated USD 1,000,000 account |
| Principal risk | No real principal risk, but real prizes and compliance review apply |
| Trading channel | MT5 selected because API connectivity is required |
| Execution | All automated orders must go through MT5 |
| Instruments | Only `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, `XRP/USD` |
| Crypto availability | Available live, but symbol strings and contract specs must be verified in MT5 |
| Historical data | Provided, but initial crypto history may be incomplete |
| Deployment | External infrastructure is allowed |
| MT5 Python runtime | Provided notes say it appears Windows-only; use Windows for the live execution worker |
| Learning loop | Offline analysis and human-approved parameter promotion only |

### Needs Verification Before Trading

- Exact MT5 symbol names. Some brokers use forms like `BTCUSD`, `BTC/USD`, `BTCUSD.r`, etc.
- `BAR/USD` contract details. `information.md` says `BAR/USD` means HBAR/Hedera.
- Tick size, point value, contract size, min volume, volume step, max volume, margin mode, and allowed filling modes.
- Whether full market depth is available for each crypto instrument.
- Whether the live environment exposes the same depth and historical schema as the test environment.
- Whether Northflank can reach the database securely from the Windows MT5 worker.

## 3. Key Findings from `rules.md`

### Competition Purpose

The event rewards reproducible AI, quant, hybrid, or human-assisted trading systems that generate return while managing risk. Strategy intent is not judged. Objective metrics drive ranking, elimination, and review.

### Account Rules

| Item | Rule |
| --- | --- |
| Account type | Simulated trading account |
| Initial funds | USD 1,000,000 |
| Maximum leverage | 30x |
| Stop-out level | 30% margin level |
| Ranking basis | Equity, return, max drawdown, Sharpe, risk discipline |
| Principal risk | No real principal risk |

### Tradable Crypto Assets

Only these crypto instruments should be used in this blueprint:

- `BAR/USD`
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

### Schedule

| Date | Event |
| --- | --- |
| 21 Jun 2026 22:00 BST | Competition starts |
| 22 Jun 2026 22:00 BST | Round 1 snapshot |
| 23 Jun 2026 22:00 BST | Round 2 snapshot |
| 24 Jun 2026 22:00 BST | Round 3 snapshot |
| 24-26 Jun 2026 | Finals |
| 26 Jun 2026 22:00 BST | Finals end |
| 27 Jun 2026 | Results and awards |

### Scoring

Final score:

```text
Final Score = 70% * Return Rank
            + 15% * Drawdown Rank
            + 10% * Sharpe Rank
            + 5% * Risk Discipline
```

Implication: the bot must pursue return, but not by risking forced liquidation or severe risk-discipline penalties.

### Risk Discipline Penalties

Avoid at all costs:
| Risk Area | Penalty Trigger |
| --- | --- |
| Margin usage | `>90%` for at least 30 min: -20 |
| Margin usage | `>95%` for at least 15 min: -30 |
| Margin usage | `>98%` for at least 10 min: compliance review |
| Leverage | `>28x` for at least 30 min: -20 |
| Leverage | `>29x` for at least 15 min: -30 |
| Leverage | approaching `30x` for at least 10 min: compliance review |
| Single-instrument exposure | `>90%` for at least 30 min: -10 |
| Net directional exposure | `>95%` for at least 30 min: -10 |

### Red Lines

Avoid at all costs:

- Forced liquidation
- API abuse
- Exploiting vulnerabilities
- Multi-accounting
- Unauthorized collusion
- High-frequency request patterns that cause anomalies

Safe harbor says requests at or below 500 per second are not automatically abnormal, but the MVP should be far below this.

## 4. Key Findings from `information.md`

### Trading Channel

- MT5 and AI Native Interface are available.
- If API connectivity is required, choose MT5.
- AI Native does not provide API access or API key creation.
- MT5 supports API integration and external systems.
- Once selected, channel choice is irreversible.

### MT5 Credentials

Programmatic trading uses standard MT5 credentials from Console -> Trading Setup:

- login
- password
- server

There is no separate REST or WebSocket order API key from organizers.

### Execution Model

The competition uses simulated trading with real market quotes and a liquidity-style environment:

- Quotes aggregate multiple broker/liquidity sources.
- Quotes are not skewed per participant.
- Orders are order-book based, not dealing-desk.
- Passive/pending limit orders can rest as liquidity.
- Marketable orders can partially fill.
- Larger orders can consume multiple depth levels.
- Slippage, liquidity limits, and market impact are part of the simulation.
- Open positions remain open if the MT5 terminal disconnects.

### Costs and Constraints

- No commission.
- No swap or overnight financing fees.
- No borrow fees.
- Long and short trading are allowed.
- Stop-losses and standard MT5 functionality are supported.
- No options.

### Market Data

- Historical data is provided for preparation.
- Crypto history may be incomplete initially.
- Live API is expected to stream 5-level depth, but exact availability must be verified.
- Final symbol strings and instrument metadata are available after MT5 login.

### Sponsor Perks

| Sponsor | Local File Detail |
| --- | --- |
| Anthropic | USD 50 API credits |
| Doubleword | Inference API access via Pydantic AI Gateway / Logfire |
| Pydantic | USD 50 Pydantic Logfire inference credits |
| Northflank | USD 100 platform credit |

## 5. Relevant MetaTrader 5 Documentation Summary

### Platform Capabilities

MetaTrader 5 supports:

- Manual trading
- Expert Advisors
- Market and pending orders
- Stop-loss and take-profit orders
- Strategy Tester for MQL5 Expert Advisors
- Multi-symbol testing and optimization
- Real tick or generated tick backtesting modes
- Execution delay simulation in Strategy Tester

For this project, the Python bot should use MT5 as the execution and market-data interface, while Python handles strategy logic, storage, analytics, and the data flywheel.

### Strategy Tester

Official MT5 documentation describes Strategy Tester as a tool for testing and optimizing Expert Advisors before live trading. It supports:

- Multi-currency strategies
- Parameter optimization
- Genetic optimization
- Forward optimization to reduce overfitting
- Real tick mode
- Generated tick modes
- Execution delay simulation
- Custom account settings, including margin and commission

Practical implication: MT5 Strategy Tester is useful if the team writes an MQL5 EA. For a Python-first hackathon MVP, a custom Python backtester over provided CSV/MT5 history is faster and easier.

### Market Depth

MT5 supports Depth of Market access. The Python API exposes this via:

- `market_book_add(symbol)`
- `market_book_get(symbol)`
- `market_book_release(symbol)`

The local notes say live API should stream the same 5-level depth, but this must be verified per symbol.

### Order Execution

MT5 trade requests pass validation stages on the trade server. A successful Python request submission does not guarantee the trade filled at the desired price. The bot must inspect the returned result code, fills, positions, and deal history.

## 6. MetaTrader 5 Python Integration Summary

The official Python package is installed with:

```bash
pip install MetaTrader5
```

Core functions relevant to this project:

| Function | Use |
| --- | --- |
| `initialize()` | Connect to or launch MT5 terminal |
| `login()` | Connect to trading account |
| `shutdown()` | Close MT5 connection |
| `last_error()` | Inspect API errors |
| `account_info()` | Read balance, equity, margin, leverage, account flags |
| `terminal_info()` | Read terminal state |
| `symbols_get()` | Discover instruments |
| `symbol_select()` | Enable a symbol in Market Watch |
| `symbol_info()` | Read contract specs, spread, digits, volume min/max/step |
| `symbol_info_tick()` | Get latest bid/ask/last tick |
| `copy_rates_from()` | Get OHLCV bars |
| `copy_rates_range()` | Get bars by time interval |
| `copy_ticks_from()` | Get ticks from a timestamp |
| `copy_ticks_range()` | Get ticks by time interval |
| `market_book_add()` | Subscribe to market depth |
| `market_book_get()` | Read market depth |
| `order_check()` | Check funds and request validity |
| `order_send()` | Submit trade request |
| `orders_get()` | Read active orders |
| `positions_get()` | Read open positions |
| `history_orders_get()` | Read historical orders |
| `history_deals_get()` | Read historical deals/fills |

### Key Implementation Notes

- MT5 stores tick and bar times in UTC. Use timezone-aware UTC datetimes.
- `copy_rates_from()` returns bars with columns including `time`, `open`, `high`, `low`, `close`, `tick_volume`, `spread`, and `real_volume`.
- `copy_ticks_from()` returns tick arrays with fields such as `time`, `bid`, `ask`, `last`, `volume`, `time_msc`, `flags`, and `volume_real`.
- `symbol_info()` should be called at startup and cached in `symbol_metadata`.
- Always call `order_check()` before `order_send()`.
- Always check `order_send()` return code and reconcile with `positions_get()` and `history_deals_get()`.
- Use `magic` values to identify bot-owned orders.
- Use `comment` fields to include strategy version and signal ID.
- Never assume volume units are "coins"; MT5 uses lots/contracts as defined by the broker.

## 7. Proposed System Architecture

### High-Level Design

```text
                 +-------------------------+
                 |  Historical Data Files  |
                 +-----------+-------------+
                             |
                             v
+----------------+    +------+-------+      +-------------------+
| MT5 Terminal   |<-->| MT5 Worker   |----->| Local SQLite/DuckDB|
| Windows        |    | Python Bot   |      | + Parquet Archive |
+----------------+    +------+-------+      +---------+---------+
                             |                        |
                             | optional               | optional sync
                             v                        v
                    +--------+---------+      +------------------+
                    | Risk Engine      |      | Northflank       |
                    | Signal Engine    |      | PostgreSQL       |
                    | Execution Engine |      +---------+--------+
                    +--------+---------+                |
                             |                          v
                             |                  +---------------+
                             |                  | Dashboard/API |
                             |                  | Northflank    |
                             |                  +---------------+
                             |
                             v
                    +------------------+
                    | Logs + Traces    |
                    | Pydantic Logfire |
                    +--------+---------+
                             |
                             v
          +------------------+------------------+
          |                                     |
          v                                     v
+------------------+                 +------------------+
| Doubleword       |                 | Anthropic Claude |
| Batch/async      |                 | Coding, review,  |
| log/news evals   |                 | analysis reports |
+------------------+                 +------------------+
```

### Component Responsibilities

| Component | Responsibility |
| --- | --- |
| MT5 terminal | Broker connection, market data, order routing |
| MT5 worker | Main Python process that polls data, computes signals, enforces risk, submits orders |
| Data collector | Stores ticks, bars, spreads, metadata, account snapshots, positions, orders, fills |
| Strategy engine | Computes features and target positions |
| Risk engine | Blocks trades that violate leverage, margin, drawdown, spread, concentration, or fail-safe rules |
| Execution engine | Runs `order_check()`, `order_send()`, reconciliation, retries, and close logic |
| Local store | Fast MVP persistence via SQLite/DuckDB and Parquet |
| Northflank PostgreSQL | More reliable shared database for demo, analytics, and dashboards |
| Northflank jobs | Scheduled analytics, parameter evaluation, dashboard refresh |
| Pydantic/Logfire | Typed schemas, validation, traces, metrics, AI gateway |
| Doubleword | Cheap async/batch inference for log/news analysis and structured reports |
| Anthropic | Coding help, architecture explanations, post-trade review, risk summaries |

### Recommended Python Project Structure

```text
mt5_crypto_bot/
  README.md
  pyproject.toml
  .env.example
  src/
    app.py
    config.py
    mt5_client.py
    symbols.py
    data_collector.py
    features.py
    strategy.py
    risk.py
    execution.py
    storage.py
    analytics.py
    retune.py
    reporting.py
    schemas.py
    logging_setup.py
  scripts/
    verify_mt5_connection.py
    bootstrap_symbols.py
    backtest.py
    run_live.py
    run_reconciliation.py
    run_retune.py
    export_demo_report.py
  tests/
    test_features.py
    test_risk.py
    test_position_sizing.py
    test_order_request.py
  data/
    raw/
    parquet/
    models/
    reports/
```

### Suggested Libraries

| Area | Libraries |
| --- | --- |
| MT5 integration | `MetaTrader5` |
| Data | `pandas`, `numpy`, `pyarrow`, `duckdb` |
| Storage | `sqlite3`, `SQLAlchemy`, `psycopg` |
| Validation | `pydantic` |
| Config | `python-dotenv`, `pydantic-settings` |
| Backtesting | custom vectorized backtester, optionally `backtesting.py` |
| Metrics | `empyrical` or custom metrics |
| ML stretch | `scikit-learn`, `lightgbm` if time permits |
| Observability | `logfire`, `structlog` |
| Dashboard stretch | `FastAPI`, `Streamlit`, or simple static HTML report |
| LLMs | `anthropic`, `openai`, `autobatcher`, `pydantic-ai` |

### Environment Variables

```text
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=
MT5_PATH=
MT5_TIMEOUT_MS=60000

TRADE_MODE=paper_or_live
TARGET_SYMBOLS=BAR/USD,BTC/USD,ETH/USD,SOL/USD,XRP/USD
BOT_MAGIC=20260621
STRATEGY_VERSION=momo_v1

DATABASE_URL=sqlite:///data/trading.db
POSTGRES_URI=
PARQUET_DIR=data/parquet

MAX_GROSS_LEVERAGE=8.0
MAX_SYMBOL_LEVERAGE=2.0
MAX_MARGIN_USAGE=0.60
DAILY_DRAWDOWN_STOP=0.06
TOTAL_DRAWDOWN_STOP=0.10

LOGFIRE_TOKEN=
ANTHROPIC_API_KEY=
DOUBLEWORD_API_KEY=
DOUBLEWORD_BASE_URL=https://api.doubleword.ai/v1
PYDANTIC_AI_GATEWAY_URL=
```

## 8. Trading Strategy for the 5 Target Instruments

### Recommended Hackathon Strategy

Use a **volatility-scaled cross-asset momentum strategy with regime and spread filters**.

Why this is best for the hackathon:

- Fast to implement.
- Works with only OHLCV, spread, and optional order-book data.
- Does not require a fragile deep learning pipeline.
- Generates enough trades for analysis and demo.
- Can be made aggressive when signals align, without running near disqualification thresholds.
- Easy to explain to judges.

### Instrument Roles

| Instrument | Role |
| --- | --- |
| `BTC/USD` | Core market regime and liquidity anchor |
| `ETH/USD` | Core momentum instrument, often high beta to BTC |
| `SOL/USD` | Higher beta momentum instrument |
| `XRP/USD` | Event-sensitive, useful but risk-capped |
| `BAR/USD` | HBAR/Hedera per `information.md`; likely higher idiosyncratic risk, smallest allocation cap |

### Timeframes

| Timeframe | Use |
| --- | --- |
| M1 | Data collection and execution timing |
| M5 | Primary signal generation |
| M15 | Ranking metric alignment, Sharpe tracking, smoother trend confirmation |
| H1 | Regime filter and volatility calibration |

Recommended MVP signal loop: every completed M5 bar.

### Features

For each symbol:

- 5-minute return
- 15-minute return
- 1-hour return
- 4-hour return
- EMA 20 and EMA 80 on M5 closes
- EMA slope
- Donchian breakout over last 20 M5 bars
- ATR 14 on M5 bars
- Realized volatility over 12, 24, and 48 M5 bars
- Spread in basis points
- Tick volume z-score
- Distance from recent high/low
- Optional order-book imbalance if `market_book_get()` works
- BTC regime feature for all altcoins:
  - BTC 1-hour return
  - BTC EMA slope
  - altcoin return minus beta-adjusted BTC return

### Signal Score

Compute a directional score per symbol every M5 close:

```text
trend_score =
    0.30 * zscore(return_1h)
  + 0.25 * zscore(ema20_minus_ema80)
  + 0.20 * breakout_score
  + 0.15 * zscore(return_15m)
  + 0.10 * volume_confirmation
```

For altcoins, add relative strength:

```text
relative_score = zscore(asset_return_1h - beta_to_btc * btc_return_1h)
final_score = 0.75 * trend_score + 0.25 * relative_score
```

Optional order-book adjustment:

```text
if book_imbalance confirms direction:
    final_score += 0.10 * sign(final_score)
if book_imbalance contradicts direction:
    final_score -= 0.10 * sign(final_score)
```

### Entry Conditions

Long entry:

- `final_score > entry_threshold`
- M5 close above EMA 80
- Spread below max spread threshold
- ATR not in extreme shock band
- Account risk checks pass
- No existing long position in the symbol, or current target position is larger than current exposure

Short entry:

- `final_score < -entry_threshold`
- M5 close below EMA 80
- Spread below max spread threshold
- ATR not in extreme shock band
- Account risk checks pass
- No existing short position in the symbol, or current target position is larger than current exposure

MVP thresholds:

| Parameter | Starting Value |
| --- | --- |
| `entry_threshold` | 1.25 |
| `exit_threshold` | 0.35 |
| `max_spread_bps_btc_eth` | 8 bps |
| `max_spread_bps_sol_xrp_bar` | 15-30 bps, verified live |
| `atr_stop_multiple` | 1.6 |
| `take_profit_multiple` | 2.4 |
| `trailing_stop_multiple` | 1.2 |
| `time_stop_minutes` | 180 |

### Exit Conditions

Exit a position when any condition is true:

- Signal crosses below `exit_threshold` for longs or above `-exit_threshold` for shorts.
- Stop-loss hit.
- Take-profit hit.
- Trailing stop hit.
- Position age exceeds `time_stop_minutes`.
- Spread becomes abnormally wide.
- Account drawdown guard triggers.
- MT5 connection is unstable and reconciliation cannot confirm state.
- Event kill switch is manually enabled.

### Stop-Loss and Take-Profit

Use ATR-based exits:

```text
long_stop = entry_price - atr_stop_multiple * ATR_M5
long_take_profit = entry_price + take_profit_multiple * ATR_M5

short_stop = entry_price + atr_stop_multiple * ATR_M5
short_take_profit = entry_price - take_profit_multiple * ATR_M5
```

Then round prices to the symbol point size from `symbol_info().point`.

### Position Sizing

Use volatility and stop-distance sizing:

```text
risk_dollars = equity * risk_per_trade
units_or_lots = risk_dollars / estimated_loss_per_lot_at_stop
```

Use `order_calc_profit()` or symbol metadata to estimate loss per lot. Then round to `volume_step`, clamp to `volume_min` and `volume_max`, and validate with `order_check()`.

Starting risk profile:

| Mode | Trigger | Risk Per Trade | Gross Leverage Cap |
| --- | --- | --- | --- |
| Defensive | Current drawdown > 5% | 0.10%-0.20% | 2x |
| Normal | Default | 0.25%-0.40% | 5x |
| Aggressive | Drawdown < 3%, broad aligned signals | 0.50%-0.75% | 8x |
| Emergency | Drawdown > 8% or margin issue | No new trades | flatten or reduce |

Do not exceed:

- 8x gross leverage for MVP.
- 10x to 12x only as a conscious finals stretch if live behavior is stable.
- 2.0x symbol leverage for BTC/ETH.
- 1.5x symbol leverage for SOL/XRP.
- 1.0x symbol leverage for BAR.
- 60% margin usage.
- 75% single-instrument share of gross exposure.
- 85% net directional share of gross exposure.

These limits sit far below the rule penalty bands while still allowing meaningful return.

### Strategy Alternatives

| Strategy | Pros | Cons | Recommendation |
| --- | --- | --- | --- |
| Pure momentum | Simple, strong in crypto trends | Choppy losses | Use as core |
| Mean reversion | Can improve Sharpe | Dangerous in crypto breakouts | Use only as exit/avoidance filter |
| News sentiment | Strong demo value | Latency and noise | Stretch, not core execution |
| ML classifier | Good story if validated | Overfit risk and build time | Stretch as challenger |
| Market making | Can harvest spread | Depth, queue, adverse selection risk | Avoid for MVP |
| High-frequency scalping | Potentially high return | API abuse and slippage risk | Avoid |

## 9. Live Data Collection and Storage Plan

### Live Market Data to Collect

| Data | MT5 Source | Frequency | MVP Storage |
| --- | --- | --- | --- |
| M1 bars | `copy_rates_from()` or `copy_rates_range()` | every minute | SQLite/Postgres + Parquet |
| M5/M15 bars | resample M1 or request directly | every 5/15 min | SQLite/Postgres |
| Latest tick | `symbol_info_tick()` | every 1-5 sec | rolling table |
| Tick history | `copy_ticks_from()` | every 15-60 sec if available | Parquet |
| Spread | tick ask - bid, `symbol_info().spread` | every poll | market snapshots |
| Symbol metadata | `symbol_info()` | startup and hourly | `symbol_metadata` |
| Market depth | `market_book_get()` | every 5-30 sec if available | `order_book_snapshots` |

### Trading Data to Collect

| Data | Source | Why |
| --- | --- | --- |
| Signals | Strategy engine | Explain every trade and non-trade |
| Risk decisions | Risk engine | Show rule compliance |
| Orders submitted | Execution engine | Auditability |
| Order check result | `order_check()` | Pre-trade validation |
| Execution result | `order_send()` | Detect rejects/fills |
| Deals/fills | `history_deals_get()` | PnL and slippage |
| Open positions | `positions_get()` | Reconciliation |
| Account snapshots | `account_info()` | Equity, margin, drawdown, Sharpe |
| Errors | `last_error()`, exceptions | Debugging and compliance |
| Parameter version | config/model registry | Reproducibility |

### MVP Database Schema

Use SQLite locally for fastest build, then optionally mirror to PostgreSQL.

```sql
CREATE TABLE symbol_metadata (
  symbol TEXT PRIMARY KEY,
  observed_at_utc TEXT NOT NULL,
  digits INTEGER,
  point REAL,
  trade_tick_size REAL,
  trade_tick_value REAL,
  trade_contract_size REAL,
  volume_min REAL,
  volume_max REAL,
  volume_step REAL,
  spread INTEGER,
  filling_mode INTEGER,
  trade_mode INTEGER,
  raw_json TEXT NOT NULL
);

CREATE TABLE bars (
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  time_utc TEXT NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  tick_volume REAL,
  spread REAL,
  real_volume REAL,
  PRIMARY KEY (symbol, timeframe, time_utc)
);

CREATE TABLE ticks (
  symbol TEXT NOT NULL,
  time_msc INTEGER NOT NULL,
  time_utc TEXT NOT NULL,
  bid REAL,
  ask REAL,
  last REAL,
  volume REAL,
  flags INTEGER,
  volume_real REAL,
  PRIMARY KEY (symbol, time_msc)
);

CREATE TABLE order_book_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  side TEXT NOT NULL,
  level INTEGER NOT NULL,
  price REAL NOT NULL,
  volume REAL,
  volume_dbl REAL
);

CREATE TABLE signals (
  signal_id TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  direction TEXT NOT NULL,
  score REAL NOT NULL,
  target_leverage REAL NOT NULL,
  target_volume REAL,
  features_json TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT
);

CREATE TABLE risk_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT,
  checked_at_utc TEXT NOT NULL,
  passed INTEGER NOT NULL,
  equity REAL,
  balance REAL,
  margin REAL,
  margin_usage REAL,
  gross_leverage REAL,
  net_directional_exposure REAL,
  max_drawdown REAL,
  reason TEXT,
  raw_json TEXT
);

CREATE TABLE orders (
  client_order_id TEXT PRIMARY KEY,
  signal_id TEXT,
  submitted_at_utc TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  requested_volume REAL NOT NULL,
  requested_price REAL,
  sl REAL,
  tp REAL,
  mt5_order_ticket INTEGER,
  retcode INTEGER,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT
);

CREATE TABLE fills (
  deal_ticket INTEGER PRIMARY KEY,
  order_ticket INTEGER,
  position_id INTEGER,
  symbol TEXT NOT NULL,
  time_utc TEXT NOT NULL,
  side TEXT,
  volume REAL,
  price REAL,
  profit REAL,
  commission REAL,
  swap REAL,
  slippage_bps REAL,
  raw_json TEXT
);

CREATE TABLE positions_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at_utc TEXT NOT NULL,
  symbol TEXT NOT NULL,
  ticket INTEGER,
  side TEXT,
  volume REAL,
  price_open REAL,
  price_current REAL,
  sl REAL,
  tp REAL,
  profit REAL,
  raw_json TEXT
);

CREATE TABLE account_snapshots (
  observed_at_utc TEXT PRIMARY KEY,
  balance REAL,
  equity REAL,
  profit REAL,
  margin REAL,
  margin_free REAL,
  margin_level REAL,
  gross_leverage REAL,
  max_drawdown REAL,
  sharpe_15m REAL,
  raw_json TEXT
);

CREATE TABLE strategy_versions (
  strategy_version TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  params_json TEXT NOT NULL,
  approved_by TEXT,
  approved_at_utc TEXT,
  active INTEGER NOT NULL
);
```

### Storage Options

| Option | Use Case | Pros | Cons |
| --- | --- | --- | --- |
| SQLite + Parquet | MVP, fastest local build | Simple, reliable, demoable | Single-writer limits |
| DuckDB + Parquet | Backtest and analytics | Fast local analytics | Not ideal as live app DB |
| Northflank PostgreSQL | Demo and team-grade storage | Central, durable, dashboard-ready | Needs setup and secure access |
| Object storage | Stretch archive | Good for ticks and artifacts | More moving parts |

### Retention

MVP retention:

- Keep all signals, orders, fills, account snapshots for entire competition.
- Keep M1/M5 bars for all target symbols for entire competition.
- Keep ticks in Parquet partitioned by `symbol/date/hour`.
- Keep market depth snapshots sampled, not every tick, unless storage budget allows.
- Export daily CSV/Parquet bundles for judging and audit.

## 10. Model Improvement / Continuous Learning Plan

### Recommended Data Flywheel

```text
Collect live data
      |
      v
Store bars, ticks, spreads, signals, orders, fills, positions, account snapshots
      |
      v
Run scheduled analytics and attribution
      |
      v
Propose parameter updates offline
      |
      v
Backtest and forward-validate on held-out windows
      |
      v
Human approves or rejects
      |
      v
Promote new strategy_version
      |
      v
Monitor champion vs challenger live
```

### MVP Improvement Loop

Every 30-60 minutes:

1. Pull latest account, fill, signal, and bar data.
2. Recompute realized PnL by symbol and strategy version.
3. Measure win rate, average win/loss, slippage, spread cost, drawdown, and Sharpe.
4. Compare signal buckets:
   - Strong positive score
   - Weak positive score
   - Neutral
   - Weak negative score
   - Strong negative score
5. Check whether losing trades are caused by:
   - Bad direction
   - Excess spread
   - Too-wide stops
   - Too-tight stops
   - Bad time of day
   - Overexposure to BTC beta
6. Propose parameter changes, but do not auto-enable them.
7. Run a quick replay/backtest over recent and historical windows.
8. Promote only if the new configuration improves return/drawdown tradeoff and does not reduce risk discipline.

### Metrics for Strategy Review

| Metric | Why It Matters |
| --- | --- |
| Total return | Main score driver |
| Max drawdown | 15% score and survival |
| 15-minute Sharpe | Competition metric |
| Risk discipline score estimate | Avoid preventable penalties |
| Trades count | Helps Sharpe award eligibility if finalist |
| PnL by symbol | Detect bad instruments |
| PnL by side | Detect long/short bias |
| PnL by score bucket | Validate signal monotonicity |
| PnL by volatility regime | Tune risk scaling |
| Slippage bps | Execution quality |
| Reject rate | Broker/API reliability |
| Average spread paid | Avoid overtrading wide markets |

### Rule-Based Tuning

Best for MVP.

Parameters to tune:

- `entry_threshold`
- `exit_threshold`
- ATR stop multiple
- take-profit multiple
- trailing-stop multiple
- risk per trade
- symbol leverage caps
- max spread bps
- time stop

Use coarse grids, not fine optimization. Example:

```text
entry_threshold: [1.0, 1.25, 1.5]
atr_stop_multiple: [1.2, 1.6, 2.0]
take_profit_multiple: [1.8, 2.4, 3.0]
risk_per_trade: [0.25%, 0.40%, 0.60%]
```

### Statistical Optimization

Use after MVP is stable.

- Walk-forward validation.
- Penalize drawdown and leverage.
- Require minimum trade count.
- Reject parameter sets that only work on one symbol.
- Select robust plateaus, not single best points.

Example objective:

```text
objective = return
          - 2.0 * max_drawdown
          + 0.25 * sharpe
          - 0.5 * risk_penalty
          - 0.2 * turnover_penalty
```

### Machine Learning Retraining

Stretch only.

A simple ML challenger could predict next 3 to 6 M5 bar direction or risk-adjusted return using:

- Recent returns
- Volatility
- ATR
- EMA distance
- breakout status
- spread
- tick volume
- BTC regime
- order-book imbalance

Recommended model:

- Logistic regression or gradient-boosted trees.
- Output probability of favorable move.
- Use only as a filter or position-size multiplier, not as the sole trading brain.

### Champion/Challenger Approach

| Role | Behavior |
| --- | --- |
| Champion | Current live strategy controls actual orders |
| Challenger | Runs in shadow mode and logs hypothetical signals |
| Promotion | Human-approved after validation |
| Rollback | Immediate switch back to previous `strategy_version` if live drawdown or reject rate worsens |

### Safety Constraints for Learning

The bot must not blindly self-modify in production because:

- Recent crypto behavior may not persist.
- Live fills differ from backtests due to slippage and partial fills.
- Overfitting can increase drawdown exactly when market regime changes.
- Autonomous parameter changes make auditability worse.
- A bad update can trigger leverage, margin, or forced-liquidation risk.

Require human approval for:

- Any change to risk limits.
- Any increase to `risk_per_trade`.
- Any increase to leverage caps.
- Any new symbol activation.
- Any new model version controlling live orders.
- Any change that uses LLM outputs in signal generation.

Validation rules:

- Split data into train, validation, and live-shadow windows.
- Never tune on the same window used for final validation.
- Require performance across at least BTC and ETH, not only BAR/SOL/XRP.
- Reject changes with higher drawdown unless return improvement is substantial.
- Keep previous parameters available for rollback.

## 11. Risk Management Framework

### Risk Objectives

1. Avoid forced liquidation.
2. Avoid rule-based penalties.
3. Preserve enough capital to survive all rounds.
4. Take enough risk to compete on return.
5. Keep a clean audit trail.

### Hard Guardrails

| Guardrail | MVP Limit |
| --- | --- |
| Gross leverage | 8x normal, 10-12x stretch |
| Margin usage | 60% max |
| Single symbol share of gross exposure | 75% max |
| Net directional exposure | 85% max |
| Account drawdown stop | 8%-10% total |
| Daily/round drawdown stop | 5%-6% |
| Max open positions | 5, one per target symbol |
| Max order retries | 2 per order |
| Max order frequency | One signal cycle per 5 minutes per symbol |
| API request rate | Far below 500 requests/sec safe harbor |

### Pre-Trade Risk Check

Before every order:

1. Confirm MT5 connection is alive.
2. Confirm symbol is visible and tradable.
3. Confirm latest tick is fresh.
4. Confirm spread is below threshold.
5. Confirm target symbol is allowed.
6. Confirm position size respects volume step and min/max.
7. Confirm stop-loss and take-profit are valid.
8. Run `order_check()`.
9. Estimate new gross leverage.
10. Estimate new symbol concentration.
11. Estimate margin usage.
12. Block order if any guardrail fails.
13. Log pass/fail reason.

### Fail-Safe Behavior

| Failure | Action |
| --- | --- |
| MT5 disconnect | Stop new orders, attempt reconnect, reconcile positions |
| Data stale | Stop new orders for affected symbol |
| Order rejected | Log retcode, do not retry blindly |
| Partial fill | Reconcile actual position and adjust stops |
| DB unavailable | Continue only if local fallback logging works |
| Equity drawdown breach | Cancel pending orders and reduce/flatten |
| Margin usage breach | Reduce largest risk position first |
| Unknown symbol metadata | Do not trade that symbol |
| Abnormal spread | Hold or exit; no new entry |
| Strategy exception | Stop trading and alert/log |

### Risk Discipline Monitoring

Compute every minute:

```text
margin_usage = used_margin / equity
gross_leverage = gross_notional_exposure / equity
single_instrument_exposure = max_symbol_notional / gross_notional_exposure
net_directional_exposure = abs(net_notional) / gross_notional_exposure
```

Store these in `account_snapshots` and alert if:

- margin usage > 50%
- gross leverage > 8x
- single-instrument concentration > 70%
- net directional exposure > 80%
- drawdown > 5%

## 12. Sponsor Tools: Best Uses and Integration Plan

### Doubleword

Source-specific detail from `doubleword.md`:

- Real-time and async hackathon usage goes through Pydantic AI Gateway / Logfire.
- Batch usage goes directly to Doubleword at `https://api.doubleword.ai/v1`.
- Doubleword is OpenAI-compatible.
- `autobatcher` can use `AsyncOpenAI` or `BatchOpenAI`.
- dottxt model variants support structured generation.
- Batch is cheapest and suited to bulk classification, extraction, embeddings, and evaluations.

Best use in this project:

- Batch summarize trade logs after each round.
- Batch classify news/headlines into structured sentiment signals for demo or shadow mode.
- Batch evaluate strategy reports and produce judge-facing evidence.
- Use embeddings for searchable memory over logs and notes if time permits.
- Do not put Doubleword directly in the live order path for MVP.

### Pydantic / Pydantic AI / Logfire

Best use:

- Define typed schemas for `Signal`, `RiskCheck`, `OrderRequest`, `OrderResult`, `Fill`, and `StrategyParams`.
- Use Pydantic validation to prevent malformed orders.
- Use Logfire to trace strategy cycles, DB writes, risk checks, and LLM calls.
- Use Pydantic AI Gateway for real-time and async Doubleword-backed inference per `doubleword.md`.
- Use Pydantic AI only for helper agents, not autonomous execution.

### Northflank

Best use:

- Host PostgreSQL for shared durable storage.
- Host a lightweight FastAPI or Streamlit dashboard.
- Run scheduled jobs for analytics, retuning proposals, and report generation.
- Store environment variables and secrets through Northflank secret groups.
- Use logs and metrics for demo.

Important limitation:

- Do not assume the MT5 Python execution worker can run directly on Northflank. The provided notes say MT5 Python integration appears Windows-only, while Northflank normally runs containers. Keep live MT5 execution on Windows and use Northflank for supporting services.

### Anthropic

Best use:

- Generate implementation scaffolding quickly.
- Review risk logic and edge cases.
- Summarize logs and explain trades for the demo.
- Produce post-round reports.
- Help write the technical architecture presentation.
- Use tool/function calling for analysis workflows if needed.

Do not use Anthropic to:

- Make unsupervised live trade decisions.
- Modify risk limits without approval.
- Consume credits on repetitive polling or per-tick decisions.
- Send sensitive credentials in prompts.

## 13. Credit Allocation Strategy

### Credit Plan

| Platform | Recommended Use | Expected Value | Avoid Using It For | Credit Efficiency |
| --- | --- | --- | --- | --- |
| Doubleword | Batch log/news classification, structured report generation, embeddings | High demo value at low cost | Per-tick live decisions, urgent execution | Very high |
| Pydantic/Logfire | Observability, typed validation, AI Gateway for real-time/async Doubleword use | High reliability and judging value | Logging every tick verbosely to paid traces | High |
| Northflank | PostgreSQL, dashboard, scheduled analytics jobs | High deployment/demo value | Running oversized services, unused replicas, GPU workloads | High |
| Anthropic | Coding support, architecture/risk review, final reports | High productivity | Bulk classification better suited to Doubleword batch | Medium-high |

### Low-Cost MVP Allocation

| Credit Pool | Spend On |
| --- | --- |
| Doubleword | One or two batch jobs per round: trade-log summary, news/sentiment shadow report |
| Pydantic/Logfire | Trace strategy cycles, risk checks, LLM calls, errors |
| Northflank | Small PostgreSQL instance and one small dashboard/API service |
| Anthropic | Build support, code review, final technical writeup |

### Better-Funded Stretch Allocation

| Credit Pool | Spend On |
| --- | --- |
| Doubleword | Batch headline classification, embeddings, challenger model reports, post-trade labels |
| Pydantic/Logfire | Full agent traces, online eval monitoring, prompt experiments |
| Northflank | Postgres with backups, dashboard, scheduled jobs, preview deployments |
| Anthropic | Automated analyst assistant for judge demo and risk investigation |

### Cost-Saving Tactics

- Use M5 bars for trading, not tick-by-tick LLM analysis.
- Store raw tick data locally/Parquet instead of sending to LLMs.
- Batch LLM jobs after trading sessions.
- Use smaller/cheaper models for classification.
- Use structured schemas to reduce retries.
- Limit Logfire sampling for high-frequency loops.
- Pause Northflank services not needed overnight.
- Use Anthropic for high-leverage reasoning and code review, not bulk data processing.

### Priority Order

1. Build MT5 execution and risk system.
2. Add local data logging.
3. Add Northflank PostgreSQL if time allows.
4. Add Logfire observability.
5. Add Doubleword batch reports.
6. Add Anthropic-assisted final documentation and demo assistant.
7. Add ML challenger only if core bot is stable.

## 14. Step-by-Step Build Plan

### Phase 1: Connectivity and Metadata

1. Install MT5 terminal on Windows.
2. Install Python and `MetaTrader5`.
3. Log in using competition credentials.
4. Verify `account_info()` returns account data.
5. Discover symbols using `symbols_get()`.
6. Map exact broker symbol names for:
   - `BAR/USD`
   - `BTC/USD`
   - `ETH/USD`
   - `SOL/USD`
   - `XRP/USD`
7. Call `symbol_select(symbol, True)` for each.
8. Store `symbol_info()._asdict()` in `symbol_metadata`.

### Phase 2: Data Collector

1. Implement M1/M5 bar polling.
2. Implement latest tick polling.
3. Implement optional tick backfill with `copy_ticks_from()`.
4. Implement optional market depth snapshots.
5. Write to SQLite and Parquet.
6. Add account snapshots every minute.
7. Add position snapshots every minute.

### Phase 3: Strategy Engine

1. Compute features from M5 bars.
2. Compute final directional score.
3. Generate target direction and target leverage.
4. Log every signal, including "no trade" decisions if practical.
5. Add parameter file and `strategy_version`.

### Phase 4: Risk Engine

1. Implement spread checks.
2. Implement leverage and margin checks.
3. Implement drawdown checks.
4. Implement symbol concentration checks.
5. Implement kill switch.
6. Unit test position sizing and guardrails.

### Phase 5: Execution Engine

1. Convert target position into order request.
2. Round volume to `volume_step`.
3. Round prices to `point`.
4. Add SL/TP.
5. Run `order_check()`.
6. Run `order_send()` only if check passes.
7. Store request and result.
8. Reconcile with `positions_get()` and `history_deals_get()`.

### Phase 6: Analytics and Learning Loop

1. Compute round metrics.
2. Compute PnL attribution by symbol, side, and signal bucket.
3. Compute slippage and spread costs.
4. Run parameter grid on recent data.
5. Save proposed parameters as inactive strategy versions.
6. Require manual approval to activate.

### Phase 7: Sponsor Integrations

1. Add Pydantic schemas.
2. Add Logfire tracing.
3. Add Northflank Postgres sync.
4. Add dashboard or report export.
5. Add Doubleword batch summaries.
6. Use Anthropic for code review and final report generation.

## 15. Deployment Plan

### MVP Deployment

| Component | Location |
| --- | --- |
| MT5 terminal | Windows laptop or Windows VPS |
| Live Python worker | Same Windows machine |
| Local DB | SQLite/DuckDB on Windows machine |
| Parquet archive | Local disk with periodic backup |
| Reports | Local generated Markdown/HTML |

### Recommended Hackathon Deployment

| Component | Location |
| --- | --- |
| MT5 terminal | Windows VPS or reliable Windows laptop |
| Live Python worker | Same Windows host |
| PostgreSQL | Northflank |
| Analytics job | Northflank scheduled job |
| Dashboard/API | Northflank service |
| Observability | Pydantic Logfire |
| Batch LLM reports | Doubleword |
| Manual analysis | Anthropic Claude |

### Windows MT5 Worker Checklist

- Disable sleep mode.
- Use stable network.
- Keep MT5 terminal logged in.
- Confirm Algo Trading / relevant permissions as needed.
- Run bot under a process supervisor or scheduled task.
- Log to local disk even if cloud DB fails.
- Set a manual kill switch file, for example `KILL_SWITCH=true`.
- Confirm timezone handling uses UTC internally.
- Confirm positions remain open if terminal disconnects, so bot must reconnect and reconcile.

### Northflank Checklist

- Create project.
- Create PostgreSQL addon.
- Link database secrets to dashboard and analytics jobs.
- Deploy API/dashboard from Git repository.
- Add runtime variables:
  - `POSTGRES_URI`
  - `LOGFIRE_TOKEN`
  - `ANTHROPIC_API_KEY` if needed
  - `DOUBLEWORD_API_KEY` if needed
- Create scheduled job for analytics:
  - every 30 or 60 minutes
  - concurrency policy: `Forbid`
  - retry limit: 1 or 2
  - time limit: 5-10 minutes
- Keep compute plans small unless dashboard load requires more.

## 16. Testing / Validation Plan

### Unit Tests

| Test | Purpose |
| --- | --- |
| Feature calculations | Prevent bad indicators |
| Signal generation | Validate thresholds and direction |
| Position sizing | Prevent oversizing |
| Volume rounding | Respect MT5 volume step |
| Stop/TP rounding | Respect symbol precision |
| Risk checks | Block dangerous trades |
| Drawdown logic | Trigger defensive mode |
| Order request builder | Ensure valid MT5 request structure |

### Integration Tests

1. Connect to MT5.
2. Pull account info.
3. Pull symbol metadata.
4. Pull M1 and M5 bars.
5. Pull latest ticks.
6. Run `order_check()` with minimum volume.
7. In test mode, submit a tiny order if allowed.
8. Confirm position appears in `positions_get()`.
9. Close position.
10. Confirm fill appears in `history_deals_get()`.

### Backtest Validation

Use provided historical data and MT5 bar history:

- Train/tune window: older data.
- Validation window: later historical data.
- Live shadow window: first live hours or test environment.
- Metrics:
  - return
  - max drawdown
  - 15-minute Sharpe
  - trade count
  - average slippage assumption
  - turnover
  - exposure and leverage

### Live Dry Run

Before live trading:

- Run in `DRY_RUN=true` mode.
- Generate signals.
- Run risk checks.
- Build order requests.
- Do not call `order_send()`.
- Compare hypothetical trades with actual next bars.
- Verify logs and dashboard.

### Launch Validation

First 30 minutes live:

- Trade minimum viable size or reduced risk.
- Confirm fills and reconciliation.
- Confirm stops and take-profits are attached.
- Confirm account snapshots update.
- Confirm kill switch works.
- Only then increase to normal mode.

## 17. Demo Plan for Judges

### Demo Narrative

"Here is a safe, reproducible AI-assisted crypto trading system using MT5 for execution, Python for strategy and risk, Northflank for data and dashboards, Pydantic/Logfire for typed observability, Doubleword for batch intelligence, and Anthropic for development and analysis support."

### Demo Flow

1. Show architecture diagram.
2. Show MT5 account connected.
3. Show target symbol metadata table.
4. Show live data collector writing bars, ticks, spreads, and account snapshots.
5. Show latest signals and why each was accepted or rejected.
6. Show risk engine blocking trades when leverage, spread, or drawdown is too high.
7. Show order lifecycle:
   - signal
   - risk check
   - `order_check()`
   - `order_send()`
   - fill
   - position
   - PnL
8. Show data flywheel:
   - live logs
   - post-trade analytics
   - proposed parameter updates
   - human approval gate
   - rollback version
9. Show sponsor usage:
   - Northflank dashboard/Postgres
   - Logfire traces
   - Doubleword batch summary
   - Anthropic-generated risk review or architecture summary
10. Show final metrics against competition formula.

### Demo Artifacts

- GitHub repository.
- Architecture diagram.
- Strategy explanation.
- Risk framework.
- Live dashboard screenshot or URL.
- Post-round report.
- `strategy_versions` table.
- Example trade audit trail.
- Sponsor integration notes.

## 18. Known Risks, Limitations, and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| MT5 Python runtime may be Windows-only | Northflank cannot directly run execution bot | Run execution on Windows; use Northflank for DB/dashboard/jobs |
| Symbol names differ | Bot fails to trade | Discover symbols after login and configure mapping |
| Crypto history incomplete | Weak backtests | Use live shadow data and robust simple strategy |
| Slippage and partial fills | Backtest overstates performance | Log fills, estimate slippage, use conservative sizing |
| Spread widens | False signals and losses | Spread filters and no-trade windows |
| Overfitting | Bad live performance | Walk-forward validation and human approval |
| LLM hallucination | Bad analysis or unsafe suggestions | LLMs never control live orders; use typed schemas |
| API abuse review | Compliance risk | M5 loop, low request rate, no high-frequency spam |
| DB outage | Lost audit data | Local-first logging with later sync |
| MT5 disconnect | Unmanaged positions | Reconnect and reconcile; reduce/flatten on uncertainty |
| Drawdown spiral | Elimination risk | Defensive mode and hard kill switch |
| Single-instrument concentration | Risk penalty | Enforce concentration caps |

## 19. Recommended MVP Scope vs Stretch Goals

### MVP Scope

Build this first:

- MT5 connection and symbol discovery.
- M1/M5 data collector.
- SQLite/DuckDB storage.
- Momentum strategy on five crypto instruments.
- ATR-based stops and take-profits.
- Risk engine with leverage, margin, spread, drawdown, and concentration checks.
- Order execution and reconciliation.
- Account and trade logging.
- Basic analytics report.
- Manual parameter approval.
- README and demo architecture.

### Strong MVP Plus

Add if time allows:

- Northflank PostgreSQL.
- Northflank dashboard.
- Pydantic schemas everywhere.
- Logfire traces for strategy, risk, execution, and DB writes.
- Doubleword batch post-trade reports.
- Anthropic-assisted risk and architecture writeup.

### Stretch Goals

Only after live system is stable:

- News/sentiment shadow model using Doubleword.
- ML challenger model.
- Champion/challenger dashboard.
- Automatic report generation after each round.
- Market depth imbalance feature.
- Parameter optimization job on Northflank.
- Embeddings over trade logs and research notes.
- Slack/Discord-style alerting, if allowed and not noisy.

### Explicit Non-Goals

Do not build for MVP:

- High-frequency market making.
- Fully autonomous online learning.
- LLM-controlled live trading.
- Complex deep learning models.
- Multi-broker infrastructure.
- Kubernetes-heavy production system.
- Overbuilt dashboards before execution works.

## 20. Appendix: Useful Links and References

### Local Source Files

- `rules.md`
- `information.md`
- `doubleword.md`

### MetaTrader 5 and Python Integration

- MT5 Python integration overview: https://www.mql5.com/en/docs/python_metatrader5
- `initialize()`: https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py
- `login()`: https://www.mql5.com/en/docs/python_metatrader5/mt5login_py
- `account_info()`: https://www.mql5.com/en/docs/python_metatrader5/mt5accountinfo_py
- `symbol_info()`: https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py
- `symbol_info_tick()`: https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py
- `copy_rates_from()`: https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py
- `copy_ticks_from()`: https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksfrom_py
- `market_book_get()`: https://www.mql5.com/en/docs/python_metatrader5/mt5marketbookget_py
- `order_check()`: https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py
- `order_send()`: https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py
- `positions_get()`: https://www.mql5.com/en/docs/python_metatrader5/mt5positionsget_py
- `history_deals_get()`: https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py
- MT5 Strategy Tester and optimization: https://www.metatrader5.com/en/terminal/help/algotrading/strategy_optimization

### Doubleword

- Doubleword main site: https://doubleword.ai
- Doubleword docs: https://docs.doubleword.ai
- Intro to Doubleword inference: https://docs.doubleword.ai/inference-api/intro-to-doubleword-inference
- Autobatcher: https://docs.doubleword.ai/inference-api/autobatcher
- Doubleword API base URL: `https://api.doubleword.ai/v1`
- Agent-readable context: https://doubleword.ai/llms.txt
- Hackathon Pydantic gateway setup: https://pydantic.dev/hackathon

### Pydantic / Logfire

- Pydantic AI docs: https://pydantic.dev/docs/ai/overview/
- Pydantic Logfire docs: https://pydantic.dev/docs/logfire/get-started/
- Pydantic AI Gateway / hackathon page: https://pydantic.dev/hackathon

### Northflank

- Northflank docs: https://northflank.com/docs
- Run an image once or on a schedule: https://northflank.com/docs/v1/application/run/run-an-image-once-or-on-a-schedule
- Deploy PostgreSQL on Northflank: https://northflank.com/docs/v1/application/databases-and-persistence/deploy-databases-on-northflank/deploy-postgresql-on-northflank
- Inject runtime variables: https://northflank.com/docs/v1/application/run/inject-runtime-variables
- Inject secrets: https://northflank.com/docs/v1/application/secure/inject-secrets
- Northflank skills repository from local notes: https://github.com/northflank/skills

### Anthropic

- Claude API overview: https://platform.claude.com/docs/en/api/overview
- Messages API: https://platform.claude.com/docs/en/api/messages
- Tool use with Claude: https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
- Console: https://platform.claude.com
- Anthropic usage policies: https://www.anthropic.com/legal/aup
