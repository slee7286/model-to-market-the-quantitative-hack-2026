"""Dry-run execution and guarded MT5 request helpers.

Dry-run remains the default behavior. Guarded live execution is reachable only
through an explicit constructor override plus ``LIVE_APPROVED=true`` and a local
approval artifact before it can even run ``order_check``.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import DEFAULT_DATABASE_URL
from mt5_crypto_bot.mt5_client import read_last_error
from mt5_crypto_bot.risk import RiskApprovedOrder
from mt5_crypto_bot.schemas import (
    ExecutionResult,
    ExecutionStatus,
    Fill,
    FillSide,
    OrderIntent,
    OrderSide,
    OrderType,
    PositionSide,
    PositionSnapshot,
    RiskCheck,
    TimeInForce,
    TradeMode,
    normalize_symbol,
)
from mt5_crypto_bot.storage import SQLiteStore


DEFAULT_LIVE_APPROVAL_FILE = Path("config/LIVE_APPROVED.json")
LIVE_APPROVAL_ENV = "LIVE_APPROVED"
DRY_RUN_RESULT_MESSAGE = "dry-run only: order intent recorded; no MT5 order_check/order_send called"
# MT5 rejects order_check/order_send with code -2 ('Invalid "comment" argument')
# once the comment reaches 30 characters; 29 is the largest length the terminal
# accepts. Truncating any higher silently fails every order at order_check.
MT5_MAX_COMMENT_LENGTH = 29
LIVE_TICK_MAX_AGE_SECONDS = 60.0
LIQUIDITY_USAGE_FRACTION = 0.80
EPSILON = 1e-12

LOGGER = logging.getLogger(__name__)


class ExecutionEngineError(RuntimeError):
    """Base class for execution-layer failures."""


class UnapprovedOrderError(ExecutionEngineError):
    """Raised when execution is attempted without a passing risk approval."""


class LiveTradingApprovalError(ExecutionEngineError):
    """Raised when a live-only path is requested without explicit approval."""


class LiveExecutionError(ExecutionEngineError):
    """Raised when guarded live execution fails before a result is available."""


@dataclass(frozen=True)
class ExecutionBatchResult:
    """Execution results from a batch of approved orders."""

    results: tuple[ExecutionResult, ...]

    @property
    def dry_run_count(self) -> int:
        return sum(1 for result in self.results if result.status == ExecutionStatus.DRY_RUN.value)

    def summary(self) -> dict[str, Any]:
        sent_to_mt5 = sum(1 for result in self.results if result.result.get("sent_to_mt5"))
        return {
            "orders": len(self.results),
            "dry_run": self.dry_run_count,
            "sent_to_mt5": sent_to_mt5,
            "statuses": [result.status for result in self.results],
        }


@dataclass(frozen=True)
class _PreparedLiveIntent:
    intent: OrderIntent
    diagnostics: dict[str, Any]
    rejection_reason: str | None = None


class ExecutionEngine:
    """Record approved order intents as dry-run or guarded live execution results."""

    def __init__(
        self,
        config: BotConfig | None = None,
        *,
        trade_mode: str | TradeMode | None = None,
        live_approval_file: str | Path = DEFAULT_LIVE_APPROVAL_FILE,
        live_approval_env: str = LIVE_APPROVAL_ENV,
    ) -> None:
        self.config = config
        self.trade_mode = _trade_mode_value(
            trade_mode if trade_mode is not None else config.trade_mode if config else TradeMode.DRY_RUN
        )
        self.live_approval_file = Path(live_approval_file)
        self.live_approval_env = live_approval_env

    def execute_approved_order(
        self,
        approved_order: RiskApprovedOrder | Mapping[str, Any],
        *,
        store: SQLiteStore | None = None,
        mt5_module: Any | None = None,
    ) -> ExecutionResult:
        """Execute one approved order.

        ``dry_run`` and ``paper`` both record a simulated result. ``live`` is
        only reachable through explicit constructor override and is blocked
        unless the approval mechanism passes.
        """
        approved = _coerce_approved_order(approved_order)
        _ensure_approved(approved)
        if self.trade_mode in {TradeMode.DRY_RUN.value, TradeMode.PAPER.value}:
            return self._execute_dry_run(approved, store=store)
        if self.trade_mode == "live":
            return self._execute_live_guarded(approved, store=store, mt5_module=mt5_module)
        raise ExecutionEngineError(f"unsupported execution trade mode: {self.trade_mode!r}")

    def execute_order_intent(
        self,
        order_intent: OrderIntent | Mapping[str, Any],
        *,
        risk_check: RiskCheck | Mapping[str, Any],
        approval_id: str | None = None,
        approved_at_utc: datetime | None = None,
        store: SQLiteStore | None = None,
        mt5_module: Any | None = None,
    ) -> ExecutionResult:
        """Execute an ``OrderIntent`` only when paired with a passing risk check."""
        intent = order_intent if isinstance(order_intent, OrderIntent) else OrderIntent(**dict(order_intent))
        check = risk_check if isinstance(risk_check, RiskCheck) else RiskCheck(**dict(risk_check))
        approved = RiskApprovedOrder(
            order_intent=intent,
            risk_check=check,
            approval_id=approval_id or _approval_id_from_check(check),
            approved_at_utc=approved_at_utc or check.checked_at_utc,
        )
        return self.execute_approved_order(approved, store=store, mt5_module=mt5_module)

    def execute_approved_orders(
        self,
        approved_orders: Iterable[RiskApprovedOrder | Mapping[str, Any]],
        *,
        store: SQLiteStore | None = None,
        mt5_module: Any | None = None,
    ) -> ExecutionBatchResult:
        """Execute a batch of risk-approved orders."""
        return ExecutionBatchResult(
            tuple(
                self.execute_approved_order(
                    approved_order,
                    store=store,
                    mt5_module=mt5_module,
                )
                for approved_order in approved_orders
            )
        )

    def _execute_dry_run(
        self,
        approved_order: RiskApprovedOrder,
        *,
        store: SQLiteStore | None,
    ) -> ExecutionResult:
        intent = approved_order.order_intent
        request = build_mt5_order_request(intent, require_broker_symbol=False)
        result_payload = {
            "dry_run": True,
            "sent_to_mt5": False,
            "order_check_called": False,
            "order_send_called": False,
            "risk_approval_id": approved_order.approval_id,
            "risk_check_id": approved_order.risk_check.check_id,
            "risk_reason": approved_order.risk_check.reason,
        }
        result = ExecutionResult(
            client_order_id=intent.client_order_id,
            executed_at_utc=_utc_now(),
            trade_mode=TradeMode.DRY_RUN
            if self.trade_mode == TradeMode.DRY_RUN.value
            else TradeMode.PAPER,
            status=ExecutionStatus.DRY_RUN,
            symbol=intent.symbol,
            requested_volume=intent.requested_volume,
            filled_volume=0.0,
            requested_price=intent.requested_price,
            message=DRY_RUN_RESULT_MESSAGE,
            request=request,
            result=result_payload,
        )
        if store is not None:
            store.upsert_order_intent(intent, status="approved")
            store.upsert_execution_result(result)
        return result

    def _execute_live_guarded(
        self,
        approved_order: RiskApprovedOrder,
        *,
        store: SQLiteStore | None,
        mt5_module: Any | None,
    ) -> ExecutionResult:
        self.require_live_approval()
        if mt5_module is None:
            raise LiveExecutionError("mt5_module is required for guarded live execution")

        prepared = _prepare_intent_for_live_execution(approved_order.order_intent, mt5_module)
        intent = prepared.intent
        if prepared.rejection_reason is not None:
            result = ExecutionResult(
                client_order_id=intent.client_order_id,
                executed_at_utc=_utc_now(),
                trade_mode=TradeMode.LIVE,
                status=ExecutionStatus.REJECTED,
                symbol=intent.symbol,
                requested_volume=intent.requested_volume,
                requested_price=intent.requested_price,
                message=f"live order rejected before order_check: {prepared.rejection_reason}",
                request={},
                result={
                    "sent_to_mt5": False,
                    "order_check_called": False,
                    "order_send_called": False,
                    "live_precheck": prepared.diagnostics,
                    "risk_approval_id": approved_order.approval_id,
                    "risk_check_id": approved_order.risk_check.check_id,
                    "risk_reason": approved_order.risk_check.reason,
                },
            )
            LOGGER.warning(
                "live precheck rejected order symbol=%s side=%s volume=%s reason=%s diagnostics=%s",
                intent.symbol,
                _value(intent.side),
                intent.requested_volume,
                prepared.rejection_reason,
                _compact_json(prepared.diagnostics),
            )
            if store is not None:
                store.upsert_order_intent(intent, status="live_precheck_rejected")
                store.upsert_execution_result(result)
            return result

        request = build_mt5_order_request(intent, mt5_module=mt5_module, require_broker_symbol=True)
        if store is not None:
            store.upsert_order_intent(intent, status="live_check_pending")
        LOGGER.info(
            "live order prepared symbol=%s side=%s volume=%s price=%s sl=%s tp=%s diagnostics=%s",
            intent.symbol,
            _value(intent.side),
            intent.requested_volume,
            intent.requested_price,
            intent.stop_loss,
            intent.take_profit,
            _compact_json(prepared.diagnostics),
        )

        check_result = mt5_module.order_check(request)
        check_payload = _object_to_mapping(check_result)
        last_error = read_last_error(mt5_module) if check_result is None else None
        if not _order_check_passed(check_result, mt5_module):
            LOGGER.warning(
                "live order_check rejected symbol=%s side=%s volume=%s price=%s retcode=%s check=%s last_error=%s",
                intent.symbol,
                _value(intent.side),
                intent.requested_volume,
                intent.requested_price,
                _retcode(check_result),
                _compact_json(check_payload),
                _compact_json(last_error),
            )
            result = ExecutionResult(
                client_order_id=intent.client_order_id,
                executed_at_utc=_utc_now(),
                trade_mode=TradeMode.LIVE,
                status=ExecutionStatus.REJECTED,
                symbol=intent.symbol,
                requested_volume=intent.requested_volume,
                requested_price=intent.requested_price,
                retcode=_retcode(check_result),
                message="live order_check rejected request; order_send was not called",
                request=request,
                result={
                    "sent_to_mt5": False,
                    "order_check_called": True,
                    "order_send_called": False,
                    "live_precheck": prepared.diagnostics,
                    "order_check": check_payload,
                    "last_error": last_error,
                },
            )
            if store is not None:
                store.upsert_execution_result(result)
            return result

        send_result = mt5_module.order_send(request)
        send_payload = _object_to_mapping(send_result)
        status = _execution_status_from_send(
            send_result,
            mt5_module,
            requested_volume=intent.requested_volume,
        )
        LOGGER.info(
            "live order_send result symbol=%s side=%s requested_volume=%s filled_volume=%s "
            "requested_price=%s fill_price=%s status=%s retcode=%s payload=%s",
            intent.symbol,
            _value(intent.side),
            intent.requested_volume,
            _as_float(send_payload.get("volume")),
            intent.requested_price,
            _positive_or_none(send_payload.get("price")),
            status.value,
            _retcode(send_result),
            _compact_json(send_payload),
        )
        result = ExecutionResult(
            client_order_id=intent.client_order_id,
            executed_at_utc=_utc_now(),
            trade_mode=TradeMode.LIVE,
            status=status,
            symbol=intent.symbol,
            requested_volume=intent.requested_volume,
            filled_volume=_as_float(send_payload.get("volume")),
            requested_price=intent.requested_price,
            average_fill_price=_positive_or_none(send_payload.get("price")),
            mt5_order_ticket=_as_optional_int(send_payload.get("order")),
            mt5_deal_ticket=_as_optional_int(send_payload.get("deal")),
            retcode=_retcode(send_result),
            message=str(send_payload.get("comment") or "guarded live order_send result"),
            request=request,
            result={
                "sent_to_mt5": True,
                "order_check_called": True,
                "order_send_called": True,
                "live_precheck": prepared.diagnostics,
                "order_check": check_payload,
                "last_error": last_error,
                "order_send": send_payload,
            },
        )
        if store is not None:
            store.upsert_execution_result(result)
        return result

    def require_live_approval(self) -> dict[str, Any]:
        """Validate explicit live-approval controls.

        This method intentionally checks both an environment flag and a local JSON
        file. The repository never creates that file in unattended runs.
        """
        if self.trade_mode != "live":
            raise LiveTradingApprovalError(
                f"live execution requires execution trade_mode='live'; current mode is {self.trade_mode!r}"
            )
        if not _truthy(os.environ.get(self.live_approval_env)):
            raise LiveTradingApprovalError(
                f"live execution requires {self.live_approval_env}=true in the runtime environment"
            )
        if not self.live_approval_file.exists():
            raise LiveTradingApprovalError(
                f"live execution requires approval file {self.live_approval_file}"
            )
        try:
            payload = json.loads(self.live_approval_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveTradingApprovalError(
                f"live approval file is unreadable or invalid JSON: {self.live_approval_file}"
            ) from exc
        if not isinstance(payload, dict) or not (
            _truthy(payload.get("live_approved")) or _truthy(payload.get("approved"))
        ):
            raise LiveTradingApprovalError(
                "live approval file must contain live_approved=true or approved=true"
            )
        return dict(payload)


def execute_dry_run_orders(
    approved_orders: Iterable[RiskApprovedOrder | Mapping[str, Any]],
    *,
    database_url: str | Path = DEFAULT_DATABASE_URL,
) -> ExecutionBatchResult:
    """Record approved orders as dry-run results in local SQLite storage."""
    with SQLiteStore(database_url) as store:
        return ExecutionEngine().execute_approved_orders(approved_orders, store=store)


def build_mt5_order_request(
    order_intent: OrderIntent | Mapping[str, Any],
    *,
    mt5_module: Any | None = None,
    broker_symbol: str | None = None,
    require_broker_symbol: bool = True,
) -> dict[str, Any]:
    """Build an MT5 request dictionary without sending it."""
    intent = order_intent if isinstance(order_intent, OrderIntent) else OrderIntent(**dict(order_intent))
    resolved_symbol = broker_symbol or _broker_symbol_from_intent(intent)
    if not resolved_symbol:
        if require_broker_symbol:
            raise ExecutionEngineError(
                "broker symbol is required for live MT5 request building; run symbol bootstrap first"
            )
        resolved_symbol = intent.symbol

    request: dict[str, Any] = {
        "action": _request_action(intent, mt5_module),
        "symbol": resolved_symbol,
        "volume": float(intent.requested_volume),
        "type": _request_order_type(intent, mt5_module),
        "deviation": int(intent.deviation_points),
        "magic": int(intent.magic),
        "comment": _request_comment(intent),
        "type_time": _request_time_type(intent.time_in_force, mt5_module),
        "type_filling": _request_filling_type(intent.time_in_force, mt5_module),
    }
    if intent.requested_price is not None:
        request["price"] = float(intent.requested_price)
    elif intent.order_type != OrderType.MARKET.value:
        raise ExecutionEngineError("pending MT5 orders require requested_price")
    if intent.stop_loss is not None:
        request["sl"] = float(intent.stop_loss)
    if intent.take_profit is not None:
        request["tp"] = float(intent.take_profit)
    return request


def _prepare_intent_for_live_execution(
    intent: OrderIntent,
    mt5_module: Any | None,
) -> _PreparedLiveIntent:
    """Refresh live price/SL/TP and apply visible-depth volume controls."""

    refreshed = _refresh_intent_to_live_market(intent, mt5_module)
    if refreshed.rejection_reason is not None:
        return refreshed
    liquidity = _apply_liquidity_constraints(refreshed.intent, mt5_module)
    diagnostics = dict(refreshed.diagnostics)
    diagnostics["liquidity"] = liquidity.diagnostics.get("liquidity", liquidity.diagnostics)
    if liquidity.rejection_reason is not None:
        return _PreparedLiveIntent(
            intent=liquidity.intent,
            diagnostics=diagnostics,
            rejection_reason=liquidity.rejection_reason,
        )
    return _PreparedLiveIntent(intent=liquidity.intent, diagnostics=diagnostics)


def _refresh_intent_to_live_market(
    intent: OrderIntent,
    mt5_module: Any | None,
) -> _PreparedLiveIntent:
    """Re-anchor a market order's price and SL/TP to a fresh live tick."""

    if mt5_module is None:
        return _PreparedLiveIntent(intent, {}, "MT5 module unavailable")
    if _value(intent.order_type) != OrderType.MARKET.value:
        return _PreparedLiveIntent(intent, {"live_refresh": {"skipped": "non-market order"}})
    if not hasattr(mt5_module, "symbol_info_tick"):
        return _PreparedLiveIntent(intent, {}, "MT5 symbol_info_tick unavailable")

    broker_symbol = _broker_symbol_from_intent(intent) or intent.symbol
    tick = _object_to_mapping(mt5_module.symbol_info_tick(broker_symbol))
    side = _value(intent.side)
    bid = _as_optional_float(tick.get("bid"))
    ask = _as_optional_float(tick.get("ask"))
    new_price = ask if side == OrderSide.BUY.value else bid
    tick_time = _tick_time_utc(tick)
    tick_age_seconds = (_utc_now() - tick_time).total_seconds() if tick_time is not None else None
    diagnostics: dict[str, Any] = {
        "live_refresh": {
            "broker_symbol": broker_symbol,
            "side": side,
            "original_requested_price": intent.requested_price,
            "live_bid": bid,
            "live_ask": ask,
            "live_tick_time_utc": tick_time.isoformat() if tick_time else None,
            "live_tick_age_seconds": tick_age_seconds,
        }
    }
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return _PreparedLiveIntent(intent, diagnostics, "live bid/ask unavailable or invalid")
    if new_price is None or new_price <= 0:
        return _PreparedLiveIntent(intent, diagnostics, "live execution price unavailable")
    if tick_age_seconds is None:
        return _PreparedLiveIntent(intent, diagnostics, "live tick timestamp unavailable")
    if tick_age_seconds < -5:
        return _PreparedLiveIntent(intent, diagnostics, "live tick timestamp is in the future")
    if tick_age_seconds > LIVE_TICK_MAX_AGE_SECONDS:
        return _PreparedLiveIntent(
            intent,
            diagnostics,
            f"live tick is stale ({tick_age_seconds:.1f}s > {LIVE_TICK_MAX_AGE_SECONDS:.1f}s)",
        )

    info = _object_to_mapping(mt5_module.symbol_info(broker_symbol)) if hasattr(mt5_module, "symbol_info") else {}
    digits = _as_optional_int(info.get("digits"))
    point = _as_optional_float(info.get("point")) or 0.0
    stops_level = _as_optional_int(info.get("trade_stops_level")) or 0
    min_dist = (stops_level + 1) * point if point > 0 else 0.0

    def _round(value: float) -> float:
        return round(value, digits) if digits is not None else value

    def _away(level: float, above: bool) -> float:
        if min_dist <= 0:
            return level
        return max(level, new_price + min_dist) if above else min(level, new_price - min_dist)

    update: dict[str, Any] = {"requested_price": _round(new_price)}
    refresh_payload = diagnostics["live_refresh"]
    refresh_payload.update(
        {
            "live_requested_price": update["requested_price"],
            "digits": digits,
            "point": point,
            "trade_stops_level": stops_level,
            "min_stop_distance": min_dist,
        }
    )
    old_price = intent.requested_price
    if old_price is not None and old_price > 0:
        is_buy = side == OrderSide.BUY.value
        if intent.stop_loss is not None:
            stop_offset = intent.stop_loss - old_price
            sl = new_price + stop_offset
            update["stop_loss"] = _round(_away(sl, above=not is_buy))
            refresh_payload["preserved_stop_offset"] = stop_offset
            refresh_payload["refreshed_stop_loss"] = update["stop_loss"]
        if intent.take_profit is not None:
            take_profit_offset = intent.take_profit - old_price
            tp = new_price + take_profit_offset
            update["take_profit"] = _round(_away(tp, above=is_buy))
            refresh_payload["preserved_take_profit_offset"] = take_profit_offset
            refresh_payload["refreshed_take_profit"] = update["take_profit"]

    metadata = dict(intent.metadata)
    metadata["dry_run_only"] = False
    metadata["live_execution"] = True
    metadata["live_refresh"] = refresh_payload
    update["metadata"] = metadata
    return _PreparedLiveIntent(intent.model_copy(update=update), diagnostics)


