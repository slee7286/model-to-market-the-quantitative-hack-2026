"""Read-only MT5 market data collection.

The collector deliberately uses market-data and account-inspection APIs only.
It never constructs orders and never calls MT5 order functions. Broker symbols
must come from ``config/symbol_map.json`` so canonical symbols stay constrained
to the allowed crypto set.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS
from mt5_crypto_bot.mt5_client import (
    build_mt5_credentials,
    initialize_mt5,
    load_mt5_module,
    login_mt5,
    read_last_error,
    shutdown_mt5,
)
from mt5_crypto_bot.schemas import normalize_symbol
from mt5_crypto_bot.storage import ParquetArchiveWriter, SQLiteStore
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEFRAMES: tuple[str, ...] = ("M1", "M5")
DEFAULT_BAR_COUNT = 200
DEFAULT_POLL_SECONDS = 5.0
MIN_POLL_SECONDS = 5.0
DEFAULT_FRESHNESS_SECONDS = 120.0
DEFAULT_TICK_BACKFILL_COUNT = 1_000
DEFAULT_MAX_DEPTH_LEVELS = 5

TIMEFRAME_ATTRIBUTE_BY_NAME: dict[str, str] = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
}

# Enforce the broker-reported maximum order volume. The connected account
# rejects larger order sizes, so keep local metadata capped at the broker limit.
VOLUME_MAX_OVERRIDE: float | None = 100.0

METADATA_FIELDS: tuple[str, ...] = (
    "digits",
    "point",
    "trade_tick_size",
    "trade_tick_value",
    "trade_contract_size",
    "volume_min",
    "volume_max",
    "volume_step",
    "spread",
    "filling_mode",
    "trade_mode",
)

MARGIN_FIELDS: tuple[str, ...] = (
    "margin_initial",
    "margin_maintenance",
    "margin_hedged",
    "margin_hedged_use_leg",
    "trade_calc_mode",
    "trade_liquidity_rate",
)


class MarketDataCollectorError(RuntimeError):
    """Raised when safe market-data collection cannot proceed."""


class SymbolMapError(MarketDataCollectorError):
    """Raised when the canonical-to-broker symbol map is missing or unsafe."""


@dataclass(frozen=True)
class CollectorSettings:
    """Bounded read-only collection settings."""

    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    bar_count: int = DEFAULT_BAR_COUNT
    tick_backfill_minutes: float | None = None
    tick_backfill_count: int = DEFAULT_TICK_BACKFILL_COUNT
    include_depth: bool = False
    max_depth_levels: int = DEFAULT_MAX_DEPTH_LEVELS
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS

    def __post_init__(self) -> None:
        normalized_timeframes = tuple(str(item).upper() for item in self.timeframes)
        unsupported = sorted(set(normalized_timeframes) - set(TIMEFRAME_ATTRIBUTE_BY_NAME))
        if unsupported:
            raise MarketDataCollectorError(
                "unsupported collector timeframe(s): " + ", ".join(unsupported)
            )
        if self.bar_count <= 0:
            raise MarketDataCollectorError("bar_count must be positive")
        if self.tick_backfill_minutes is not None and self.tick_backfill_minutes <= 0:
            raise MarketDataCollectorError("tick_backfill_minutes must be positive when set")
        if self.tick_backfill_count <= 0:
            raise MarketDataCollectorError("tick_backfill_count must be positive")
        if self.max_depth_levels <= 0:
            raise MarketDataCollectorError("max_depth_levels must be positive")
        if self.freshness_seconds <= 0:
            raise MarketDataCollectorError("freshness_seconds must be positive")
        object.__setattr__(self, "timeframes", normalized_timeframes)


@dataclass(frozen=True)
class CollectionCycleResult:
    """Summary of one collector cycle."""

    started_at_utc: datetime
    finished_at_utc: datetime
    symbols: tuple[str, ...]
    bars_written: int = 0
    ticks_written: int = 0
    metadata_written: int = 0
    order_book_rows_written: int = 0
    request_count: int = 0
    stale_symbols: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)
    bars_archive_path: str | None = None
    ticks_archive_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe cycle summary for CLI output and logs."""
        return {
            "started_at_utc": self.started_at_utc.isoformat(),
            "finished_at_utc": self.finished_at_utc.isoformat(),
            "symbols": list(self.symbols),
            "bars_written": self.bars_written,
            "ticks_written": self.ticks_written,
            "metadata_written": self.metadata_written,
            "order_book_rows_written": self.order_book_rows_written,
            "request_count": self.request_count,
            "stale_symbols": list(self.stale_symbols),
            "errors": self.errors,
            "bars_archive_path": self.bars_archive_path,
            "ticks_archive_path": self.ticks_archive_path,
        }


