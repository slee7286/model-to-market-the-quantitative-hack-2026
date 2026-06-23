"""SQLite storage and optional Parquet archives for the dry-run bot.

The storage layer is intentionally local-first and dependency-light. SQLite is
the MVP audit database; Parquet is an optional high-volume archive when
``pyarrow`` is installed through the ``data`` extra.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from mt5_crypto_bot.constants import (
    ALLOWED_SYMBOLS,
    DEFAULT_DATABASE_URL,
    DEFAULT_STRATEGY_VERSION,
)
from mt5_crypto_bot.schemas import (
    AccountSnapshot,
    ExecutionResult,
    Fill,
    OrderIntent,
    PositionSnapshot,
    RiskCheck,
    Signal,
    StrategyParams,
    SymbolConfig,
    normalize_symbol,
)


SQLITE_SCHEMA_VERSION = 1
DEFAULT_SQLITE_PATH = Path("data/trading.db")

REQUIRED_TABLES: tuple[str, ...] = (
    "symbol_metadata",
    "bars",
    "ticks",
    "order_book_snapshots",
    "signals",
    "risk_checks",
    "orders",
    "fills",
    "positions_snapshots",
    "account_snapshots",
    "strategy_versions",
)


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


ALLOWED_SYMBOL_SQL = ", ".join(_sql_quote(symbol) for symbol in ALLOWED_SYMBOLS)
SYMBOL_CHECK = f"symbol IN ({ALLOWED_SYMBOL_SQL})"

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at_utc TEXT NOT NULL,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol_metadata (
  symbol TEXT PRIMARY KEY CHECK ({SYMBOL_CHECK}),
  broker_symbol TEXT,
  observed_at_utc TEXT NOT NULL,
  digits INTEGER,
  point REAL,
  trade_tick_size REAL,
  trade_tick_value REAL,
  trade_contract_size REAL,
  volume_min REAL,
  volume_max REAL,
  volume_step REAL,
  spread REAL,
  filling_mode INTEGER,
  trade_mode INTEGER,
  margin_json TEXT,
  raw_json TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL CHECK ({SYMBOL_CHECK}),
  timeframe TEXT NOT NULL,
  time_utc TEXT NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  tick_volume REAL,
  spread REAL,
  real_volume REAL,
  source TEXT,
  raw_json TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (symbol, timeframe, time_utc)
);

CREATE TABLE IF NOT EXISTS ticks (
  symbol TEXT NOT NULL CHECK ({SYMBOL_CHECK}),
  time_msc INTEGER NOT NULL,
  time_utc TEXT NOT NULL,
  bid REAL,
  ask REAL,
  last REAL,
  volume REAL,
  flags INTEGER,
  volume_real REAL,
  source TEXT,
  raw_json TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (symbol, time_msc)
);

CREATE TABLE IF NOT EXISTS order_book_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL CHECK ({SYMBOL_CHECK}),
  observed_at_utc TEXT NOT NULL,
  side TEXT NOT NULL,
  level INTEGER NOT NULL,
  price REAL NOT NULL,
  volume REAL,
  volume_dbl REAL,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  symbol TEXT NOT NULL CHECK ({SYMBOL_CHECK}),
  timeframe TEXT NOT NULL,
  direction TEXT NOT NULL,
  score REAL NOT NULL,
  target_leverage REAL NOT NULL,
  target_volume REAL,
  target_price REAL,
  features_json TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT,
  raw_json TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_id TEXT UNIQUE,
  signal_id TEXT,
  checked_at_utc TEXT NOT NULL,
  passed INTEGER NOT NULL,
  symbol TEXT CHECK (symbol IS NULL OR {SYMBOL_CHECK}),
  equity REAL,
  balance REAL,
  margin REAL,
  margin_usage REAL,
  gross_leverage REAL,
  net_directional_exposure REAL,
  single_instrument_exposure REAL,
  max_drawdown REAL,
  reason TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY,
  signal_id TEXT,
  created_at_utc TEXT,
  submitted_at_utc TEXT NOT NULL,
  symbol TEXT NOT NULL CHECK ({SYMBOL_CHECK}),
  side TEXT NOT NULL,
  order_type TEXT,
  requested_volume REAL NOT NULL,
  requested_price REAL,
  sl REAL,
  tp REAL,
  time_in_force TEXT,
  strategy_version TEXT,
  mt5_order_ticket INTEGER,
  mt5_deal_ticket INTEGER,
  retcode INTEGER,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
  fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_ticket INTEGER UNIQUE,
  order_ticket INTEGER,
  position_id INTEGER,
  symbol TEXT NOT NULL CHECK ({SYMBOL_CHECK}),
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

CREATE TABLE IF NOT EXISTS positions_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at_utc TEXT NOT NULL,
  symbol TEXT NOT NULL CHECK ({SYMBOL_CHECK}),
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

CREATE TABLE IF NOT EXISTS account_snapshots (
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
  currency TEXT,
  raw_json TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
  strategy_version TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  params_json TEXT NOT NULL,
  approved_by TEXT,
  approved_at_utc TEXT,
  active INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_bars_symbol_time
  ON bars(symbol, timeframe, time_utc);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time
  ON ticks(symbol, time_utc);
CREATE INDEX IF NOT EXISTS idx_order_book_symbol_time
  ON order_book_snapshots(symbol, observed_at_utc);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_time
  ON signals(symbol, created_at_utc);
CREATE INDEX IF NOT EXISTS idx_risk_checks_signal_time
  ON risk_checks(signal_id, checked_at_utc);
CREATE INDEX IF NOT EXISTS idx_orders_signal
  ON orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_fills_symbol_time
  ON fills(symbol, time_utc);
CREATE INDEX IF NOT EXISTS idx_positions_symbol_time
  ON positions_snapshots(symbol, observed_at_utc);
"""