def _apply_liquidity_constraints(
    intent: OrderIntent,
    mt5_module: Any | None,
) -> _PreparedLiveIntent:
    """Cap market-order volume using visible depth when MT5 exposes it."""

    broker_symbol = _broker_symbol_from_intent(intent) or intent.symbol
    info = _object_to_mapping(mt5_module.symbol_info(broker_symbol)) if mt5_module is not None and hasattr(mt5_module, "symbol_info") else {}
    volume_min = _as_optional_float(info.get("volume_min"))
    volume_max = _as_optional_float(info.get("volume_max"))
    volume_step = _as_optional_float(info.get("volume_step"))
    point = _as_optional_float(info.get("point")) or 0.0
    diagnostics: dict[str, Any] = {
        "liquidity": {
            "broker_symbol": broker_symbol,
            "source": "not_checked",
            "requested_volume_before": intent.requested_volume,
            "requested_volume_after": intent.requested_volume,
            "volume_min": volume_min,
            "volume_max": volume_max,
            "volume_step": volume_step,
            "usage_fraction": LIQUIDITY_USAGE_FRACTION,
        }
    }
    if mt5_module is None or not hasattr(mt5_module, "market_book_get"):
        diagnostics["liquidity"]["source"] = "market_book_unavailable"
        return _PreparedLiveIntent(intent, diagnostics)

    book_rows = _read_market_book(mt5_module, broker_symbol)
    if not book_rows:
        diagnostics["liquidity"]["source"] = "market_book_empty"
        return _PreparedLiveIntent(intent, diagnostics)

    side = _value(intent.side)
    requested_price = _as_optional_float(intent.requested_price)
    deviation_price = max(float(intent.deviation_points) * point, 0.0) if point > 0 else None
    eligible = [
        level
        for level in book_rows
        if _book_level_matches_order(
            level,
            side=side,
            mt5_module=mt5_module,
            requested_price=requested_price,
            deviation_price=deviation_price,
        )
    ]
    visible_volume = sum(_book_level_volume(level) for level in eligible)
    diagnostics["liquidity"].update(
        {
            "source": "market_book",
            "depth_levels": len(book_rows),
            "eligible_levels": len(eligible),
            "visible_volume": visible_volume,
            "deviation_points": intent.deviation_points,
            "deviation_price": deviation_price,
        }
    )
    if visible_volume <= 0:
        return _PreparedLiveIntent(
            intent,
            diagnostics,
            "no visible liquidity inside deviation window",
        )

    raw_cap = visible_volume * LIQUIDITY_USAGE_FRACTION
    capped_volume = min(float(intent.requested_volume), raw_cap)
    if volume_max is not None and volume_max > 0:
        capped_volume = min(capped_volume, volume_max)
    rounded_volume = _round_volume_down(capped_volume, volume_step)
    diagnostics["liquidity"]["raw_volume_cap"] = raw_cap
    diagnostics["liquidity"]["requested_volume_after"] = rounded_volume
    if volume_min is not None and rounded_volume < volume_min:
        return _PreparedLiveIntent(
            intent.model_copy(update={"metadata": _metadata_with(intent, "liquidity", diagnostics["liquidity"])}),
            diagnostics,
            "visible liquidity cap is below broker minimum volume",
        )
    if rounded_volume <= 0:
        return _PreparedLiveIntent(intent, diagnostics, "visible liquidity cap rounds to zero")
    if rounded_volume >= float(intent.requested_volume) - EPSILON:
        metadata = _metadata_with(intent, "liquidity", diagnostics["liquidity"])
        return _PreparedLiveIntent(intent.model_copy(update={"metadata": metadata}), diagnostics)

    diagnostics["liquidity"]["cap_reason"] = "requested volume reduced to 80% of visible depth"
    metadata = _metadata_with(intent, "liquidity", diagnostics["liquidity"])
    return _PreparedLiveIntent(
        intent.model_copy(update={"requested_volume": rounded_volume, "metadata": metadata}),
        diagnostics,
    )


