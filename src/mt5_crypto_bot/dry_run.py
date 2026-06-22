"""End-to-end dry-run orchestration.

This module wires the read-only collector, feature engineering, strategy,
pre-trade risk checks, and dry-run execution recording. It never calls MT5
``order_check`` or ``order_send``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mt5_crypto_bot.backtest import make_synthetic_fixture_market_data
from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_DATABASE_URL
from mt5_crypto_bot.data_collector import (
    CollectionCycleResult,
    CollectorSettings,
    MarketDataCollector,
    MarketDataCollectorError,
    SymbolMapError,
    load_confirmed_symbol_map,
)
from mt5_crypto_bot.execution import ExecutionBatchResult, ExecutionEngine, read_positions
from mt5_crypto_bot.features import (
    FeatureConfig,
    FeatureEngineeringError,
    compute_feature_snapshots_from_store,
)
from mt5_crypto_bot.mt5_client import (
    MT5ConfigurationError,
    MT5ConnectionError,
    MT5DependencyError,
    MT5Error,
    build_mt5_credentials,
    initialize_mt5,
    load_mt5_module,
    login_mt5,
    read_account_info,
    shutdown_mt5,
)
from mt5_crypto_bot.risk import (
    RiskBatchResult,
    RiskEngine,
    RiskLimits,
    load_risk_context_from_store,
)
from mt5_crypto_bot.schemas import AccountSnapshot, PositionSide, PositionSnapshot, SymbolConfig
from mt5_crypto_bot.schemas import normalize_symbols
from mt5_crypto_bot.storage import SQLiteStore
from mt5_crypto_bot.strategy import (
    DryRunStrategyEngine,
    StrategyCycleResult,
    StrategyEngineError,
    load_strategy_context_from_store,
)
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


DEFAULT_DRY_RUN_POLL_SECONDS = 15.0
MIN_DRY_RUN_POLL_SECONDS = 5.0
DEFAULT_FIXTURE_BAR_COUNT = 130
INITIAL_EQUITY = 1_000_000.0

SAFE_COLLECTION_ERRORS = (
    SymbolMapError,
    MarketDataCollectorError,
    MT5DependencyError,
    MT5ConfigurationError,
    MT5ConnectionError,
    MT5Error,
    OSError,
)


class DryRunOrchestrationError(RuntimeError):
    """Raised when the end-to-end dry-run cycle cannot proceed safely."""


@dataclass(frozen=True)
class DryRunCycleResult:
    """Summary of one end-to-end dry-run cycle."""

    started_at_utc: datetime
    finished_at_utc: datetime
    data_mode: str
    target_symbols: tuple[str, ...]
    collection_result: CollectionCycleResult | None
    strategy_result: StrategyCycleResult
    risk_result: RiskBatchResult
    execution_result: ExecutionBatchResult
    table_counts: Mapping[str, int]
    fallback_reason: str | None = None

    def summary(self) -> dict[str, Any]:
        """Return a JSON-safe cycle summary for CLI output."""
        return {
            "started_at_utc": self.started_at_utc.isoformat(),
            "finished_at_utc": self.finished_at_utc.isoformat(),
            "data_mode": self.data_mode,
            "fallback_reason": self.fallback_reason,
            "target_symbols": list(self.target_symbols),
            "collection": None
            if self.collection_result is None
            else self.collection_result.as_dict(),
            "strategy": self.strategy_result.summary(),
            "risk": self.risk_result.summary(),
            "execution": self.execution_result.summary(),
            "table_counts": dict(self.table_counts),
        }


def run_dry_run_cycle(
    config: BotConfig,
    *,
    database_url: str | Path | None = None,
    target_symbols: Sequence[str] | str | None = None,
    symbol_map_path: str | Path = DEFAULT_SYMBOL_MAP_PATH,
    collector_settings: CollectorSettings | None = None,
    feature_config: FeatureConfig | None = None,
    fixture_fallback: bool = True,
    force_fixture: bool = False,
    fixture_bar_count: int = DEFAULT_FIXTURE_BAR_COUNT,
    now_utc: datetime | None = None,
    enforce_freshness: bool = True,
    kill_switch_file: str | Path | None = Path("config/KILL_SWITCH"),
    mt5_module: Any | None = None,
) -> DryRunCycleResult:
    """Run one collect-feature-strategy-risk-execution dry-run cycle."""
    started_at = _utc_now()
    now = _to_utc(now_utc) if now_utc is not None else started_at
    symbols = normalize_symbols(target_symbols if target_symbols is not None else config.target_symbols)
    db_url = database_url or config.database_url or DEFAULT_DATABASE_URL

    with SQLiteStore(db_url) as store:
        store.initialize_schema()

    data_mode = "mt5_read_only"
    fallback_reason: str | None = None
    collection_result: CollectionCycleResult | None = None
    if force_fixture:
        data_mode = "synthetic_fixture_forced"
        collection_result = seed_synthetic_fixture_data(
            db_url,
            target_symbols=symbols,
            now_utc=now,
            count=fixture_bar_count,
            reason="forced fixture dry-run",
        )
    else:
        try:
            collection_result = collect_market_account_once(
                config,
                database_url=db_url,
                target_symbols=symbols,
                symbol_map_path=symbol_map_path,
                settings=collector_settings,
                mt5_module=mt5_module,
                now_utc=now,
            )
        except SAFE_COLLECTION_ERRORS as exc:
            fallback_reason = _exception_summary(exc)
            if not fixture_fallback:
                raise DryRunOrchestrationError(
                    "read-only MT5 collection failed and fixture fallback is disabled: "
                    + fallback_reason
                ) from exc
            if _should_seed_fixture_after_collection_failure(db_url, symbols):
                data_mode = "synthetic_fixture_fallback"
                collection_result = seed_synthetic_fixture_data(
                    db_url,
                    target_symbols=symbols,
                    now_utc=now,
                    count=fixture_bar_count,
                    reason=fallback_reason,
                )
            else:
                data_mode = "stored_data_fallback"
                ensure_default_account_and_flat_positions(
                    db_url,
                    target_symbols=symbols,
                    now_utc=now,
                    reason=fallback_reason,
                )

    try:
        strategy_result, risk_result, execution_result = run_strategy_risk_execution_once(
            db_url,
            config=config,
            target_symbols=symbols,
            feature_config=feature_config,
            now_utc=now,
            enforce_freshness=enforce_freshness,
            kill_switch_file=kill_switch_file,
        )
    except (FeatureEngineeringError, StrategyEngineError) as exc:
        raise DryRunOrchestrationError(str(exc)) from exc

    with SQLiteStore(db_url) as store:
        table_counts = {
            table: store.count_rows(table)
            for table in ("signals", "risk_checks", "orders", "account_snapshots")
        }

    return DryRunCycleResult(
        started_at_utc=started_at,
        finished_at_utc=_utc_now(),
        data_mode=data_mode,
        fallback_reason=fallback_reason,
        target_symbols=symbols,
        collection_result=collection_result,
        strategy_result=strategy_result,
        risk_result=risk_result,
        execution_result=execution_result,
        table_counts=table_counts,
    )


def run_dry_run_session(
    config: BotConfig,
    *,
    once: bool = True,
    minutes: float | None = None,
    poll_seconds: float = DEFAULT_DRY_RUN_POLL_SECONDS,
    **cycle_kwargs: Any,
) -> list[DryRunCycleResult]:
    """Run one or more dry-run cycles with conservative bounded polling."""
    if poll_seconds < MIN_DRY_RUN_POLL_SECONDS:
        raise DryRunOrchestrationError(
            f"poll_seconds must be >= {MIN_DRY_RUN_POLL_SECONDS:g} for conservative polling"
        )
    if not once and (minutes is None or minutes <= 0):
        raise DryRunOrchestrationError("minutes must be positive unless --once is used")

    results: list[DryRunCycleResult] = []
    stop_at = time.monotonic() + float(minutes or 0.0) * 60.0
    while True:
        results.append(run_dry_run_cycle(config, **cycle_kwargs))
        if once:
            break
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_seconds, remaining))
    return results


def collect_market_account_once(
    config: BotConfig,
    *,
    database_url: str | Path,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    symbol_map_path: str | Path = DEFAULT_SYMBOL_MAP_PATH,
    settings: CollectorSettings | None = None,
    mt5_module: Any | None = None,
    now_utc: datetime | None = None,
) -> CollectionCycleResult:
    """Collect read-only market, account, and position state in one MT5 session."""
    symbols = normalize_symbols(target_symbols)
    symbol_map = load_confirmed_symbol_map(symbol_map_path, target_symbols=symbols)
    credentials = build_mt5_credentials(config)
    mt5 = mt5_module or load_mt5_module()
    initialized = False
    try:
        initialize_mt5(credentials, mt5)
        initialized = True
        login_mt5(credentials, mt5)
        observed_at = _to_utc(now_utc) if now_utc is not None else _utc_now()
        with SQLiteStore(database_url) as store:
            collector = MarketDataCollector(
                mt5,
                store,
                symbol_map,
                settings=settings or CollectorSettings(),
            )
            result = collector.collect_once()
            store.upsert_account_snapshot(
                _account_snapshot_from_mt5(mt5, store=store, observed_at_utc=observed_at)
            )
            position_snapshots = (
                read_positions(
                    mt5,
                    broker_to_canonical={broker: canonical for canonical, broker in symbol_map.items()},
                    observed_at_utc=observed_at,
                    store=store,
                )
                if hasattr(mt5, "positions_get")
                else tuple()
            )
            _insert_flat_position_snapshots(
                store,
                symbols=symbols,
                observed_at_utc=observed_at,
                existing_symbols={snapshot.symbol for snapshot in position_snapshots},
                source="mt5_read_only_flat_snapshot",
            )
            return result
    finally:
        if initialized:
            shutdown_mt5(mt5)


def run_strategy_risk_execution_once(
    database_url: str | Path,
    *,
    config: BotConfig,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    feature_config: FeatureConfig | None = None,
    now_utc: datetime | None = None,
    enforce_freshness: bool = True,
    kill_switch_file: str | Path | None = Path("config/KILL_SWITCH"),
) -> tuple[StrategyCycleResult, RiskBatchResult, ExecutionBatchResult]:
    """Compute features, generate signals, risk-check intents, and record dry-run results."""
    symbols = normalize_symbols(target_symbols)
    now = _to_utc(now_utc) if now_utc is not None else _utc_now()
    features = compute_feature_snapshots_from_store(
        database_url,
        target_symbols=symbols,
        config=feature_config,
        require_data=True,
        now_utc=now,
    )

    with SQLiteStore(database_url) as store:
        strategy_context = load_strategy_context_from_store(
            store,
            symbols,
            now_utc=now,
            enforce_freshness=enforce_freshness,
        )
        engine = DryRunStrategyEngine(
            params=config.strategy_params(),
            strategy_version=config.strategy_version,
            magic=config.bot_magic,
        )
        store.upsert_strategy_version(engine.params, active=True, approved_by="dry_run")
        strategy_result = engine.generate_signals(
            features,
            context=strategy_context,
            store=store,
            latest_only=True,
        )

    risk_context = load_risk_context_from_store(
        database_url,
        target_symbols=symbols,
        now_utc=now,
        kill_switch_file=kill_switch_file,
    )
    risk_engine = RiskEngine(RiskLimits.from_config(config))
    execution_engine = ExecutionEngine(config=config)
    with SQLiteStore(database_url) as store:
        risk_result = risk_engine.check_order_intents(
            strategy_result.order_intents,
            risk_context,
            store=store,
        )
        execution_result = execution_engine.execute_approved_orders(
            risk_result.approved_orders,
            store=store,
        )
    return strategy_result, risk_result, execution_result


def seed_synthetic_fixture_data(
    database_url: str | Path,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    now_utc: datetime | None = None,
    count: int = DEFAULT_FIXTURE_BAR_COUNT,
    reason: str | None = None,
) -> CollectionCycleResult:
    """Seed deterministic market/account data for offline dry-run smoke tests."""
    symbols = normalize_symbols(target_symbols)
    now = _to_utc(now_utc) if now_utc is not None else _utc_now()
    start = now - timedelta(minutes=5 * count)
    bars, ticks = make_synthetic_fixture_market_data(
        symbols=symbols,
        count=count,
        start_utc=start,
    )
    with SQLiteStore(database_url) as store:
        metadata_written = 0
        for symbol in symbols:
            store.upsert_symbol_metadata(_fixture_symbol_metadata(symbol, now, reason))
            metadata_written += 1
        bars_written = store.upsert_bars(bars)
        ticks_written = store.upsert_ticks(ticks)
        store.upsert_account_snapshot(_default_account_snapshot(now, reason=reason))
        _insert_flat_position_snapshots(
            store,
            symbols=symbols,
            observed_at_utc=now,
            existing_symbols=set(),
            source="synthetic_fixture_flat_snapshot",
        )
    return CollectionCycleResult(
        started_at_utc=now,
        finished_at_utc=_utc_now(),
        symbols=symbols,
        bars_written=bars_written,
        ticks_written=ticks_written,
        metadata_written=metadata_written,
        request_count=0,
        errors={"fallback_reason": reason or "synthetic fixture dry-run"},
    )


def ensure_default_account_and_flat_positions(
    database_url: str | Path,
    *,
    target_symbols: Sequence[str] | str | None,
    now_utc: datetime,
    reason: str | None,
) -> None:
    """Ensure stored-data fallback has account and flat position snapshots."""
    symbols = normalize_symbols(target_symbols)
    with SQLiteStore(database_url) as store:
        account = store.fetch_one("SELECT observed_at_utc FROM account_snapshots LIMIT 1")
        if account is None:
            store.upsert_account_snapshot(_default_account_snapshot(now_utc, reason=reason))
        _insert_flat_position_snapshots(
            store,
            symbols=symbols,
            observed_at_utc=now_utc,
            existing_symbols=set(),
            source="stored_data_fallback_flat_snapshot",
        )


def _account_snapshot_from_mt5(
    mt5_module: Any,
    *,
    store: SQLiteStore,
    observed_at_utc: datetime,
) -> AccountSnapshot:
    account = read_account_info(mt5_module)
    balance = _float_or_default(account.get("balance"), INITIAL_EQUITY)
    equity = _float_or_default(account.get("equity"), balance)
    return AccountSnapshot(
        observed_at_utc=observed_at_utc,
        balance=max(balance, 0.0),
        equity=max(equity, 0.0),
        profit=_float_or_default(account.get("profit"), 0.0),
        margin=max(_float_or_default(account.get("margin"), 0.0), 0.0),
        margin_free=_optional_float(account.get("margin_free")),
        margin_level=_optional_float(account.get("margin_level")),
        gross_leverage=0.0,
        max_drawdown=_max_drawdown_with_equity(store, equity),
        currency=str(account.get("currency") or "USD"),
        raw={"source": "mt5.account_info", **account},
    )


def _default_account_snapshot(now: datetime, *, reason: str | None) -> AccountSnapshot:
    return AccountSnapshot(
        observed_at_utc=now,
        balance=INITIAL_EQUITY,
        equity=INITIAL_EQUITY,
        profit=0.0,
        margin=0.0,
        margin_free=INITIAL_EQUITY,
        margin_level=None,
        gross_leverage=0.0,
        max_drawdown=0.0,
        currency="USD",
        raw={
            "source": "dry_run_fixture_account",
            "reason": reason,
            "dry_run_only": True,
        },
    )


def _fixture_symbol_metadata(symbol: str, now: datetime, reason: str | None) -> SymbolConfig:
    low_price_symbols = {"BAR/USD", "XRP/USD"}
    point = 0.00001 if symbol in low_price_symbols else 0.01
    return SymbolConfig(
        symbol=symbol,
        broker_symbol=symbol.replace("/", ""),
        digits=5 if symbol in low_price_symbols else 2,
        point=point,
        trade_tick_size=point,
        trade_tick_value=1.0,
        trade_contract_size=1.0,
        volume_min=0.01,
        volume_max=1_000_000.0,
        volume_step=0.01,
        spread=1.0,
        filling_mode=1,
        trade_mode=4,
        raw={
            "source": "synthetic_fixture_metadata",
            "observed_at_utc": now.isoformat(),
            "reason": reason,
            "dry_run_only": True,
        },
    )


def _insert_flat_position_snapshots(
    store: SQLiteStore,
    *,
    symbols: tuple[str, ...],
    observed_at_utc: datetime,
    existing_symbols: set[str],
    source: str,
) -> None:
    for symbol in symbols:
        if symbol in existing_symbols:
            continue
        store.insert_position_snapshot(
            PositionSnapshot(
                observed_at_utc=observed_at_utc,
                symbol=symbol,
                side=PositionSide.FLAT,
                volume=0.0,
                profit=0.0,
                raw={"source": source, "dry_run_only": True},
            )
        )


def _should_seed_fixture_after_collection_failure(
    database_url: str | Path,
    symbols: tuple[str, ...],
) -> bool:
    with SQLiteStore(database_url) as store:
        if not _has_market_data(store, symbols):
            return True
        return _latest_market_source_is_fixture(store, symbols)


def _has_market_data(store: SQLiteStore, symbols: tuple[str, ...]) -> bool:
    placeholders = ",".join("?" for _ in symbols)
    bars = store.fetch_one(
        f"SELECT COUNT(*) AS count FROM bars WHERE symbol IN ({placeholders})",
        symbols,
    )
    ticks = store.fetch_one(
        f"SELECT COUNT(*) AS count FROM ticks WHERE symbol IN ({placeholders})",
        symbols,
    )
    metadata = store.fetch_one(
        f"SELECT COUNT(*) AS count FROM symbol_metadata WHERE symbol IN ({placeholders})",
        symbols,
    )
    return bool(
        bars
        and ticks
        and metadata
        and int(bars["count"]) > 0
        and int(ticks["count"]) > 0
        and int(metadata["count"]) > 0
    )


def _latest_market_source_is_fixture(store: SQLiteStore, symbols: tuple[str, ...]) -> bool:
    placeholders = ",".join("?" for _ in symbols)
    row = store.fetch_one(
        f"""
        SELECT source
        FROM bars
        WHERE symbol IN ({placeholders})
        ORDER BY time_utc DESC
        LIMIT 1
        """,
        symbols,
    )
    if row is None:
        return False
    return str(row["source"] or "").startswith("synthetic_fixture")


def _max_drawdown_with_equity(store: SQLiteStore, current_equity: float) -> float:
    rows = store.fetch_all(
        """
        SELECT equity, max_drawdown
        FROM account_snapshots
        ORDER BY observed_at_utc
        """
    )
    equities = [_optional_float(row["equity"]) for row in rows]
    valid_equities = [value for value in equities if value is not None and value > 0]
    valid_equities.append(current_equity)
    if not valid_equities:
        return 0.0
    peak = valid_equities[0]
    max_drawdown = 0.0
    for equity in valid_equities:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    for row in rows:
        existing = _optional_float(row["max_drawdown"])
        if existing is not None:
            max_drawdown = max(max_drawdown, existing)
    return max_drawdown


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _float_or_default(value: Any, default: float) -> float:
    number = _optional_float(value)
    return default if number is None else number


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _exception_summary(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


__all__ = [
    "DEFAULT_DRY_RUN_POLL_SECONDS",
    "DEFAULT_FIXTURE_BAR_COUNT",
    "MIN_DRY_RUN_POLL_SECONDS",
    "DryRunCycleResult",
    "DryRunOrchestrationError",
    "collect_market_account_once",
    "run_dry_run_cycle",
    "run_dry_run_session",
    "run_strategy_risk_execution_once",
    "seed_synthetic_fixture_data",
]