def load_confirmed_symbol_map(
    path: str | Path = DEFAULT_SYMBOL_MAP_PATH,
    *,
    target_symbols: Iterable[str] = ALLOWED_SYMBOLS,
) -> dict[str, str]:
    """Load confirmed canonical-to-broker mappings for allowed target symbols.

    ``scripts/bootstrap_symbols.py`` writes a rich schema with per-symbol status.
    This loader accepts only mapped target symbols and, when status information is
    present, requires ``status=confirmed``. It never returns symbols outside the
    canonical allow-list.
    """
    map_path = Path(path)
    if not map_path.exists():
        raise SymbolMapError(
            f"symbol map not found at {map_path}. Run scripts/bootstrap_symbols.py "
            "after MT5 credentials are configured, then rerun the collector."
        )

    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SymbolMapError(f"could not read valid JSON symbol map at {map_path}") from exc

    canonical_to_broker = payload.get("canonical_to_broker")
    if not isinstance(canonical_to_broker, Mapping):
        raise SymbolMapError("symbol map is missing a canonical_to_broker object")

    symbols_section = payload.get("symbols", {})
    if symbols_section is not None and not isinstance(symbols_section, Mapping):
        raise SymbolMapError("symbol map symbols section must be an object when present")

    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    for raw_symbol in target_symbols:
        canonical = normalize_symbol(raw_symbol)
        broker_symbol = canonical_to_broker.get(canonical)
        status: Any = None
        if isinstance(symbols_section, Mapping):
            entry = symbols_section.get(canonical)
            if isinstance(entry, Mapping):
                status = entry.get("status")

        if not isinstance(broker_symbol, str) or not broker_symbol.strip():
            unresolved.append(f"{canonical} (missing broker symbol)")
            continue
        if status is not None and status != "confirmed":
            unresolved.append(f"{canonical} (status={status!r})")
            continue
        mapping[canonical] = broker_symbol.strip()

    if unresolved:
        raise SymbolMapError(
            "symbol map has unresolved target mappings: "
            + ", ".join(unresolved)
            + ". Run scripts/bootstrap_symbols.py and manually resolve ambiguous entries."
        )
    if not mapping:
        raise SymbolMapError("symbol map did not yield any confirmed allowed crypto mappings")
    return mapping