def read_positions(
    mt5_module: Any,
    *,
    broker_to_canonical: Mapping[str, str] | None = None,
    observed_at_utc: datetime | None = None,
    store: SQLiteStore | None = None,
) -> tuple[PositionSnapshot, ...]:
    """Read open MT5 positions without modifying orders or positions."""
    raw_positions = mt5_module.positions_get()
    snapshots = tuple(
        snapshot
        for snapshot in (
            _position_snapshot_from_mt5(position, mt5_module, broker_to_canonical, observed_at_utc)
            for position in _materialize_mt5_sequence(raw_positions)
        )
        if snapshot is not None
    )
    if store is not None:
        for snapshot in snapshots:
            store.insert_position_snapshot(snapshot)
    return snapshots


def read_deals(
    mt5_module: Any,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    *,
    broker_to_canonical: Mapping[str, str] | None = None,
    store: SQLiteStore | None = None,
) -> tuple[Fill, ...]:
    """Read MT5 deal history without placing, modifying, or closing orders."""
    start = _datetime_from_any(date_from) if date_from is not None else _utc_now() - timedelta(days=1)
    end = _datetime_from_any(date_to) if date_to is not None else _utc_now()
    raw_deals = mt5_module.history_deals_get(start, end)
    order_lookup = _load_order_lookup_for_fills(store) if store is not None else {}
    fills = tuple(
        fill
        for fill in (
            _fill_from_mt5_deal(deal, mt5_module, broker_to_canonical, order_lookup)
            for deal in _materialize_mt5_sequence(raw_deals)
        )
        if fill is not None
    )
    if store is not None:
        for fill in fills:
            store.insert_fill(fill)
    return fills