class StorageError(RuntimeError):
    """Raised when local storage configuration is invalid."""


class SQLiteStore:
    """Small SQLite repository for market data and audit records."""

    def __init__(
        self,
        database_url: str | Path = DEFAULT_DATABASE_URL,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.database_path = parse_sqlite_path(database_url)
        self.timeout_seconds = timeout_seconds
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> SQLiteStore:
        self.initialize_schema()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the open SQLite connection, creating it when needed."""
        if self._connection is None:
            if self.database_path != ":memory:":
                Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database_path, timeout=self.timeout_seconds)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
            if self.database_path != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            self._connection = connection
        return self._connection

    def close(self) -> None:
        """Close the SQLite connection if it is open."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def initialize_schema(self) -> None:
        """Create all storage tables and indexes idempotently."""
        connection = self.connection
        connection.executescript(SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(version, applied_at_utc, description)
            VALUES (?, ?, ?)
            """,
            (
                SQLITE_SCHEMA_VERSION,
                utc_now_iso(),
                "Initial local SQLite audit schema.",
            ),
        )
        connection.commit()

    def list_tables(self) -> set[str]:
        """Return user table names currently present in the database."""
        rows = self.fetch_all(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        return {str(row["name"]) for row in rows}

    def count_rows(self, table: str) -> int:
        """Count rows in a known table name."""
        allowed_tables = set(REQUIRED_TABLES) | {"schema_migrations"}
        if table not in allowed_tables:
            raise StorageError(f"unknown table for row count: {table}")
        row = self.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")
        return int(row["count"]) if row is not None else 0

    def fetch_one(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> sqlite3.Row | None:
        cursor = self.connection.execute(sql, parameters)
        return cursor.fetchone()

    def fetch_all(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[sqlite3.Row]:
        cursor = self.connection.execute(sql, parameters)
        return list(cursor.fetchall())

    def upsert_symbol_metadata(self, metadata: SymbolConfig | Mapping[str, Any]) -> None:
        """Insert or update broker metadata for one canonical symbol."""
        data = _model_or_mapping(metadata)
        symbol = normalize_symbol(data.get("symbol") or data.get("canonical_symbol"))
        observed_at = _utc_iso(data.get("observed_at_utc"))
        now = utc_now_iso()
        raw = data.get("raw") if data.get("raw") is not None else data

        self.connection.execute(
            """
            INSERT INTO symbol_metadata(
              symbol, broker_symbol, observed_at_utc, digits, point,
              trade_tick_size, trade_tick_value, trade_contract_size,
              volume_min, volume_max, volume_step, spread, filling_mode,
              trade_mode, margin_json, raw_json, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
              broker_symbol = excluded.broker_symbol,
              observed_at_utc = excluded.observed_at_utc,
              digits = excluded.digits,
              point = excluded.point,
              trade_tick_size = excluded.trade_tick_size,
              trade_tick_value = excluded.trade_tick_value,
              trade_contract_size = excluded.trade_contract_size,
              volume_min = excluded.volume_min,
              volume_max = excluded.volume_max,
              volume_step = excluded.volume_step,
              spread = excluded.spread,
              filling_mode = excluded.filling_mode,
              trade_mode = excluded.trade_mode,
              margin_json = excluded.margin_json,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                symbol,
                data.get("broker_symbol"),
                observed_at,
                data.get("digits"),
                data.get("point"),
                data.get("trade_tick_size") or data.get("tick_size"),
                data.get("trade_tick_value") or data.get("tick_value"),
                data.get("trade_contract_size") or data.get("contract_size"),
                data.get("volume_min"),
                data.get("volume_max"),
                data.get("volume_step"),
                data.get("spread"),
                data.get("filling_mode"),
                data.get("trade_mode"),
                _json_dumps(data.get("margin", {})),
                _json_dumps(raw),
                now,
            ),
        )
        self.connection.commit()

    def upsert_bars(self, bars: Iterable[Mapping[str, Any]]) -> int:
        """Insert or update OHLCV bars keyed by symbol, timeframe, and UTC time."""
        rows = [self._coerce_bar_row(row) for row in bars]
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO bars(
              symbol, timeframe, time_utc, open, high, low, close,
              tick_volume, spread, real_volume, source, raw_json,
              created_at_utc, updated_at_utc
            )
            VALUES (
              :symbol, :timeframe, :time_utc, :open, :high, :low, :close,
              :tick_volume, :spread, :real_volume, :source, :raw_json,
              :created_at_utc, :updated_at_utc
            )
            ON CONFLICT(symbol, timeframe, time_utc) DO UPDATE SET
              open = excluded.open,
              high = excluded.high,
              low = excluded.low,
              close = excluded.close,
              tick_volume = excluded.tick_volume,
              spread = excluded.spread,
              real_volume = excluded.real_volume,
              source = excluded.source,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def upsert_ticks(self, ticks: Iterable[Mapping[str, Any]]) -> int:
        """Insert or update ticks keyed by symbol and millisecond timestamp."""
        rows = [self._coerce_tick_row(row) for row in ticks]
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO ticks(
              symbol, time_msc, time_utc, bid, ask, last, volume,
              flags, volume_real, source, raw_json, created_at_utc, updated_at_utc
            )
            VALUES (
              :symbol, :time_msc, :time_utc, :bid, :ask, :last, :volume,
              :flags, :volume_real, :source, :raw_json, :created_at_utc, :updated_at_utc
            )
            ON CONFLICT(symbol, time_msc) DO UPDATE SET
              time_utc = excluded.time_utc,
              bid = excluded.bid,
              ask = excluded.ask,
              last = excluded.last,
              volume = excluded.volume,
              flags = excluded.flags,
              volume_real = excluded.volume_real,
              source = excluded.source,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def insert_order_book_snapshots(self, snapshots: Iterable[Mapping[str, Any]]) -> int:
        """Append level-by-level market-depth snapshots."""
        rows = [self._coerce_order_book_row(row) for row in snapshots]
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO order_book_snapshots(
              symbol, observed_at_utc, side, level, price, volume, volume_dbl, raw_json
            )
            VALUES (
              :symbol, :observed_at_utc, :side, :level, :price, :volume,
              :volume_dbl, :raw_json
            )
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def upsert_signal(self, signal: Signal | Mapping[str, Any]) -> None:
        """Insert or update a strategy signal with full feature audit JSON."""
        data = _model_or_mapping(signal)
        symbol = normalize_symbol(data["symbol"])
        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO signals(
              signal_id, created_at_utc, strategy_version, symbol, timeframe,
              direction, score, target_leverage, target_volume, target_price,
              features_json, decision, reason, raw_json, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_id) DO UPDATE SET
              created_at_utc = excluded.created_at_utc,
              strategy_version = excluded.strategy_version,
              symbol = excluded.symbol,
              timeframe = excluded.timeframe,
              direction = excluded.direction,
              score = excluded.score,
              target_leverage = excluded.target_leverage,
              target_volume = excluded.target_volume,
              target_price = excluded.target_price,
              features_json = excluded.features_json,
              decision = excluded.decision,
              reason = excluded.reason,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                data["signal_id"],
                _utc_iso(data["created_at_utc"]),
                data["strategy_version"],
                symbol,
                data["timeframe"],
                data["direction"],
                data["score"],
                data["target_leverage"],
                data.get("target_volume"),
                data.get("target_price"),
                _json_dumps(data.get("features", {})),
                data["decision"],
                data.get("reason"),
                _json_dumps(data),
                now,
            ),
        )
        self.connection.commit()

    def insert_risk_check(self, risk_check: RiskCheck | Mapping[str, Any]) -> int:
        """Persist a risk decision. ``check_id`` makes repeated writes idempotent."""
        data = _model_or_mapping(risk_check)
        symbol = normalize_symbol(data["symbol"]) if data.get("symbol") else None
        cursor = self.connection.execute(
            """
            INSERT INTO risk_checks(
              check_id, signal_id, checked_at_utc, passed, symbol, equity,
              balance, margin, margin_usage, gross_leverage,
              net_directional_exposure, single_instrument_exposure,
              max_drawdown, reason, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_id) DO UPDATE SET
              signal_id = excluded.signal_id,
              checked_at_utc = excluded.checked_at_utc,
              passed = excluded.passed,
              symbol = excluded.symbol,
              equity = excluded.equity,
              balance = excluded.balance,
              margin = excluded.margin,
              margin_usage = excluded.margin_usage,
              gross_leverage = excluded.gross_leverage,
              net_directional_exposure = excluded.net_directional_exposure,
              single_instrument_exposure = excluded.single_instrument_exposure,
              max_drawdown = excluded.max_drawdown,
              reason = excluded.reason,
              raw_json = excluded.raw_json
            """,
            (
                data.get("check_id"),
                data.get("signal_id"),
                _utc_iso(data["checked_at_utc"]),
                int(bool(data["passed"])),
                symbol,
                data.get("equity"),
                data.get("balance"),
                data.get("margin"),
                data.get("margin_usage"),
                data.get("gross_leverage"),
                data.get("net_directional_exposure"),
                data.get("single_instrument_exposure"),
                data.get("max_drawdown"),
                data.get("reason"),
                _json_dumps(data),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def upsert_order_intent(
        self,
        order_intent: OrderIntent | Mapping[str, Any],
        *,
        status: str = "intent",
    ) -> None:
        """Persist an order intent before dry-run or future guarded execution."""
        data = _model_or_mapping(order_intent)
        now = utc_now_iso()
        symbol = normalize_symbol(data["symbol"])
        self.connection.execute(
            """
            INSERT INTO orders(
              client_order_id, signal_id, created_at_utc, submitted_at_utc,
              symbol, side, order_type, requested_volume, requested_price,
              sl, tp, time_in_force, strategy_version, status,
              request_json, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
              signal_id = excluded.signal_id,
              created_at_utc = excluded.created_at_utc,
              submitted_at_utc = excluded.submitted_at_utc,
              symbol = excluded.symbol,
              side = excluded.side,
              order_type = excluded.order_type,
              requested_volume = excluded.requested_volume,
              requested_price = excluded.requested_price,
              sl = excluded.sl,
              tp = excluded.tp,
              time_in_force = excluded.time_in_force,
              strategy_version = excluded.strategy_version,
              status = excluded.status,
              request_json = excluded.request_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                data["client_order_id"],
                data.get("signal_id"),
                _utc_iso(data["created_at_utc"]),
                _utc_iso(data["created_at_utc"]),
                symbol,
                data["side"],
                data.get("order_type"),
                data["requested_volume"],
                data.get("requested_price"),
                data.get("stop_loss"),
                data.get("take_profit"),
                data.get("time_in_force"),
                data.get("strategy_version", DEFAULT_STRATEGY_VERSION),
                status,
                _json_dumps(data),
                now,
            ),
        )
        self.connection.commit()

    def upsert_execution_result(self, result: ExecutionResult | Mapping[str, Any]) -> None:
        """Attach dry-run or future guarded execution results to an order row."""
        data = _model_or_mapping(result)
        now = utc_now_iso()
        symbol = normalize_symbol(data["symbol"])
        self.connection.execute(
            """
            INSERT INTO orders(
              client_order_id, submitted_at_utc, symbol, side, requested_volume,
              requested_price, mt5_order_ticket, mt5_deal_ticket, retcode,
              status, request_json, result_json, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
              submitted_at_utc = excluded.submitted_at_utc,
              symbol = excluded.symbol,
              requested_volume = excluded.requested_volume,
              requested_price = excluded.requested_price,
              mt5_order_ticket = excluded.mt5_order_ticket,
              mt5_deal_ticket = excluded.mt5_deal_ticket,
              retcode = excluded.retcode,
              status = excluded.status,
              request_json = excluded.request_json,
              result_json = excluded.result_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                data["client_order_id"],
                _utc_iso(data["executed_at_utc"]),
                symbol,
                data.get("side", "unknown"),
                data["requested_volume"],
                data.get("requested_price"),
                data.get("mt5_order_ticket"),
                data.get("mt5_deal_ticket"),
                data.get("retcode"),
                data["status"],
                _json_dumps(data.get("request", {})),
                _json_dumps(data),
                now,
            ),
        )
        self.connection.commit()

    def insert_fill(self, fill: Fill | Mapping[str, Any]) -> int:
        """Insert or update a fill/deal record."""
        data = _model_or_mapping(fill)
        symbol = normalize_symbol(data["symbol"])
        cursor = self.connection.execute(
            """
            INSERT INTO fills(
              deal_ticket, order_ticket, position_id, symbol, time_utc, side,
              volume, price, profit, commission, swap, slippage_bps, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deal_ticket) DO UPDATE SET
              order_ticket = excluded.order_ticket,
              position_id = excluded.position_id,
              symbol = excluded.symbol,
              time_utc = excluded.time_utc,
              side = excluded.side,
              volume = excluded.volume,
              price = excluded.price,
              profit = excluded.profit,
              commission = excluded.commission,
              swap = excluded.swap,
              slippage_bps = excluded.slippage_bps,
              raw_json = excluded.raw_json
            """,
            (
                data.get("deal_ticket"),
                data.get("order_ticket"),
                data.get("position_id"),
                symbol,
                _utc_iso(data.get("filled_at_utc") or data.get("time_utc")),
                data.get("side"),
                data.get("volume"),
                data.get("price"),
                data.get("profit"),
                data.get("commission"),
                data.get("swap"),
                data.get("slippage_bps"),
                _json_dumps(data),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def insert_position_snapshot(self, snapshot: PositionSnapshot | Mapping[str, Any]) -> int:
        """Append a position snapshot."""
        data = _model_or_mapping(snapshot)
        symbol = normalize_symbol(data["symbol"])
        cursor = self.connection.execute(
            """
            INSERT INTO positions_snapshots(
              observed_at_utc, symbol, ticket, side, volume, price_open,
              price_current, sl, tp, profit, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_iso(data["observed_at_utc"]),
                symbol,
                data.get("ticket"),
                data.get("side"),
                data.get("volume"),
                data.get("price_open"),
                data.get("price_current"),
                data.get("stop_loss"),
                data.get("take_profit"),
                data.get("profit"),
                _json_dumps(data),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def upsert_account_snapshot(self, snapshot: AccountSnapshot | Mapping[str, Any]) -> None:
        """Insert or update the account snapshot for a UTC observation time."""
        data = _model_or_mapping(snapshot)
        observed_at = _utc_iso(data["observed_at_utc"])
        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO account_snapshots(
              observed_at_utc, balance, equity, profit, margin, margin_free,
              margin_level, gross_leverage, max_drawdown, sharpe_15m,
              currency, raw_json, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(observed_at_utc) DO UPDATE SET
              balance = excluded.balance,
              equity = excluded.equity,
              profit = excluded.profit,
              margin = excluded.margin,
              margin_free = excluded.margin_free,
              margin_level = excluded.margin_level,
              gross_leverage = excluded.gross_leverage,
              max_drawdown = excluded.max_drawdown,
              sharpe_15m = excluded.sharpe_15m,
              currency = excluded.currency,
              raw_json = excluded.raw_json,
              updated_at_utc = excluded.updated_at_utc
            """,
            (
                observed_at,
                data.get("balance"),
                data.get("equity"),
                data.get("profit"),
                data.get("margin"),
                data.get("margin_free"),
                data.get("margin_level"),
                data.get("gross_leverage"),
                data.get("max_drawdown"),
                data.get("sharpe_15m"),
                data.get("currency", "USD"),
                _json_dumps(data),
                now,
            ),
        )
        self.connection.commit()

    def upsert_strategy_version(
        self,
        strategy: StrategyParams | Mapping[str, Any] | str,
        *,
        active: bool = False,
        approved_by: str | None = None,
        approved_at_utc: datetime | str | None = None,
    ) -> None:
        """Insert or update a strategy version record.

        Strategy rows are inactive unless explicitly passed as active. Later
        retuning prompts can store proposals here without changing behavior.
        """
        if isinstance(strategy, str):
            strategy_version = strategy
            params: dict[str, Any] = {"strategy_version": strategy}
        else:
            params = _model_or_mapping(strategy)
            strategy_version = str(params.get("strategy_version", DEFAULT_STRATEGY_VERSION))
        created_at = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO strategy_versions(
              strategy_version, created_at_utc, params_json, approved_by,
              approved_at_utc, active, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_version) DO UPDATE SET
              params_json = excluded.params_json,
              approved_by = excluded.approved_by,
              approved_at_utc = excluded.approved_at_utc,
              active = excluded.active,
              raw_json = excluded.raw_json
            """,
            (
                strategy_version,
                created_at,
                _json_dumps(params),
                approved_by,
                _utc_iso(approved_at_utc) if approved_at_utc else None,
                int(active),
                _json_dumps(params),
            ),
        )
        self.connection.commit()

    def _coerce_bar_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        now = utc_now_iso()
        symbol = normalize_symbol(data["symbol"])
        return {
            "symbol": symbol,
            "timeframe": str(data["timeframe"]).upper(),
            "time_utc": _utc_iso(data.get("time_utc") or data.get("time")),
            "open": float(data["open"]),
            "high": float(data["high"]),
            "low": float(data["low"]),
            "close": float(data["close"]),
            "tick_volume": data.get("tick_volume"),
            "spread": data.get("spread"),
            "real_volume": data.get("real_volume"),
            "source": data.get("source"),
            "raw_json": _json_dumps(data),
            "created_at_utc": now,
            "updated_at_utc": now,
        }

    def _coerce_tick_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        now = utc_now_iso()
        symbol = normalize_symbol(data["symbol"])
        time_msc = data.get("time_msc")
        if time_msc is None:
            raw_time = data.get("time_utc") or data.get("time")
            time_msc = int(_datetime_from_any(raw_time).timestamp() * 1000)
        time_utc = data.get("time_utc")
        if time_utc is None:
            time_utc = int(time_msc) / 1000
        return {
            "symbol": symbol,
            "time_msc": int(time_msc),
            "time_utc": _utc_iso(time_utc),
            "bid": data.get("bid"),
            "ask": data.get("ask"),
            "last": data.get("last"),
            "volume": data.get("volume"),
            "flags": data.get("flags"),
            "volume_real": data.get("volume_real"),
            "source": data.get("source"),
            "raw_json": _json_dumps(data),
            "created_at_utc": now,
            "updated_at_utc": now,
        }

    def _coerce_order_book_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        return {
            "symbol": normalize_symbol(data["symbol"]),
            "observed_at_utc": _utc_iso(data.get("observed_at_utc")),
            "side": str(data["side"]).lower(),
            "level": int(data["level"]),
            "price": float(data["price"]),
            "volume": data.get("volume"),
            "volume_dbl": data.get("volume_dbl"),
            "raw_json": _json_dumps(data),
        }


class ParquetArchiveWriter:
    """Optional Parquet writer for high-volume bar and tick archives."""

    def __init__(self, root_dir: str | Path = "data/parquet") -> None:
        self.root_dir = Path(root_dir)

    @property
    def available(self) -> bool:
        """Return true when pyarrow is importable."""
        return _load_pyarrow() is not None

    def write_bars(self, bars: Iterable[Mapping[str, Any]]) -> Path | None:
        """Write bar rows to a partitioned Parquet file if pyarrow is installed."""
        return self.write_rows("bars", bars)

    def write_ticks(self, ticks: Iterable[Mapping[str, Any]]) -> Path | None:
        """Write tick rows to a partitioned Parquet file if pyarrow is installed."""
        return self.write_rows("ticks", ticks)

    def write_rows(self, dataset: str, rows: Iterable[Mapping[str, Any]]) -> Path | None:
        """Write rows into ``root/dataset/date=YYYY-MM-DD/*.parquet``.

        Returns ``None`` when there are no rows or when the optional pyarrow
        dependency is unavailable.
        """
        materialized = [_parquet_safe_row(row) for row in rows]
        if not materialized:
            return None
        pyarrow_modules = _load_pyarrow()
        if pyarrow_modules is None:
            return None

        pa, pq = pyarrow_modules
        date = _partition_date(materialized[0])
        output_dir = self.root_dir / dataset / f"date={date}"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{dataset}-{int(time.time() * 1000)}-{uuid4().hex[:8]}.parquet"
        table = pa.Table.from_pylist(materialized)
        pq.write_table(table, output_path)
        return output_path


def parse_sqlite_path(database_url: str | Path) -> str:
    """Parse local SQLite path strings and sqlite URLs."""
    if isinstance(database_url, Path):
        return str(database_url)
    value = str(database_url)
    if value in {":memory:", "sqlite:///:memory:"}:
        return ":memory:"
    if value.startswith("sqlite:///"):
        path = value.removeprefix("sqlite:///")
        return path or str(DEFAULT_SQLITE_PATH)
    if value.startswith("sqlite://"):
        raise StorageError(
            "only local sqlite:/// URLs are supported by the MVP storage layer"
        )
    return value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_iso(value: Any) -> str:
    return _datetime_from_any(value).isoformat()


def _datetime_from_any(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, int | float):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise StorageError(f"cannot convert value to UTC datetime: {value!r}")


def _model_or_mapping(value: Any) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return dict(value)
    raise StorageError(f"expected a pydantic model or mapping, got {type(value).__name__}")


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Iterable):
        return [_json_safe(item) for item in value]
    return str(value)


def _load_pyarrow() -> tuple[Any, Any] | None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return None
    return pa, pq


def _parquet_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe(value) for key, value in row.items()}


def _partition_date(row: Mapping[str, Any]) -> str:
    for key in ("time_utc", "observed_at_utc", "created_at_utc", "time"):
        if key in row and row[key] is not None:
            return _datetime_from_any(row[key]).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


__all__ = [
    "DEFAULT_SQLITE_PATH",
    "ParquetArchiveWriter",
    "REQUIRED_TABLES",
    "SQLITE_SCHEMA_VERSION",
    "SQLiteStore",
    "StorageError",
    "parse_sqlite_path",
]
