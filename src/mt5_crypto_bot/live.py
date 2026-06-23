"""Guarded live MT5 orchestration.

This module is intentionally separate from the dry-run runner. It requires a
bounded runtime, confirmed symbol mapping, fresh MT5 data, risk approval, and
the explicit live-approval gates enforced by ``ExecutionEngine`` before any MT5
``order_check`` or ``order_send`` call can happen.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_DATABASE_URL
from mt5_crypto_bot.data_collector import CollectionCycleResult, CollectorSettings, load_confirmed_symbol_map
from mt5_crypto_bot.dry_run import MIN_DRY_RUN_POLL_SECONDS, collect_market_account_once
from mt5_crypto_bot.execution import (
    DEFAULT_LIVE_APPROVAL_FILE,
    ExecutionBatchResult,
    ExecutionEngine,
    LiveTradingApprovalError,
    read_deals,
    read_positions,
)
from mt5_crypto_bot.features import (
    FeatureConfig,
    FeatureEngineeringError,
    compute_feature_snapshots_from_store,
)
from mt5_crypto_bot.mt5_client import (
    build_mt5_credentials,
    initialize_mt5,
    load_mt5_module,
    login_mt5,
    shutdown_mt5,
)
from mt5_crypto_bot.risk import RiskBatchResult, RiskEngine, RiskLimits, load_risk_context_from_store
from mt5_crypto_bot.schemas import normalize_symbols
from mt5_crypto_bot.storage import SQLiteStore
from mt5_crypto_bot.strategy import (
    DryRunStrategyEngine,
    StrategyCycleResult,
    StrategyEngineError,
    load_strategy_context_from_store,
)
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


DEFAULT_LIVE_POLL_SECONDS = 15.0
LOGGER = logging.getLogger(__name__)


class LiveRunError(RuntimeError):
    """Raised when the guarded live runner cannot proceed safely."""


@dataclass(frozen=True)
class LiveCycleResult:
    """Summary of one guarded live cycle."""

    started_at_utc: datetime
    finished_at_utc: datetime
    target_symbols: tuple[str, ...]
    collection_result: CollectionCycleResult
    strategy_result: StrategyCycleResult
    risk_result: RiskBatchResult
    execution_result: ExecutionBatchResult
    table_counts: Mapping[str, int]

    def summary(self) -> dict[str, Any]:
        """Return a JSON-safe live-cycle summary."""
        return {
            "started_at_utc": self.started_at_utc.isoformat(),
            "finished_at_utc": self.finished_at_utc.isoformat(),
            "target_symbols": list(self.target_symbols),
            "collection": self.collection_result.as_dict(),
            "strategy": self.strategy_result.summary(),
            "risk": self.risk_result.summary(),
            "execution": self.execution_result.summary(),
            "table_counts": dict(self.table_counts),
        }


def run_live_cycle(
    config: BotConfig,
    *,
    database_url: str | Path | None = None,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    symbol_map_path: str | Path = DEFAULT_SYMBOL_MAP_PATH,
    collector_settings: CollectorSettings | None = None,
    feature_config: FeatureConfig | None = None,
    kill_switch_file: str | Path | None = Path("config/KILL_SWITCH"),
    live_approval_file: str | Path = DEFAULT_LIVE_APPROVAL_FILE,
    minutes_limit: float | None = None,
    mt5_module: Any | None = None,
    manage_connection: bool = True,
) -> LiveCycleResult:
    """Run one guarded live collection, signal, risk, and execution cycle.

    When ``manage_connection`` is False, ``mt5_module`` is an already-connected
    session owned by the caller; collection and execution reuse it without
    initializing or shutting it down.
    """
    symbols = normalize_symbols(target_symbols)
    db_url = database_url or config.database_url or DEFAULT_DATABASE_URL
    started_at = _utc_now()
    approval_payload = _require_live_approval(
        symbols=symbols,
        live_approval_file=live_approval_file,
        minutes_limit=minutes_limit,
    )
    del approval_payload

    collection_result = collect_market_account_once(
        config,
        database_url=db_url,
        target_symbols=symbols,
        symbol_map_path=symbol_map_path,
        settings=collector_settings,
        mt5_module=mt5_module,
        now_utc=started_at,
        manage_connection=manage_connection,
    )

    try:
        strategy_result, risk_result = _run_strategy_and_risk_once(
            db_url,
            config=config,
            target_symbols=symbols,
            feature_config=feature_config,
            now_utc=started_at,
            kill_switch_file=kill_switch_file,
        )
    except (FeatureEngineeringError, StrategyEngineError) as exc:
        raise LiveRunError(str(exc)) from exc

    execution_result = _execute_live_approved_orders(
        risk_result,
        config=config,
        database_url=db_url,
        target_symbols=symbols,
        symbol_map_path=symbol_map_path,
        live_approval_file=live_approval_file,
        mt5_module=mt5_module,
        manage_connection=manage_connection,
    )
    with SQLiteStore(db_url) as store:
        table_counts = {
            table: store.count_rows(table)
            for table in ("signals", "risk_checks", "orders", "account_snapshots")
        }
    LOGGER.info(
        "live cycle complete symbols=%s signals=%s order_intents=%s approved=%s sent_to_mt5=%s table_counts=%s",
        len(symbols),
        len(strategy_result.signals),
        len(strategy_result.order_intents),
        len(risk_result.approved_orders),
        execution_result.summary()["sent_to_mt5"],
        dict(table_counts),
    )
    return LiveCycleResult(
        started_at_utc=started_at,
        finished_at_utc=_utc_now(),
        target_symbols=symbols,
        collection_result=collection_result,
        strategy_result=strategy_result,
        risk_result=risk_result,
        execution_result=execution_result,
        table_counts=table_counts,
    )


def run_live_session(
    config: BotConfig,
    *,
    minutes: float,
    poll_seconds: float = DEFAULT_LIVE_POLL_SECONDS,
    on_cycle: Callable[[LiveCycleResult, int], None] | None = None,
    **cycle_kwargs: Any,
) -> list[LiveCycleResult]:
    """Run a bounded guarded live session with conservative polling.

    ``on_cycle`` is invoked with ``(cycle_result, cycle_number)`` after each
    cycle completes, so callers can report per-cycle order outcomes in real time
    instead of waiting for the whole session to return.
    """
    if minutes <= 0:
        raise LiveRunError("live --minutes must be positive")
    if poll_seconds < MIN_DRY_RUN_POLL_SECONDS:
        raise LiveRunError(
            f"poll_seconds must be >= {MIN_DRY_RUN_POLL_SECONDS:g} for conservative polling"
        )

    # Hold ONE MT5 connection for the whole session instead of reconnecting every
    # cycle. Repeated initialize/login/shutdown forces the terminal to
    # re-authorize to the broker, which churns the connection and resets the
    # terminal's AutoTrading state, causing order_send to be rejected.
    mt5 = cycle_kwargs.pop("mt5_module", None)
    owns_connection = mt5 is None
    initialized = False
    results: list[LiveCycleResult] = []
    try:
        if owns_connection:
            # Validate live approval BEFORE touching MT5 so the connection is
            # never opened without an approved session.
            symbols = normalize_symbols(cycle_kwargs.get("target_symbols") or ALLOWED_SYMBOLS)
            approval_file = cycle_kwargs.get("live_approval_file", DEFAULT_LIVE_APPROVAL_FILE)
            _require_live_approval(
                symbols=symbols,
                live_approval_file=approval_file,
                minutes_limit=minutes,
            )
            credentials = build_mt5_credentials(config)
            mt5 = load_mt5_module()
            LOGGER.info("initializing persistent MT5 session")
            initialize_mt5(credentials, mt5)
            initialized = True
            LOGGER.info("logging in to persistent MT5 session")
            login_mt5(credentials, mt5)

        stop_at = time.monotonic() + float(minutes) * 60.0
        while True:
            LOGGER.info("starting live cycle %s", len(results) + 1)
            cycle_result = run_live_cycle(
                config,
                minutes_limit=minutes,
                mt5_module=mt5,
                manage_connection=False,
                **cycle_kwargs,
            )
            results.append(cycle_result)
            if on_cycle is not None:
                on_cycle(cycle_result, len(results))
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_seconds, remaining))
        return results
    finally:
        if owns_connection and initialized:
            shutdown_mt5(mt5)


def _require_live_approval(
    *,
    symbols: tuple[str, ...],
    live_approval_file: str | Path,
    minutes_limit: float | None,
) -> dict[str, Any]:
    engine = ExecutionEngine(trade_mode="live", live_approval_file=live_approval_file)
    payload = engine.require_live_approval()
    scope = payload.get("scope")
    if scope:
        approved_symbols = set(normalize_symbols(scope))
        missing = [symbol for symbol in symbols if symbol not in approved_symbols]
        if missing:
            raise LiveTradingApprovalError(
                "live approval scope does not include requested symbols: " + ", ".join(missing)
            )
    max_minutes = _as_optional_float(payload.get("max_minutes"))
    if max_minutes is not None and minutes_limit is not None and minutes_limit > max_minutes:
        raise LiveTradingApprovalError(
            f"requested live runtime {minutes_limit:g} minutes exceeds approval max_minutes={max_minutes:g}"
        )
    return payload


def _run_strategy_and_risk_once(
    database_url: str | Path,
    *,
    config: BotConfig,
    target_symbols: tuple[str, ...],
    feature_config: FeatureConfig | None,
    now_utc: datetime,
    kill_switch_file: str | Path | None,
) -> tuple[StrategyCycleResult, RiskBatchResult]:
    features = compute_feature_snapshots_from_store(
        database_url,
        target_symbols=target_symbols,
        config=feature_config,
        require_data=True,
        now_utc=now_utc,
    )
    with SQLiteStore(database_url) as store:
        strategy_context = load_strategy_context_from_store(
            store,
            target_symbols,
            now_utc=now_utc,
            enforce_freshness=True,
        )
        strategy_engine = DryRunStrategyEngine(
            params=config.strategy_params(),
            strategy_version=config.strategy_version,
            magic=config.bot_magic,
        )
        store.upsert_strategy_version(strategy_engine.params, active=True, approved_by="guarded_live")
        strategy_result = strategy_engine.generate_signals(
            features,
            context=strategy_context,
            store=store,
            latest_only=True,
        )

    risk_context = load_risk_context_from_store(
        database_url,
        target_symbols=target_symbols,
        now_utc=now_utc,
        kill_switch_file=kill_switch_file,
    )
    risk_engine = RiskEngine(RiskLimits.from_config(config))
    with SQLiteStore(database_url) as store:
        risk_result = risk_engine.check_order_intents(
            strategy_result.order_intents,
            risk_context,
            store=store,
        )
    return strategy_result, risk_result


def _execute_live_approved_orders(
    risk_result: RiskBatchResult,
    *,
    config: BotConfig,
    database_url: str | Path,
    target_symbols: tuple[str, ...],
    symbol_map_path: str | Path,
    live_approval_file: str | Path,
    mt5_module: Any | None,
    manage_connection: bool = True,
) -> ExecutionBatchResult:
    if not risk_result.approved_orders:
        return ExecutionBatchResult(())

    symbol_map = load_confirmed_symbol_map(symbol_map_path, target_symbols=target_symbols)
    broker_to_canonical = {broker: canonical for canonical, broker in symbol_map.items()}
    mt5 = mt5_module or load_mt5_module()
    initialized = False
    try:
        if manage_connection:
            credentials = build_mt5_credentials(config)
            initialize_mt5(credentials, mt5)
            initialized = True
            login_mt5(credentials, mt5)
        # MT5 requires the symbol selected in Market Watch before order_check;
        # otherwise order_check returns None and the order is treated as rejected.
        if hasattr(mt5, "symbol_select"):
            for broker_symbol in symbol_map.values():
                mt5.symbol_select(broker_symbol, True)
        with SQLiteStore(database_url) as store:
            execution_result = ExecutionEngine(
                config=config,
                trade_mode="live",
                live_approval_file=live_approval_file,
            ).execute_approved_orders(
                risk_result.approved_orders,
                store=store,
                mt5_module=mt5,
            )
            read_positions(
                mt5,
                broker_to_canonical=broker_to_canonical,
                observed_at_utc=_utc_now(),
                store=store,
            )
            read_deals(
                mt5,
                broker_to_canonical=broker_to_canonical,
                store=store,
            )
        return execution_result
    finally:
        if manage_connection and initialized:
            shutdown_mt5(mt5)


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "DEFAULT_LIVE_POLL_SECONDS",
    "LiveCycleResult",
    "LiveRunError",
    "run_live_cycle",
    "run_live_session",
]