def _coerce_approved_order(value: RiskApprovedOrder | Mapping[str, Any]) -> RiskApprovedOrder:
    if isinstance(value, RiskApprovedOrder):
        return value
    if not isinstance(value, Mapping):
        raise UnapprovedOrderError(
            f"expected RiskApprovedOrder or mapping, got {type(value).__name__}"
        )
    try:
        intent = value.get("order_intent")
        check = value.get("risk_check")
        if intent is None or check is None:
            raise KeyError("order_intent and risk_check are required")
        order_intent = intent if isinstance(intent, OrderIntent) else OrderIntent(**dict(intent))
        risk_check = check if isinstance(check, RiskCheck) else RiskCheck(**dict(check))
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise UnapprovedOrderError(f"invalid approved-order payload: {exc}") from exc
    return RiskApprovedOrder(
        order_intent=order_intent,
        risk_check=risk_check,
        approval_id=str(value.get("approval_id") or _approval_id_from_check(risk_check)),
        approved_at_utc=_datetime_from_any(value.get("approved_at_utc") or risk_check.checked_at_utc),
    )


def _ensure_approved(approved_order: RiskApprovedOrder) -> None:
    if not approved_order.risk_check.passed:
        raise UnapprovedOrderError(
            "execution requires a passing risk check; blocked orders are not executable"
        )
    if not approved_order.approval_id:
        raise UnapprovedOrderError("execution requires a risk approval id")