class MarketDataCollector:
    """Collect M1/M5 bars, ticks, metadata, and optional depth into storage."""

    def __init__(
        self,
        mt5_module: Any,
        store: SQLiteStore,
        symbol_map: Mapping[str, str],
        *,
        settings: CollectorSettings | None = None,
        parquet_writer: ParquetArchiveWriter | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.mt5 = mt5_module
        self.store = store
        self.symbol_map = {
            normalize_symbol(canonical): str(broker_symbol)
            for canonical, broker_symbol in symbol_map.items()
        }
        self.settings = settings or CollectorSettings()
        self.parquet_writer = parquet_writer
        self.logger = logger or LOGGER
        # Broker server-time -> UTC offset for tick timestamps, detected per cycle.
        self._tick_utc_offset: timedelta | None = None

    def collect_once(self) -> CollectionCycleResult:
        """Run one read-only collection cycle and persist rows."""
        started_at = datetime.now(timezone.utc)
        # Re-detect the broker tick clock offset against UTC on every cycle.
        self._tick_utc_offset = None
        bars: list[dict[str, Any]] = []
        ticks: list[dict[str, Any]] = []
        order_book_rows: list[dict[str, Any]] = []
        stale_symbols: list[str] = []
        errors: dict[str, str] = {}
        request_count = 0
        metadata_written = 0

        for canonical, broker_symbol in self.symbol_map.items():
            observed_at = datetime.now(timezone.utc)
            try:
                metadata, requests = self._collect_metadata(canonical, broker_symbol, observed_at)
                request_count += requests
                if metadata is not None:
                    self.store.upsert_symbol_metadata(metadata)
                    metadata_written += 1
            except Exception as exc:  # pragma: no cover - covered through per-symbol tests
                errors[f"{canonical}:metadata"] = _exception_summary(exc)
                self.logger.warning("metadata collection failed for %s: %s", canonical, exc)

            for timeframe in self.settings.timeframes:
                try:
                    timeframe_bars, requests = self._collect_bars(
                        canonical,
                        broker_symbol,
                        timeframe,
                    )
                    request_count += requests
                    bars.extend(timeframe_bars)
                except Exception as exc:
                    errors[f"{canonical}:bars:{timeframe}"] = _exception_summary(exc)
                    self.logger.warning(
                        "%s %s bar collection failed: %s",
                        canonical,
                        timeframe,
                        exc,
                    )

            try:
                latest_tick, is_stale, requests = self._collect_latest_tick(
                    canonical,
                    broker_symbol,
                    observed_at,
                )
                request_count += requests
                if latest_tick is not None:
                    ticks.append(latest_tick)
                if is_stale:
                    stale_symbols.append(canonical)
            except Exception as exc:
                errors[f"{canonical}:tick"] = _exception_summary(exc)
                self.logger.warning("latest tick collection failed for %s: %s", canonical, exc)

            if self.settings.tick_backfill_minutes is not None:
                try:
                    backfill_ticks, requests = self._collect_tick_backfill(
                        canonical,
                        broker_symbol,
                    )
                    request_count += requests
                    ticks.extend(backfill_ticks)
                except Exception as exc:
                    errors[f"{canonical}:tick_backfill"] = _exception_summary(exc)
                    self.logger.warning("tick backfill failed for %s: %s", canonical, exc)

            if self.settings.include_depth:
                try:
                    depth_rows, requests = self._collect_market_depth(
                        canonical,
                        broker_symbol,
                    )
                    request_count += requests
                    order_book_rows.extend(depth_rows)
                except Exception as exc:
                    errors[f"{canonical}:depth"] = _exception_summary(exc)
                    self.logger.info("market depth unavailable for %s: %s", canonical, exc)

        bars_written = self.store.upsert_bars(bars)
        ticks_written = self.store.upsert_ticks(ticks)
        order_book_rows_written = self.store.insert_order_book_snapshots(order_book_rows)

        bars_archive_path: Path | None = None
        ticks_archive_path: Path | None = None
        if self.parquet_writer is not None:
            bars_archive_path = self.parquet_writer.write_bars(bars)
            ticks_archive_path = self.parquet_writer.write_ticks(ticks)

        finished_at = datetime.now(timezone.utc)
        result = CollectionCycleResult(
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            symbols=tuple(self.symbol_map),
            bars_written=bars_written,
            ticks_written=ticks_written,
            metadata_written=metadata_written,
            order_book_rows_written=order_book_rows_written,
            request_count=request_count,
            stale_symbols=tuple(sorted(set(stale_symbols))),
            errors=errors,
            bars_archive_path=str(bars_archive_path) if bars_archive_path else None,
            ticks_archive_path=str(ticks_archive_path) if ticks_archive_path else None,
        )
        self.logger.info(
            "collector cycle complete symbols=%s bars=%s ticks=%s depth_rows=%s "
            "metadata=%s requests=%s stale=%s errors=%s",
            len(result.symbols),
            result.bars_written,
            result.ticks_written,
            result.order_book_rows_written,
            result.metadata_written,
            result.request_count,
            len(result.stale_symbols),
            len(result.errors),
        )
        return result

    def _collect_metadata(
        self,
        canonical: str,
        broker_symbol: str,
        observed_at: datetime,
    ) -> tuple[dict[str, Any] | None, int]:
        info = self.mt5.symbol_info(broker_symbol)
        if info is None:
            return None, 1
        raw = _object_to_mapping(info)
        metadata: dict[str, Any] = {
            "symbol": canonical,
            "broker_symbol": broker_symbol,
            "observed_at_utc": observed_at,
            "raw": raw,
            "margin": {
                field: _json_safe(raw[field])
                for field in MARGIN_FIELDS
                if field in raw and raw[field] is not None
            },
        }
        for field_name in METADATA_FIELDS:
            metadata[field_name] = _json_safe(raw.get(field_name))
        if VOLUME_MAX_OVERRIDE is not None:
            metadata["volume_max"] = VOLUME_MAX_OVERRIDE
        return metadata, 1

    def _collect_bars(
        self,
        canonical: str,
        broker_symbol: str,
        timeframe: str,
    ) -> tuple[list[dict[str, Any]], int]:
        mt5_timeframe = _mt5_timeframe(self.mt5, timeframe)
        raw_rates = self.mt5.copy_rates_from(
            broker_symbol,
            mt5_timeframe,
            datetime.now(timezone.utc),
            self.settings.bar_count,
        )
        bars: list[dict[str, Any]] = []
        for record in _iter_mt5_records(raw_rates):
            bars.append(
                {
                    "symbol": canonical,
                    "broker_symbol": broker_symbol,
                    "timeframe": timeframe,
                    "time_utc": _datetime_from_any(record.get("time")),
                    "open": record["open"],
                    "high": record["high"],
                    "low": record["low"],
                    "close": record["close"],
                    "tick_volume": record.get("tick_volume"),
                    "spread": record.get("spread"),
                    "real_volume": record.get("real_volume"),
                    "source": "mt5.copy_rates_from",
                    "raw": record,
                }
            )
        return bars, 1

    def _collect_latest_tick(
        self,
        canonical: str,
        broker_symbol: str,
        observed_at: datetime,
    ) -> tuple[dict[str, Any] | None, bool, int]:
        raw_tick = self.mt5.symbol_info_tick(broker_symbol)
        if raw_tick is None:
            return None, False, 1
        tick = _tick_row(canonical, broker_symbol, _object_to_mapping(raw_tick), "mt5.symbol_info_tick")
        self._detect_tick_offset(tick, observed_at)
        self._apply_tick_offset(tick)
        tick_dt = _datetime_from_any(tick["time_utc"])
        age_seconds = max(0.0, (observed_at - tick_dt).total_seconds())
        tick["observed_at_utc"] = observed_at
        tick["tick_age_seconds"] = age_seconds
        return tick, age_seconds > self.settings.freshness_seconds, 1

    def _detect_tick_offset(self, tick: Mapping[str, Any], observed_at: datetime) -> None:
        """Detect the broker tick clock's whole-hour offset from UTC, once per cycle.

        MT5 reports tick times in broker server time, which differs from UTC by a
        whole number of hours. A tick we just read cannot post-date the moment we
        read it, so any positive skew beyond a small tolerance reveals the server
        offset. Snap it to whole hours so genuine sub-minute staleness is kept.
        """
        if self._tick_utc_offset is not None:
            return
        server_dt = _datetime_from_any(tick["time_utc"])
        skew_seconds = (server_dt - observed_at).total_seconds()
        if skew_seconds > 60.0:
            self._tick_utc_offset = timedelta(hours=round(skew_seconds / 3600.0))
        else:
            self._tick_utc_offset = timedelta(0)

    def _apply_tick_offset(self, tick: dict[str, Any]) -> None:
        """Shift a tick's timestamps from broker server time to UTC, if offset set."""
        offset = self._tick_utc_offset
        if not offset:
            return
        aligned = _datetime_from_any(tick["time_utc"]) - offset
        tick["time_utc"] = aligned
        tick["time_msc"] = int(aligned.timestamp() * 1000)

    def _collect_tick_backfill(
        self,
        canonical: str,
        broker_symbol: str,
    ) -> tuple[list[dict[str, Any]], int]:
        date_from = datetime.now(timezone.utc) - timedelta(
            minutes=float(self.settings.tick_backfill_minutes or 0)
        )
        flags = getattr(self.mt5, "COPY_TICKS_ALL", 0)
        raw_ticks = self.mt5.copy_ticks_from(
            broker_symbol,
            date_from,
            self.settings.tick_backfill_count,
            flags,
        )
        backfill = [
            _tick_row(canonical, broker_symbol, record, "mt5.copy_ticks_from")
            for record in _iter_mt5_records(raw_ticks)
        ]
        for tick in backfill:
            self._apply_tick_offset(tick)
        return backfill, 1

    def _collect_market_depth(
        self,
        canonical: str,
        broker_symbol: str,
    ) -> tuple[list[dict[str, Any]], int]:
        for function_name in ("market_book_add", "market_book_get", "market_book_release"):
            if not hasattr(self.mt5, function_name):
                raise MarketDataCollectorError(
                    f"MT5 module does not expose optional {function_name}"
                )

        observed_at = datetime.now(timezone.utc)
        request_count = 0
        added = False
        rows: list[dict[str, Any]] = []
        try:
            added = bool(self.mt5.market_book_add(broker_symbol))
            request_count += 1
            if not added:
                last_error = read_last_error(self.mt5)
                raise MarketDataCollectorError(
                    "market_book_add returned false; last_error="
                    + json.dumps(last_error, sort_keys=True)
                )

            raw_book = self.mt5.market_book_get(broker_symbol)
            request_count += 1
            rows = _market_depth_rows(
                canonical,
                broker_symbol,
                observed_at,
                raw_book,
                self.mt5,
                self.settings.max_depth_levels,
            )
        finally:
            if added:
                self.mt5.market_book_release(broker_symbol)
                request_count += 1
        return rows, request_count


def run_bounded_collection(
    config: BotConfig,
    *,
    symbol_map_path: str | Path = DEFAULT_SYMBOL_MAP_PATH,
    database_url: str | Path | None = None,
    settings: CollectorSettings | None = None,
    once: bool = True,
    minutes: float | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    mt5_module: Any | None = None,
    shutdown: bool = True,
    parquet_writer: ParquetArchiveWriter | None = None,
    logger: logging.Logger | None = None,
) -> list[CollectionCycleResult]:
    """Initialize MT5, run bounded read-only collection, and shut down."""
    if poll_seconds < MIN_POLL_SECONDS:
        raise MarketDataCollectorError(
            f"poll_seconds must be >= {MIN_POLL_SECONDS:g} to keep polling conservative"
        )
    if not once and (minutes is None or minutes <= 0):
        raise MarketDataCollectorError("minutes must be positive for bounded loop collection")

    symbol_map = load_confirmed_symbol_map(symbol_map_path, target_symbols=config.target_symbols)
    credentials = build_mt5_credentials(config)
    mt5 = mt5_module or load_mt5_module()
    log = logger or LOGGER
    initialized = False
    results: list[CollectionCycleResult] = []
    store = SQLiteStore(database_url or config.database_url)

    try:
        initialize_mt5(credentials, mt5)
        initialized = True
        login_mt5(credentials, mt5)
        with store:
            collector = MarketDataCollector(
                mt5,
                store,
                symbol_map,
                settings=settings,
                parquet_writer=parquet_writer,
                logger=log,
            )
            if once:
                results.append(collector.collect_once())
                return results

            assert minutes is not None
            stop_at = time.monotonic() + (minutes * 60.0)
            while True:
                results.append(collector.collect_once())
                remaining = stop_at - time.monotonic()
                if remaining <= 0:
                    break
                sleep_seconds = min(poll_seconds, remaining)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
        return results
    finally:
        if shutdown and initialized:
            shutdown_mt5(mt5)


def _mt5_timeframe(mt5_module: Any, timeframe: str) -> Any:
    attribute_name = TIMEFRAME_ATTRIBUTE_BY_NAME[timeframe]
    if not hasattr(mt5_module, attribute_name):
        raise MarketDataCollectorError(f"MT5 module is missing {attribute_name}")
    return getattr(mt5_module, attribute_name)


def _iter_mt5_records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(value)]

    dtype = getattr(value, "dtype", None)
    names = getattr(dtype, "names", None)
    if names:
        return [
            {str(name): _native_scalar(item[name]) for name in names}
            for item in value
        ]

    records: list[dict[str, Any]] = []
    try:
        iterator = iter(value)
    except TypeError:
        return [_object_to_mapping(value)]

    for item in iterator:
        records.append(_object_to_mapping(item))
    return records


