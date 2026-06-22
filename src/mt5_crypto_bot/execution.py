"""Dry-run execution and guarded MT5 request helpers.

Dry-run remains the default behavior. Guarded live execution is reachable only
through an explicit constructor override plus ``LIVE_APPROVED=true`` and a local
approval artifact before it can even run ``order_check``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import DEFAULT_DATABASE_URL
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


class ExecutionEngineError(RuntimeError):
    """Base class for execution-layer failures."""


class UnapprovedOrderError(ExecutionEngineError):
    """Raised when execution is attempted without a passing risk approval."""


class LiveTradingApprovalError(ExecutionEngineError):
    """Raised when a live-only path is requested without explicit approval."""


class LiveExecutionError(ExecutionEngineError):
    """Raised when guarded future live execution fails before a result is available."""


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


class ExecutionEngine:
    """Record approved order intents as dry-run execution results."""

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

        In the current non-live build, ``dry_run`` and ``paper`` both record a
        simulated result. ``live`` is only reachable through explicit constructor
        override and is blocked unless the approval mechanism passes.
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

        intent = approved_order.order_intent
        request = build_mt5_order_request(intent, mt5_module=mt5_module, require_broker_symbol=True)
        if store is not None:
            store.upsert_order_intent(intent, status="live_check_pending")

        check_result = mt5_module.order_check(request)
        check_payload = _object_to_mapping(check_result)
        if not _order_check_passed(check_result, mt5_module):
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
                    "order_check": check_payload,
                },
            )
            if store is not None:
                store.upsert_execution_result(result)
            return result

        send_result = mt5_module.order_send(request)
        send_payload = _object_to_mapping(send_result)
        result = ExecutionResult(
            client_order_id=intent.client_order_id,
            executed_at_utc=_utc_now(),
            trade_mode=TradeMode.LIVE,
            status=_execution_status_from_send(send_result, mt5_module),
            symbol=intent.symbol,
            requested_volume=intent.requested_volume,
            filled_volume=_as_float(send_payload.get("volume")),
            requested_price=intent.requested_price,
            average_fill_price=_as_optional_float(send_payload.get("price")),
            mt5_order_ticket=_as_optional_int(send_payload.get("order")),
            mt5_deal_ticket=_as_optional_int(send_payload.get("deal")),
            retcode=_retcode(send_result),
            message=str(send_payload.get("comment") or "guarded live order_send result"),
            request=request,
            result={
                "sent_to_mt5": True,
                "order_check_called": True,
                "order_send_called": True,
                "order_check": check_payload,
                "order_send": send_payload,
            },
        )
        if store is not None:
            store.upsert_execution_result(result)
        return result

    def require_live_approval(self) -> dict[str, Any]:
        """Validate explicit future live-approval controls.

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
    fills = tuple(
        fill
        for fill in (
            _fill_from_mt5_deal(deal, mt5_module, broker_to_canonical)
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
    return comment[:31]


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


def _execution_status_from_send(send_result: Any, mt5_module: Any) -> ExecutionStatus:
    retcode = _retcode(send_result)
    if retcode in {
        _mt5_constant(mt5_module, "TRADE_RETCODE_DONE", 10009),
        _mt5_constant(mt5_module, "TRADE_RETCODE_PLACED", 10008),
    }:
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
        price_open=_as_optional_float(data.get("price_open")),
        price_current=_as_optional_float(data.get("price_current")),
        stop_loss=_as_optional_float(data.get("sl")),
        take_profit=_as_optional_float(data.get("tp")),
        profit=_as_float(data.get("profit")),
        raw=data,
    )


def _fill_from_mt5_deal(
    raw_deal: Any,
    mt5_module: Any,
    broker_to_canonical: Mapping[str, str] | None,
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
    return Fill(
        deal_ticket=_as_optional_int(data.get("ticket")),
        order_ticket=_as_optional_int(data.get("order")),
        position_id=_as_optional_int(data.get("position_id")),
        symbol=symbol,
        filled_at_utc=_datetime_from_any(data.get("time_msc") or data.get("time") or _utc_now()),
        side=side,
        volume=volume,
        price=price,
        profit=_as_float(data.get("profit")),
        commission=_as_float(data.get("commission")),
        swap=_as_float(data.get("swap")),
        raw=data,
    )


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