def _approval_id_from_check(risk_check: RiskCheck) -> str:
    raw = risk_check.details.get("approval_id")
    if raw:
        return str(raw)
    if risk_check.check_id:
        return f"risk-approved-{risk_check.check_id}"
    return f"risk-approved-{risk_check.signal_id or 'unknown'}"


def _broker_symbol_from_intent(intent: OrderIntent) -> str | None:
    raw = intent.metadata.get("broker_symbol")
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip()


def _metadata_with(intent: OrderIntent, key: str, value: Any) -> dict[str, Any]:
    metadata = dict(intent.metadata)
    metadata[key] = value
    return metadata


def _tick_time_utc(tick: Mapping[str, Any]) -> datetime | None:
    raw = tick.get("time_msc") or tick.get("time") or tick.get("time_utc")
    if raw is None:
        return None
    try:
        return _datetime_from_any(raw)
    except Exception:
        return None


def _read_market_book(mt5_module: Any, broker_symbol: str) -> tuple[dict[str, Any], ...]:
    added = False
    if hasattr(mt5_module, "market_book_add"):
        try:
            added = bool(mt5_module.market_book_add(broker_symbol))
        except Exception:
            added = False
    try:
        raw_book = mt5_module.market_book_get(broker_symbol)
        return tuple(_object_to_mapping(level) for level in _materialize_mt5_sequence(raw_book))
    finally:
        if added and hasattr(mt5_module, "market_book_release"):
            try:
                mt5_module.market_book_release(broker_symbol)
            except Exception:
                pass