def _tick_row(
    canonical: str,
    broker_symbol: str,
    record: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    data = dict(record)
    time_msc = data.get("time_msc")
    if time_msc is None:
        time_value = data.get("time")
        time_msc = int(_datetime_from_any(time_value).timestamp() * 1000)
    bid = _float_or_none(data.get("bid"))
    ask = _float_or_none(data.get("ask"))
    spread = None
    spread_bps = None
    if bid is not None and ask is not None:
        spread = max(0.0, ask - bid)
        mid = (ask + bid) / 2
        if mid > 0:
            spread_bps = (spread / mid) * 10_000
    return {
        "symbol": canonical,
        "broker_symbol": broker_symbol,
        "time_msc": int(time_msc),
        "time_utc": _datetime_from_any(int(time_msc) / 1000),
        "bid": bid,
        "ask": ask,
        "last": _float_or_none(data.get("last")),
        "volume": _float_or_none(data.get("volume")),
        "flags": data.get("flags"),
        "volume_real": _float_or_none(data.get("volume_real")),
        "spread": spread,
        "spread_bps": spread_bps,
        "source": source,
        "raw": data,
    }


def _market_depth_rows(
    canonical: str,
    broker_symbol: str,
    observed_at: datetime,
    raw_book: Any,
    mt5_module: Any,
    max_depth_levels: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    levels_by_side: dict[str, int] = {}
    for entry in _iter_mt5_records(raw_book):
        side = _book_side(entry.get("type"), mt5_module)
        next_level = levels_by_side.get(side, 0) + 1
        levels_by_side[side] = next_level
        if next_level > max_depth_levels:
            continue
        price = entry.get("price")
        if price is None:
            continue
        rows.append(
            {
                "symbol": canonical,
                "broker_symbol": broker_symbol,
                "observed_at_utc": observed_at,
                "side": side,
                "level": next_level,
                "price": price,
                "volume": entry.get("volume"),
                "volume_dbl": entry.get("volume_dbl"),
                "raw": entry,
            }
        )
    return rows


def _book_side(book_type: Any, mt5_module: Any) -> str:
    buy_types = {
        getattr(mt5_module, "BOOK_TYPE_BUY", 2),
        getattr(mt5_module, "BOOK_TYPE_BUY_MARKET", 4),
    }
    sell_types = {
        getattr(mt5_module, "BOOK_TYPE_SELL", 1),
        getattr(mt5_module, "BOOK_TYPE_SELL_MARKET", 3),
    }
    if book_type in buy_types:
        return "bid"
    if book_type in sell_types:
        return "ask"
    return "unknown"


def _object_to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        return dict(asdict())
    try:
        return dict(vars(value))
    except TypeError:
        pass
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if not callable(item):
            result[name] = item
    return result


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
    return _datetime_from_any(_native_scalar(value))


def _native_scalar(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except ValueError:
            return value
    return value


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Iterable):
        return [_json_safe(item) for item in value]
    return _json_safe(_native_scalar(value))


def _exception_summary(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


__all__ = [
    "CollectionCycleResult",
    "CollectorSettings",
    "DEFAULT_BAR_COUNT",
    "DEFAULT_FRESHNESS_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "MIN_POLL_SECONDS",
    "MarketDataCollector",
    "MarketDataCollectorError",
    "SymbolMapError",
    "load_confirmed_symbol_map",
    "run_bounded_collection",
]
