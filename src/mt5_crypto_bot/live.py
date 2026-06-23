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
from mt5_crypto_bot.schemas import ExecutionStatus, OrderIntent, normalize_symbols
from mt5_crypto_bot.storage import SQLiteStore
from mt5_crypto_bot.strategy import (
    DryRunStrategyEngine,
    StrategyCycleResult,
    StrategyEngineError,
    load_strategy_context_from_store,
)
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


DEFAULT_LIVE_POLL_SECONDS = 15.0
DEFAULT_FAILED_ORDER_SUPPRESSION_SECONDS = 10 * 60.0
LOGGER = logging.getLogger(__name__)


class LiveRunError(RuntimeError):
    """Raised when the guarded live runner cannot proceed safely."""


@dataclass(frozen=True)
class SuppressedOrderIntent:
    """An unchanged failed intent skipped before another risk/execution attempt."""

    order_intent: OrderIntent
    reason: str
    attempts: int
    first_failed_at_utc: datetime
    last_failed_at_utc: datetime
    suppressed_until_utc: datetime


@dataclass
class _RetryState:
    reason: str
    attempts: int
    first_failed_at_utc: datetime
    last_failed_at_utc: datetime
    suppressed_until_utc: datetime


class OrderRetryGuard:
    """Suppress exact duplicate order intents after deterministic failures."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = DEFAULT_FAILED_ORDER_SUPPRESSION_SECONDS,
        max_entries: int = 256,
    ) -> None:
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_entries = max(1, int(max_entries))
        self._failures: dict[str, _RetryState] = {}

    def filter_order_intents(
        self,
        order_intents: Sequence[OrderIntent],
        *,
        now_utc: datetime,
    ) -> tuple[tuple[OrderIntent, ...], tuple[SuppressedOrderIntent, ...]]:
        """Return intents that should be retried and exact duplicates to skip."""

        now = _to_utc(now_utc)
        self._prune(now)
        allowed: list[OrderIntent] = []
        suppressed: list[SuppressedOrderIntent] = []
        for intent in order_intents:
            state = self._failures.get(_intent_fingerprint(intent))
            if state is None or state.suppressed_until_utc <= now:
                allowed.append(intent)
                continue
            suppressed.append(
                SuppressedOrderIntent(
                    order_intent=intent,
                    reason=state.reason,
                    attempts=state.attempts,
                    first_failed_at_utc=state.first_failed_at_utc,
                    last_failed_at_utc=state.last_failed_at_utc,
                    suppressed_until_utc=state.suppressed_until_utc,
                )
            )
        return tuple(allowed), tuple(suppressed)

    def observe_risk_result(self, risk_result: RiskBatchResult, *, now_utc: datetime) -> None:
        """Record blocked risk checks so unchanged intents are not retried immediately."""

        now = _to_utc(now_utc)
        for decision in risk_result.decisions:
            intent = decision.order_intent
            if intent is None:
                continue
            if decision.passed:
                self.clear_intent(intent)
                continue
            self.record_failure(intent, decision.risk_check.reason or "risk check blocked", now_utc=now)

    def observe_execution_result(
        self,
        risk_result: RiskBatchResult,
        execution_result: ExecutionBatchResult,
        *,
        now_utc: datetime,
    ) -> None:
        """Record live precheck/check/send failures and clear successful attempts."""

        now = _to_utc(now_utc)
        intents_by_id = {
            approved.order_intent.client_order_id: approved.order_intent
            for approved in risk_result.approved_orders
        }
        for result in execution_result.results:
            intent = intents_by_id.get(result.client_order_id)
            if intent is None:
                continue
            status = str(getattr(result.status, "value", result.status))
            if status in {
                ExecutionStatus.FILLED.value,
                ExecutionStatus.PARTIAL.value,
                ExecutionStatus.DRY_RUN.value,
            }:
                self.clear_intent(intent)
                continue
            self.record_failure(
                intent,
                result.message or f"execution status {status}",
                now_utc=now,
            )

    def record_failure(self, intent: OrderIntent, reason: str, *, now_utc: datetime) -> None:
        """Record one failed attempt for an exact order-intent fingerprint."""

        now = _to_utc(now_utc)
        key = _intent_fingerprint(intent)
        existing = self._failures.get(key)
        first_failed = existing.first_failed_at_utc if existing else now
        attempts = (existing.attempts + 1) if existing else 1
        self._failures[key] = _RetryState(
            reason=reason,
            attempts=attempts,
            first_failed_at_utc=first_failed,
            last_failed_at_utc=now,
            suppressed_until_utc=datetime.fromtimestamp(
                now.timestamp() + self.cooldown_seconds,
                tz=timezone.utc,
            ),
        )
        self._prune(now)

    def clear_intent(self, intent: OrderIntent) -> None:
        self._failures.pop(_intent_fingerprint(intent), None)

    def _prune(self, now: datetime) -> None:
        expired = [
            key
            for key, state in self._failures.items()
            if state.suppressed_until_utc <= now
        ]
        for key in expired:
            self._failures.pop(key, None)
        while len(self._failures) > self.max_entries:
            oldest_key = min(
                self._failures,
                key=lambda item: self._failures[item].last_failed_at_utc,
            )
            self._failures.pop(oldest_key, None)


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
    suppressed_order_intents: tuple[SuppressedOrderIntent, ...]
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
            "suppressed_order_intents": [
                {
                    "client_order_id": item.order_intent.client_order_id,
                    "symbol": item.order_intent.symbol,
                    "side": str(getattr(item.order_intent.side, "value", item.order_intent.side)),
                    "requested_volume": item.order_intent.requested_volume,
                    "requested_price": item.order_intent.requested_price,
                    "reason": item.reason,
                    "attempts": item.attempts,
                    "first_failed_at_utc": item.first_failed_at_utc.isoformat(),
                    "last_failed_at_utc": item.last_failed_at_utc.isoformat(),
                    "suppressed_until_utc": item.suppressed_until_utc.isoformat(),
                }
                for item in self.suppressed_order_intents
            ],
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
    order_retry_guard: OrderRetryGuard | None = None,
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
        strategy_result, risk_result, suppressed_order_intents = _run_strategy_and_risk_once(
            db_url,
            config=config,
            target_symbols=symbols,
            feature_config=feature_config,
            now_utc=started_at,
            kill_switch_file=kill_switch_file,
            order_retry_guard=order_retry_guard,
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
    if order_retry_guard is not None:
        observed_at = _utc_now()
        order_retry_guard.observe_risk_result(risk_result, now_utc=observed_at)
        order_retry_guard.observe_execution_result(
            risk_result,
            execution_result,
            now_utc=observed_at,
        )
    with SQLiteStore(db_url) as store:
        table_counts = {
            table: store.count_rows(table)
            for table in ("signals", "risk_checks", "orders", "account_snapshots")
        }
    LOGGER.info(
        "live cycle complete symbols=%s signals=%s order_intents=%s suppressed=%s approved=%s sent_to_mt5=%s table_counts=%s",
        len(symbols),
        len(strategy_result.signals),
        len(strategy_result.order_intents),
        len(suppressed_order_intents),
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
        suppressed_order_intents=suppressed_order_intents,
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
    retry_guard = cycle_kwargs.pop("order_retry_guard", None) or OrderRetryGuard()
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
                order_retry_guard=retry_guard,
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
    order_retry_guard: OrderRetryGuard | None,
) -> tuple[StrategyCycleResult, RiskBatchResult, tuple[SuppressedOrderIntent, ...]]:
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

    order_intents = strategy_result.order_intents
    suppressed_order_intents: tuple[SuppressedOrderIntent, ...] = tuple()
    if order_retry_guard is not None and order_intents:
        order_intents, suppressed_order_intents = order_retry_guard.filter_order_intents(
            order_intents,
            now_utc=now_utc,
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
            order_intents,
            risk_context,
            store=store,
        )
    return strategy_result, risk_result, suppressed_order_intents


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


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _intent_fingerprint(intent: OrderIntent) -> str:
    feature_time = intent.metadata.get("feature_time_utc")
    return "|".join(
        (
            intent.strategy_version,
            intent.client_order_id,
            str(intent.signal_id or ""),
            intent.symbol,
            str(getattr(intent.side, "value", intent.side)),
            _rounded_key(intent.requested_volume),
            _rounded_key(intent.requested_price),
            _rounded_key(intent.stop_loss),
            _rounded_key(intent.take_profit),
            str(feature_time or ""),
        )
    )


def _rounded_key(value: float | None) -> str:
    if value is None:
        return ""
    return f"{float(value):.10g}"


__all__ = [
    "DEFAULT_LIVE_POLL_SECONDS",
    "DEFAULT_FAILED_ORDER_SUPPRESSION_SECONDS",
    "LiveCycleResult",
    "LiveRunError",
    "OrderRetryGuard",
    "SuppressedOrderIntent",
    "run_live_cycle",
    "run_live_session",
]