def _book_level_matches_order(
    level: Mapping[str, Any],
    *,
    side: str,
    mt5_module: Any,
    requested_price: float | None,
    deviation_price: float | None,
) -> bool:
    level_type = _as_optional_int(level.get("type"))
    price = _as_optional_float(level.get("price"))
    if level_type is None or price is None or price <= 0:
        return False
    if side == OrderSide.BUY.value:
        ask_types = {
            _mt5_constant(mt5_module, "BOOK_TYPE_SELL", 1),
            _mt5_constant(mt5_module, "BOOK_TYPE_SELL_MARKET", 3),
        }
        if level_type not in ask_types:
            return False
        return requested_price is None or deviation_price is None or price <= requested_price + deviation_price
    bid_types = {
        _mt5_constant(mt5_module, "BOOK_TYPE_BUY", 2),
        _mt5_constant(mt5_module, "BOOK_TYPE_BUY_MARKET", 4),
    }
    if level_type not in bid_types:
        return False
    return requested_price is None or deviation_price is None or price >= requested_price - deviation_price


def _book_level_volume(level: Mapping[str, Any]) -> float:
    volume = _as_optional_float(level.get("volume_dbl"))
    if volume is None:
        volume = _as_optional_float(level.get("volume"))
    return max(volume or 0.0, 0.0)


def _round_volume_down(value: float, step: float | None) -> float:
    if step is None or step <= 0 or not math.isfinite(value):
        return max(float(value), 0.0)
    rounded = math.floor((float(value) + EPSILON) / step) * step
    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    return round(max(rounded, 0.0), decimals + 2)


def _request_action(intent: OrderIntent, mt5_module: Any | None) -> int:
    if intent.order_type == OrderType.MARKET.value:
        return _mt5_constant(mt5_module, "TRADE_ACTION_DEAL", 1)
    return _mt5_constant(mt5_module, "TRADE_ACTION_PENDING", 5)


def _request_order_type(intent: OrderIntent, mt5_module: Any | None) -> int:
    side = _value(intent.side)
    order_type = _value(intent.order_type)
    if order_type == OrderType.MARKET.value:
        return _mt5_constant(
            mt5_module,
            "ORDER_TYPE_BUY" if side == OrderSide.BUY.value else "ORDER_TYPE_SELL",
            0 if side == OrderSide.BUY.value else 1,
        )
    if order_type == OrderType.LIMIT.value:
        return _mt5_constant(
            mt5_module,
            "ORDER_TYPE_BUY_LIMIT" if side == OrderSide.BUY.value else "ORDER_TYPE_SELL_LIMIT",
            2 if side == OrderSide.BUY.value else 3,
        )
    if order_type == OrderType.STOP.value:
        return _mt5_constant(
            mt5_module,
            "ORDER_TYPE_BUY_STOP" if side == OrderSide.BUY.value else "ORDER_TYPE_SELL_STOP",
            4 if side == OrderSide.BUY.value else 5,
        )
    raise ExecutionEngineError(f"unsupported order type for MT5 request: {order_type!r}")


def _request_time_type(time_in_force: TimeInForce | str, mt5_module: Any | None) -> int:
    tif = _value(time_in_force)
    if tif == TimeInForce.DAY.value:
        return _mt5_constant(mt5_module, "ORDER_TIME_DAY", 1)
    return _mt5_constant(mt5_module, "ORDER_TIME_GTC", 0)


def _request_filling_type(time_in_force: TimeInForce | str, mt5_module: Any | None) -> int:
    tif = _value(time_in_force)
    if tif == TimeInForce.FOK.value:
        return _mt5_constant(mt5_module, "ORDER_FILLING_FOK", 0)
    if tif == TimeInForce.IOC.value:
        return _mt5_constant(mt5_module, "ORDER_FILLING_IOC", 1)
    return _mt5_constant(mt5_module, "ORDER_FILLING_RETURN", 2)


def _request_comment(intent: OrderIntent) -> str:
    comment = intent.comment or f"{intent.strategy_version}:{intent.client_order_id}"
    return comment[:MT5_MAX_COMMENT_LENGTH]


def _mt5_constant(mt5_module: Any | None, name: str, fallback: int) -> int:
    if mt5_module is None:
        return fallback
    return int(getattr(mt5_module, name, fallback))


def _order_check_passed(check_result: Any, mt5_module: Any) -> bool:
    if check_result is None:
        return False
    retcode = _retcode(check_result)
    if retcode is None:
        return True
    allowed = {
        0,
        _mt5_constant(mt5_module, "TRADE_RETCODE_DONE", 10009),
        _mt5_constant(mt5_module, "TRADE_RETCODE_PLACED", 10008),
    }
    return retcode in allowed


def _execution_status_from_send(
    send_result: Any,
    mt5_module: Any,
    *,
    requested_volume: float | None = None,
) -> ExecutionStatus:
    retcode = _retcode(send_result)
    payload = _object_to_mapping(send_result)
    filled_volume = _as_optional_float(payload.get("volume"))
    if retcode in {
        _mt5_constant(mt5_module, "TRADE_RETCODE_DONE", 10009),
        _mt5_constant(mt5_module, "TRADE_RETCODE_PLACED", 10008),
    }:
        if requested_volume is not None and filled_volume is not None and filled_volume < requested_volume - EPSILON:
            return ExecutionStatus.PARTIAL
        return ExecutionStatus.FILLED
    if retcode == _mt5_constant(mt5_module, "TRADE_RETCODE_DONE_PARTIAL", 10010):
        return ExecutionStatus.PARTIAL
    return ExecutionStatus.FAILED


def _position_snapshot_from_mt5(
    raw_position: Any,
    mt5_module: Any,
    broker_to_canonical: Mapping[str, str] | None,
    observed_at_utc: datetime | None,
) -> PositionSnapshot | None:
    data = _object_to_mapping(raw_position)
    symbol = _canonical_symbol(data.get("symbol"), broker_to_canonical)
    if symbol is None:
        return None
    position_type = data.get("type")
    buy_type = _mt5_constant(mt5_module, "POSITION_TYPE_BUY", 0)
    sell_type = _mt5_constant(mt5_module, "POSITION_TYPE_SELL", 1)
    side = PositionSide.LONG if position_type == buy_type else PositionSide.SHORT if position_type == sell_type else PositionSide.FLAT
    return PositionSnapshot(
        observed_at_utc=_datetime_from_any(observed_at_utc or data.get("time_update_msc") or data.get("time_msc") or data.get("time") or _utc_now()),
        symbol=symbol,
        ticket=_as_optional_int(data.get("ticket")),
        side=side,
        volume=_as_float(data.get("volume")),
        price_open=_positive_or_none(data.get("price_open")),
        price_current=_positive_or_none(data.get("price_current")),
        stop_loss=_positive_or_none(data.get("sl")),
        take_profit=_positive_or_none(data.get("tp")),
        profit=_as_float(data.get("profit")),
        raw=data,
    )


def _fill_from_mt5_deal(
    raw_deal: Any,
    mt5_module: Any,
    broker_to_canonical: Mapping[str, str] | None,
    order_lookup: Mapping[int, Mapping[str, Any]] | None = None,
) -> Fill | None:
    data = _object_to_mapping(raw_deal)
    symbol = _canonical_symbol(data.get("symbol"), broker_to_canonical)
    if symbol is None:
        return None
    deal_type = data.get("type")
    buy_type = _mt5_constant(mt5_module, "DEAL_TYPE_BUY", 0)
    sell_type = _mt5_constant(mt5_module, "DEAL_TYPE_SELL", 1)
    if deal_type == buy_type:
        side = FillSide.BUY
    elif deal_type == sell_type:
        side = FillSide.SELL
    else:
        return None
    price = _as_optional_float(data.get("price"))
    volume = _as_optional_float(data.get("volume"))
    if price is None or price <= 0 or volume is None or volume <= 0:
        return None
    order_ticket = _as_optional_int(data.get("order"))
    order_details = order_lookup.get(order_ticket) if order_lookup and order_ticket is not None else None
    slippage_bps = _fill_slippage_bps(
        requested_price=_as_optional_float(order_details.get("requested_price")) if order_details else None,
        fill_price=price,
        side=side,
    )
    if order_details:
        data["matched_client_order_id"] = order_details.get("client_order_id")
        data["matched_requested_price"] = order_details.get("requested_price")
        data["matched_requested_volume"] = order_details.get("requested_volume")
        data["computed_slippage_bps"] = slippage_bps
    return Fill(
        deal_ticket=_as_optional_int(data.get("ticket")),
        order_ticket=order_ticket,
        position_id=_as_optional_int(data.get("position_id")),
        symbol=symbol,
        filled_at_utc=_datetime_from_any(data.get("time_msc") or data.get("time") or _utc_now()),
        side=side,
        volume=volume,
        price=price,
        profit=_as_float(data.get("profit")),
        commission=_as_float(data.get("commission")),
        swap=_as_float(data.get("swap")),
        slippage_bps=slippage_bps,
        raw=data,
    )


def _load_order_lookup_for_fills(store: SQLiteStore | None) -> dict[int, dict[str, Any]]:
    if store is None:
        return {}
    rows = store.fetch_all(
        """
        SELECT client_order_id, mt5_order_ticket, mt5_deal_ticket, side,
               requested_price, requested_volume
        FROM orders
        WHERE mt5_order_ticket IS NOT NULL OR mt5_deal_ticket IS NOT NULL
        """
    )
    lookup: dict[int, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        order_ticket = _as_optional_int(data.get("mt5_order_ticket"))
        if order_ticket is not None:
            lookup[order_ticket] = data
    return lookup


def _fill_slippage_bps(
    *,
    requested_price: float | None,
    fill_price: float,
    side: FillSide,
) -> float | None:
    if requested_price is None or requested_price <= 0 or fill_price <= 0:
        return None
    if side == FillSide.BUY:
        return (fill_price - requested_price) / requested_price * 10_000.0
    if side == FillSide.SELL:
        return (requested_price - fill_price) / requested_price * 10_000.0
    return abs(fill_price - requested_price) / requested_price * 10_000.0


def _canonical_symbol(
    raw_symbol: Any,
    broker_to_canonical: Mapping[str, str] | None,
) -> str | None:
    if raw_symbol is None:
        return None
    symbol = str(raw_symbol).strip()
    if broker_to_canonical and symbol in broker_to_canonical:
        try:
            return normalize_symbol(broker_to_canonical[symbol])
        except (TypeError, ValueError):
            return None
    try:
        return normalize_symbol(symbol)
    except (TypeError, ValueError):
        return None


def _materialize_mt5_sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return tuple(value)


def _object_to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        return {str(key): _json_safe(item) for key, item in dict(asdict()).items()}
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if not callable(item):
            result[name] = _json_safe(item)
    return result


def _retcode(value: Any) -> int | None:
    data = _object_to_mapping(value)
    return _as_optional_int(data.get("retcode"))


def _trade_mode_value(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def _value(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "approved"}


def _datetime_from_any(value: Any) -> datetime:
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
    raise ExecutionEngineError(f"cannot parse datetime value: {value!r}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    number = _as_optional_float(value)
    return 0.0 if number is None else number


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _positive_or_none(value: Any) -> float | None:
    """Treat MT5's 0.0 'unset' sentinel (and non-positive values) as absent.

    MT5 reports sl/tp (and occasionally prices) as 0.0 when not set, but the
    PositionSnapshot schema requires those fields to be > 0 when present.
    """
    number = _as_optional_float(value)
    if number is None or number <= 0:
        return None
    return number


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return _datetime_from_any(value).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Iterable) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    return str(value)


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(value)


__all__ = [
    "DEFAULT_LIVE_APPROVAL_FILE",
    "DRY_RUN_RESULT_MESSAGE",
    "ExecutionBatchResult",
    "ExecutionEngine",
    "ExecutionEngineError",
    "LiveExecutionError",
    "LiveTradingApprovalError",
    "UnapprovedOrderError",
    "build_mt5_order_request",
    "execute_dry_run_orders",
    "read_deals",
    "read_positions",
]
