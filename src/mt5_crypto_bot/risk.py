"""Pre-trade risk engine for dry-run order intents.

The strategy layer may emit raw ``OrderIntent`` objects, but this module is the
approval boundary. Later execution code should accept only ``RiskApprovedOrder``
objects produced here, never raw strategy output.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_DATABASE_URL
from mt5_crypto_bot.schemas import (
    AccountSnapshot,
    OrderIntent,
    OrderSide,
    PositionSide,
    PositionSnapshot,
    RiskCheck,
    SymbolConfig,
    normalize_symbol,
    normalize_symbols,
)
from mt5_crypto_bot.storage import SQLiteStore


EPSILON = 1e-9
RULE_MAX_ACCOUNT_LEVERAGE = 30.0

SPREAD_CAP_BPS: dict[str, float] = {
    "BAR/USD": 25.0,
    "BTC/USD": 8.0,
    "ETH/USD": 8.0,
    "SOL/USD": 15.0,
    "XRP/USD": 15.0,
}

HARD_SYMBOL_LEVERAGE_CAP: dict[str, float] = {
    "BAR/USD": 0.75,
    "BTC/USD": 2.00,
    "ETH/USD": 2.00,
    "SOL/USD": 1.50,
    "XRP/USD": 1.25,
}


class RiskEngineError(RuntimeError):
    """Raised when risk state cannot be loaded or interpreted."""


@dataclass(frozen=True)
class RiskLimits:
    """Internal caps, deliberately stricter than ``rules.md`` penalty zones."""

    max_gross_leverage: float = 8.0
    max_symbol_leverage: float = 2.0
    max_margin_usage: float = 0.60
    max_single_instrument_share: float = 0.75
    max_net_directional_share: float = 0.85
    no_new_risk_drawdown: float = 0.08
    hard_drawdown: float = 0.10
    tick_stale_seconds: float = 120.0
    feature_stale_seconds: float = 15 * 60.0
    max_future_feature_skew_seconds: float = 60.0
    concentration_min_gross_leverage: float = 1.0
    account_leverage_cap: float = RULE_MAX_ACCOUNT_LEVERAGE
    kill_switch_file: Path | None = Path("config/KILL_SWITCH")

    @classmethod
    def from_config(cls, config: BotConfig) -> RiskLimits:
        """Build limits from config while never loosening the design freeze caps."""
        total_stop = float(config.total_drawdown_stop)
        return cls(
            max_gross_leverage=min(float(config.max_gross_leverage), 8.0),
            max_symbol_leverage=min(float(config.max_symbol_leverage), 2.0),
            max_margin_usage=min(float(config.max_margin_usage), 0.60),
            no_new_risk_drawdown=min(0.08, total_stop),
            hard_drawdown=min(0.10, total_stop),
        )


@dataclass(frozen=True)
class AccountRiskState:
    """Account fields needed to evaluate portfolio-level risk."""

    balance: float
    equity: float
    margin: float = 0.0
    margin_free: float | None = None
    margin_level: float | None = None
    gross_leverage: float = 0.0
    max_drawdown: float = 0.0
    leverage: float = RULE_MAX_ACCOUNT_LEVERAGE
    observed_at_utc: datetime | None = None

    @classmethod
    def from_snapshot(cls, snapshot: AccountSnapshot | Mapping[str, Any]) -> AccountRiskState:
        data = _model_or_mapping(snapshot)
        raw = data.get("raw") if isinstance(data.get("raw"), Mapping) else {}
        leverage = _as_optional_float(data.get("leverage"))
        if leverage is None and isinstance(raw, Mapping):
            leverage = _as_optional_float(raw.get("leverage"))
        return cls(
            balance=_as_float(data.get("balance")),
            equity=_as_float(data.get("equity")),
            margin=_as_float(data.get("margin")),
            margin_free=_as_optional_float(data.get("margin_free")),
            margin_level=_as_optional_float(data.get("margin_level")),
            gross_leverage=_as_float(data.get("gross_leverage")),
            max_drawdown=_as_float(data.get("max_drawdown")),
            leverage=leverage or RULE_MAX_ACCOUNT_LEVERAGE,
            observed_at_utc=_datetime_from_any(data.get("observed_at_utc"))
            if data.get("observed_at_utc") is not None
            else None,
        )


@dataclass(frozen=True)
class PositionRiskState:
    """Latest known position state for notional and direction calculations."""

    symbol: str
    side: PositionSide | str
    volume: float
    price_current: float | None = None
    price_open: float | None = None
    observed_at_utc: datetime | None = None

    @classmethod
    def from_snapshot(cls, snapshot: PositionSnapshot | Mapping[str, Any]) -> PositionRiskState:
        data = _model_or_mapping(snapshot)
        return cls(
            symbol=normalize_symbol(data["symbol"]),
            side=data.get("side", PositionSide.FLAT),
            volume=_as_float(data.get("volume")),
            price_current=_as_optional_float(data.get("price_current")),
            price_open=_as_optional_float(data.get("price_open")),
            observed_at_utc=_datetime_from_any(data.get("observed_at_utc"))
            if data.get("observed_at_utc") is not None
            else None,
        )


@dataclass(frozen=True)
class MarketRiskState:
    """Latest bid/ask state for one canonical symbol."""

    symbol: str
    observed_at_utc: datetime
    bid: float | None = None
    ask: float | None = None
    last: float | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        return self.ask - self.bid

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return self.last
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return self.last
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float | None:
        spread = self.spread
        mid = self.mid
        if spread is None or mid is None or mid <= 0:
            return None
        return spread / mid * 10_000.0


@dataclass(frozen=True)
class RiskContext:
    """State required to evaluate one or more pre-trade order intents."""

    account: AccountRiskState | AccountSnapshot | Mapping[str, Any] | None = None
    symbol_metadata: Mapping[str, SymbolConfig | Mapping[str, Any]] = field(default_factory=dict)
    positions: Sequence[PositionRiskState | PositionSnapshot | Mapping[str, Any]] = field(
        default_factory=tuple
    )
    market: Mapping[str, MarketRiskState | Mapping[str, Any]] = field(default_factory=dict)
    now_utc: datetime | None = None
    kill_switch_active: bool = False

    def account_state(self) -> AccountRiskState | None:
        if self.account is None:
            return None
        if isinstance(self.account, AccountRiskState):
            return self.account
        return AccountRiskState.from_snapshot(self.account)

    def now(self) -> datetime:
        return _datetime_from_any(self.now_utc) if self.now_utc is not None else _utc_now()


@dataclass(frozen=True)
class PortfolioRiskMetrics:
    """Projected portfolio risk metrics after applying a candidate order."""

    equity: float
    gross_notional: float
    net_notional: float
    gross_leverage: float
    net_directional_exposure: float
    single_instrument_exposure: float
    margin_usage: float
    symbol_leverage: Mapping[str, float]
    symbol_notionals: Mapping[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "equity": self.equity,
            "gross_notional": self.gross_notional,
            "net_notional": self.net_notional,
            "gross_leverage": self.gross_leverage,
            "net_directional_exposure": self.net_directional_exposure,
            "single_instrument_exposure": self.single_instrument_exposure,
            "margin_usage": self.margin_usage,
            "symbol_leverage": dict(self.symbol_leverage),
            "symbol_notionals": dict(self.symbol_notionals),
        }


@dataclass(frozen=True)
class RiskApprovedOrder:
    """Order intent plus its persisted approving risk check."""

    order_intent: OrderIntent
    risk_check: RiskCheck
    approval_id: str
    approved_at_utc: datetime


@dataclass(frozen=True)
class RiskGateDecision:
    """Result of one order-intent risk evaluation."""

    order_intent: OrderIntent | None
    risk_check: RiskCheck
    approved_order: RiskApprovedOrder | None = None

    @property
    def passed(self) -> bool:
        return bool(self.risk_check.passed)

    @property
    def reasons(self) -> tuple[str, ...]:
        raw = self.risk_check.details.get("reasons", [])
        if isinstance(raw, list):
            return tuple(str(item) for item in raw)
        return tuple()


@dataclass(frozen=True)
class RiskBatchResult:
    """Risk-gate output for a batch of strategy order intents."""

    decisions: tuple[RiskGateDecision, ...]

    @property
    def approved_orders(self) -> tuple[RiskApprovedOrder, ...]:
        return tuple(decision.approved_order for decision in self.decisions if decision.approved_order)

    @property
    def risk_checks(self) -> tuple[RiskCheck, ...]:
        return tuple(decision.risk_check for decision in self.decisions)

    def summary(self) -> dict[str, Any]:
        passed = sum(1 for decision in self.decisions if decision.passed)
        return {
            "risk_checks": len(self.decisions),
            "passed": passed,
            "blocked": len(self.decisions) - passed,
            "approved_orders": len(self.approved_orders),
        }


class RiskEngine:
    """Evaluate and persist pre-trade risk checks for dry-run order intents."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def check_order_intents(
        self,
        order_intents: Iterable[OrderIntent | Mapping[str, Any]],
        context: RiskContext,
        *,
        store: SQLiteStore | None = None,
    ) -> RiskBatchResult:
        decisions = tuple(
            self.check_order_intent(order_intent, context, store=store)
            for order_intent in order_intents
        )
        return RiskBatchResult(decisions)

    def check_order_intent(
        self,
        order_intent: OrderIntent | Mapping[str, Any],
        context: RiskContext,
        *,
        store: SQLiteStore | None = None,
    ) -> RiskGateDecision:
        """Evaluate one intent and persist its ``RiskCheck`` when a store is given."""
        now = context.now()
        try:
            intent = _coerce_order_intent(order_intent)
        except (ValidationError, ValueError, TypeError) as exc:
            risk_check = self._blocked_invalid_intent(order_intent, now, exc)
            _persist_risk_check(store, risk_check)
            return RiskGateDecision(order_intent=None, risk_check=risk_check)

        risk_check = self._evaluate_valid_intent(intent, context, now)
        _persist_risk_check(store, risk_check)
        approved_order = None
        if risk_check.passed:
            approval_id = str(risk_check.details["approval_id"])
            approved_order = RiskApprovedOrder(
                order_intent=intent,
                risk_check=risk_check,
                approval_id=approval_id,
                approved_at_utc=risk_check.checked_at_utc,
            )
        return RiskGateDecision(
            order_intent=intent,
            risk_check=risk_check,
            approved_order=approved_order,
        )

    def current_metrics(self, context: RiskContext) -> PortfolioRiskMetrics | None:
        account = context.account_state()
        if account is None or account.equity <= 0:
            return None
        current_notionals, _ = _current_symbol_notionals(context, account)
        return _portfolio_metrics(
            current_notionals,
            account=account,
            limits=self.limits,
            use_account_gross_floor=True,
        )

    def summarize_context(self, context: RiskContext) -> dict[str, Any]:
        """Return a JSON-safe read-only account/risk state summary."""
        account = context.account_state()
        metrics = self.current_metrics(context)
        now = context.now()
        market_summary: dict[str, Any] = {}
        for symbol in normalize_symbols(context.market.keys()) if context.market else ():
            market = _market_for(context.market, symbol)
            if market is None:
                continue
            market_summary[symbol] = {
                "observed_at_utc": market.observed_at_utc.isoformat(),
                "age_seconds": max(0.0, (now - market.observed_at_utc).total_seconds()),
                "bid": market.bid,
                "ask": market.ask,
                "last": market.last,
                "spread": market.spread,
                "spread_bps": market.spread_bps,
                "spread_cap_bps": SPREAD_CAP_BPS[symbol],
            }
        return {
            "observed_at_utc": now.isoformat(),
            "kill_switch_active": bool(context.kill_switch_active),
            "limits": _json_safe(self.limits.__dict__),
            "account": None
            if account is None
            else {
                "balance": account.balance,
                "equity": account.equity,
                "margin": account.margin,
                "margin_free": account.margin_free,
                "margin_level": account.margin_level,
                "gross_leverage": account.gross_leverage,
                "max_drawdown": account.max_drawdown,
                "leverage": account.leverage,
                "observed_at_utc": account.observed_at_utc.isoformat()
                if account.observed_at_utc
                else None,
            },
            "portfolio_metrics": None if metrics is None else metrics.as_dict(),
            "market": market_summary,
        }

    def _blocked_invalid_intent(
        self,
        order_intent: OrderIntent | Mapping[str, Any],
        now: datetime,
        exc: Exception,
    ) -> RiskCheck:
        raw = dict(order_intent) if isinstance(order_intent, Mapping) else {}
        symbol: str | None = None
        try:
            if raw.get("symbol"):
                symbol = normalize_symbol(raw["symbol"])
        except (TypeError, ValueError):
            symbol = None
        reason = f"block: invalid order intent: {exc}"
        return RiskCheck(
            check_id=_new_check_id(raw.get("client_order_id", "invalid")),
            signal_id=raw.get("signal_id"),
            checked_at_utc=now,
            passed=False,
            symbol=symbol,
            reason=reason,
            details={"reasons": [reason], "raw_order_intent": _json_safe(raw)},
        )

    def _evaluate_valid_intent(
        self,
        intent: OrderIntent,
        context: RiskContext,
        now: datetime,
    ) -> RiskCheck:
        reasons: list[str] = []
        details: dict[str, Any] = {
            "client_order_id": intent.client_order_id,
            "reasons": reasons,
            "checks": {},
        }
        account = context.account_state()
        symbol = normalize_symbol(intent.symbol)
        metadata = _metadata_for(context.symbol_metadata, symbol)
        market = _market_for(context.market, symbol)

        if symbol not in ALLOWED_SYMBOLS:
            reasons.append("block: symbol is not in the allowed crypto set")
        if account is None:
            reasons.append("block: account state unavailable")
        elif account.equity <= 0:
            reasons.append("block: account equity is unavailable or non-positive")
        if metadata is None:
            reasons.append("block: symbol metadata unavailable")
        else:
            reasons.extend(_metadata_reasons(metadata))
            reasons.extend(_volume_reasons(intent.requested_volume, metadata))
        if market is None:
            reasons.append("block: latest tick unavailable")
        else:
            reasons.extend(_market_reasons(market, symbol, now, self.limits))
        reasons.extend(_feature_freshness_reasons(intent, now, self.limits))

        order_price = _order_price(intent, market)
        if order_price is None or order_price <= 0:
            reasons.append("block: order price unavailable")

        current_metrics: PortfolioRiskMetrics | None = None
        projected_metrics: PortfolioRiskMetrics | None = None
        risk_reducing = False
        if account is not None and account.equity > 0:
            current_notionals, position_reasons = _current_symbol_notionals(context, account)
            reasons.extend(position_reasons)
            current_metrics = _portfolio_metrics(
                current_notionals,
                account=account,
                limits=self.limits,
                use_account_gross_floor=True,
            )
            projected_notionals = dict(current_notionals)
            if metadata is not None and order_price is not None and order_price > 0:
                order_notional = _order_signed_notional(intent, metadata, order_price)
                projected_notionals[symbol] = projected_notionals.get(symbol, 0.0) + order_notional
            projected_metrics = _portfolio_metrics(
                projected_notionals,
                account=account,
                limits=self.limits,
                use_account_gross_floor=False,
            )
            risk_reducing = _is_risk_reducing(
                symbol,
                current=current_metrics,
                projected=projected_metrics,
            )
            details["current_metrics"] = current_metrics.as_dict()
            details["projected_metrics"] = projected_metrics.as_dict()
            details["risk_reducing"] = risk_reducing

        if context.kill_switch_active and not risk_reducing:
            reasons.append("block: kill switch active")

        if account is not None and account.max_drawdown >= self.limits.hard_drawdown and not risk_reducing:
            reasons.append("block: hard drawdown guard")
        elif (
            account is not None
            and account.max_drawdown >= self.limits.no_new_risk_drawdown
            and not risk_reducing
        ):
            reasons.append("block: no-new-risk drawdown guard")

        if projected_metrics is not None:
            reasons.extend(_portfolio_cap_reasons(projected_metrics, symbol, self.limits, risk_reducing))

        if metadata is not None and market is not None and order_price is not None:
            reasons.extend(_stop_distance_reasons(intent, metadata, market, order_price, risk_reducing))
            reasons.extend(_spread_cap_reasons(market, symbol, risk_reducing))

        approval_id = _approval_id(intent.client_order_id)
        passed = len(reasons) == 0
        if passed:
            details["approval_id"] = approval_id
            reason = "passed: risk checks approved order intent"
        else:
            reason = "; ".join(dict.fromkeys(reasons))

        return RiskCheck(
            check_id=_new_check_id(intent.client_order_id),
            signal_id=intent.signal_id,
            checked_at_utc=now,
            passed=passed,
            symbol=symbol,
            equity=account.equity if account is not None else None,
            balance=account.balance if account is not None else None,
            margin=account.margin if account is not None else None,
            margin_usage=projected_metrics.margin_usage if projected_metrics is not None else None,
            gross_leverage=projected_metrics.gross_leverage if projected_metrics is not None else None,
            net_directional_exposure=projected_metrics.net_directional_exposure
            if projected_metrics is not None
            else None,
            single_instrument_exposure=projected_metrics.single_instrument_exposure
            if projected_metrics is not None
            else None,
            max_drawdown=account.max_drawdown if account is not None else None,
            reason=reason,
            details=details,
        )


def load_risk_context_from_store(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    now_utc: datetime | None = None,
    kill_switch_file: str | Path | None = Path("config/KILL_SWITCH"),
) -> RiskContext:
    """Build current risk state from the local SQLite audit store."""
    symbols = normalize_symbols(target_symbols)
    with SQLiteStore(database_url) as store:
        account = _latest_account_from_store(store)
        metadata = _metadata_from_store(store, symbols)
        positions = _latest_positions_from_store(store, symbols)
        market = _latest_market_from_store(store, symbols)
    return RiskContext(
        account=account,
        symbol_metadata=metadata,
        positions=positions,
        market=market,
        now_utc=now_utc,
        kill_switch_active=read_kill_switch(kill_switch_file),
    )


def read_kill_switch(path: str | Path | None = Path("config/KILL_SWITCH")) -> bool:
    """Return true when env or local file kill switch is enabled."""
    env_value = os.environ.get("KILL_SWITCH") or os.environ.get("BOT_KILL_SWITCH")
    if _truthy(env_value):
        return True
    if path is None:
        return False
    kill_path = Path(path)
    if not kill_path.exists():
        return False
    try:
        content = kill_path.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return content == "" or _truthy(content)


def _latest_account_from_store(store: SQLiteStore) -> AccountRiskState | None:
    row = store.fetch_one(
        """
        SELECT observed_at_utc, balance, equity, profit, margin, margin_free,
               margin_level, gross_leverage, max_drawdown, currency, raw_json
        FROM account_snapshots
        ORDER BY observed_at_utc DESC
        LIMIT 1
        """
    )
    if row is None:
        return None
    raw = _loads_json(row["raw_json"])
    return AccountRiskState.from_snapshot(
        {
            "observed_at_utc": row["observed_at_utc"],
            "balance": row["balance"],
            "equity": row["equity"],
            "margin": row["margin"],
            "margin_free": row["margin_free"],
            "margin_level": row["margin_level"],
            "gross_leverage": row["gross_leverage"],
            "max_drawdown": row["max_drawdown"],
            "raw": raw,
        }
    )


def _metadata_from_store(store: SQLiteStore, symbols: tuple[str, ...]) -> dict[str, SymbolConfig]:
    placeholders = ",".join("?" for _ in symbols)
    rows = store.fetch_all(
        f"""
        SELECT symbol, broker_symbol, digits, point, trade_tick_size,
               trade_tick_value, trade_contract_size, volume_min, volume_max,
               volume_step, spread, filling_mode, trade_mode, raw_json
        FROM symbol_metadata
        WHERE symbol IN ({placeholders})
        """,
        symbols,
    )
    result: dict[str, SymbolConfig] = {}
    for row in rows:
        config = SymbolConfig(
            symbol=row["symbol"],
            broker_symbol=row["broker_symbol"],
            digits=row["digits"],
            point=row["point"],
            trade_tick_size=row["trade_tick_size"],
            trade_tick_value=row["trade_tick_value"],
            trade_contract_size=row["trade_contract_size"],
            volume_min=row["volume_min"],
            volume_max=row["volume_max"],
            volume_step=row["volume_step"],
            spread=row["spread"],
            filling_mode=row["filling_mode"],
            trade_mode=row["trade_mode"],
            raw=_loads_json(row["raw_json"]),
        )
        result[config.symbol] = config
    return result


def _latest_positions_from_store(
    store: SQLiteStore,
    symbols: tuple[str, ...],
) -> tuple[PositionRiskState, ...]:
    placeholders = ",".join("?" for _ in symbols)
    rows = store.fetch_all(
        f"""
        SELECT p.observed_at_utc, p.symbol, p.side, p.volume, p.price_open, p.price_current
        FROM positions_snapshots p
        JOIN (
          SELECT symbol, MAX(observed_at_utc) AS latest_at
          FROM positions_snapshots
          WHERE symbol IN ({placeholders})
          GROUP BY symbol
        ) latest
          ON p.symbol = latest.symbol AND p.observed_at_utc = latest.latest_at
        """,
        symbols,
    )
    return tuple(PositionRiskState.from_snapshot(dict(row)) for row in rows)


def _latest_market_from_store(
    store: SQLiteStore,
    symbols: tuple[str, ...],
) -> dict[str, MarketRiskState]:
    result: dict[str, MarketRiskState] = {}
    for symbol in symbols:
        row = store.fetch_one(
            """
            SELECT symbol, time_utc, bid, ask, last
            FROM ticks
            WHERE symbol = ?
            ORDER BY time_utc DESC, time_msc DESC
            LIMIT 1
            """,
            (symbol,),
        )
        if row is None:
            continue
        result[symbol] = MarketRiskState(
            symbol=symbol,
            observed_at_utc=_datetime_from_any(row["time_utc"]),
            bid=_as_optional_float(row["bid"]),
            ask=_as_optional_float(row["ask"]),
            last=_as_optional_float(row["last"]),
        )
    return result


def _coerce_order_intent(value: OrderIntent | Mapping[str, Any]) -> OrderIntent:
    if isinstance(value, OrderIntent):
        return value
    if isinstance(value, Mapping):
        return OrderIntent(**dict(value))
    raise TypeError(f"expected OrderIntent or mapping, got {type(value).__name__}")


def _metadata_for(
    metadata_map: Mapping[str, SymbolConfig | Mapping[str, Any]],
    symbol: str,
) -> SymbolConfig | None:
    raw = metadata_map.get(symbol)
    if raw is None:
        return None
    if isinstance(raw, SymbolConfig):
        return raw
    data = dict(raw)
    data.setdefault("symbol", symbol)
    return SymbolConfig(**data)


def _market_for(
    market_map: Mapping[str, MarketRiskState | Mapping[str, Any]],
    symbol: str,
) -> MarketRiskState | None:
    raw = market_map.get(symbol)
    if raw is None:
        return None
    if isinstance(raw, MarketRiskState):
        return raw
    data = dict(raw)
    data.setdefault("symbol", symbol)
    if "observed_at_utc" not in data and "time_utc" in data:
        data["observed_at_utc"] = data["time_utc"]
    return MarketRiskState(
        symbol=normalize_symbol(data["symbol"]),
        observed_at_utc=_datetime_from_any(data["observed_at_utc"]),
        bid=_as_optional_float(data.get("bid")),
        ask=_as_optional_float(data.get("ask")),
        last=_as_optional_float(data.get("last")),
    )


def _metadata_reasons(metadata: SymbolConfig) -> list[str]:
    reasons: list[str] = []
    required_fields = (
        "broker_symbol",
        "point",
        "trade_contract_size",
        "volume_min",
        "volume_max",
        "volume_step",
    )
    for field_name in required_fields:
        value = getattr(metadata, field_name)
        if value is None or value == "":
            reasons.append(f"block: missing metadata field {field_name}")
    return reasons


def _volume_reasons(volume: float, metadata: SymbolConfig) -> list[str]:
    reasons: list[str] = []
    min_volume = metadata.volume_min
    max_volume = metadata.volume_max
    step = metadata.volume_step
    if min_volume is not None and volume + EPSILON < min_volume:
        reasons.append("block: requested volume below broker minimum")
    if max_volume is not None and volume - EPSILON > max_volume:
        reasons.append("block: requested volume above broker maximum")
    if step is None or step <= 0:
        reasons.append("block: volume step metadata unavailable")
    else:
        units = volume / step
        if not math.isclose(units, round(units), rel_tol=0.0, abs_tol=1e-7):
            reasons.append("block: requested volume does not align to broker volume step")
    return reasons


def _market_reasons(
    market: MarketRiskState,
    symbol: str,
    now: datetime,
    limits: RiskLimits,
) -> list[str]:
    reasons: list[str] = []
    age_seconds = (now - market.observed_at_utc).total_seconds()
    if age_seconds < -limits.max_future_feature_skew_seconds:
        reasons.append("block: latest tick timestamp is in the future")
    if age_seconds > limits.tick_stale_seconds:
        reasons.append("block: latest tick is stale")
    if market.bid is None or market.ask is None or market.bid <= 0 or market.ask <= 0:
        reasons.append("block: current bid/ask unavailable")
    elif market.ask < market.bid:
        reasons.append("block: current ask is below bid")
    if market.spread_bps is None:
        reasons.append("block: spread bps unavailable")
    elif market.spread_bps > SPREAD_CAP_BPS[symbol] * 1.5:
        reasons.append("block: spread exceeds abnormal exit threshold")
    return reasons


def _spread_cap_reasons(
    market: MarketRiskState,
    symbol: str,
    risk_reducing: bool,
) -> list[str]:
    spread_bps = market.spread_bps
    if spread_bps is None:
        return []
    if spread_bps > SPREAD_CAP_BPS[symbol] and not risk_reducing:
        return ["block: spread exceeds entry cap"]
    return []


def _feature_freshness_reasons(
    intent: OrderIntent,
    now: datetime,
    limits: RiskLimits,
) -> list[str]:
    raw_time = intent.metadata.get("feature_time_utc")
    if raw_time is None:
        return ["block: strategy feature timestamp unavailable"]
    try:
        feature_time = _datetime_from_any(raw_time)
    except (TypeError, ValueError) as exc:
        return [f"block: invalid strategy feature timestamp: {exc}"]
    age_seconds = (now - feature_time).total_seconds()
    if age_seconds < -limits.max_future_feature_skew_seconds:
        return ["block: strategy feature timestamp is in the future"]
    if age_seconds > limits.feature_stale_seconds:
        return ["block: strategy feature timestamp is stale"]
    return []


def _current_symbol_notionals(
    context: RiskContext,
    account: AccountRiskState,
) -> tuple[dict[str, float], list[str]]:
    notionals = {symbol: 0.0 for symbol in ALLOWED_SYMBOLS}
    reasons: list[str] = []
    for raw_position in context.positions:
        position = (
            raw_position
            if isinstance(raw_position, PositionRiskState)
            else PositionRiskState.from_snapshot(raw_position)
        )
        symbol = normalize_symbol(position.symbol)
        if position.volume <= 0:
            continue
        metadata = _metadata_for(context.symbol_metadata, symbol)
        if metadata is None or metadata.trade_contract_size is None:
            reasons.append(f"block: missing metadata for open position {symbol}")
            continue
        price = position.price_current or position.price_open
        if price is None or price <= 0:
            market = _market_for(context.market, symbol)
            price = market.mid if market is not None else None
        if price is None or price <= 0:
            reasons.append(f"block: missing price for open position {symbol}")
            continue
        side = str(getattr(position.side, "value", position.side)).lower()
        sign = 1.0 if side == PositionSide.LONG.value else -1.0 if side == PositionSide.SHORT.value else 0.0
        notionals[symbol] = notionals.get(symbol, 0.0) + sign * position.volume * price * metadata.trade_contract_size
    if account.gross_leverage > 0 and not any(abs(value) > EPSILON for value in notionals.values()):
        reasons.append("block: account reports exposure but no position snapshots are available")
    return notionals, reasons


def _portfolio_metrics(
    symbol_notionals: Mapping[str, float],
    *,
    account: AccountRiskState,
    limits: RiskLimits,
    use_account_gross_floor: bool,
) -> PortfolioRiskMetrics:
    equity = account.equity
    gross_notional = sum(abs(float(value)) for value in symbol_notionals.values())
    net_notional = sum(float(value) for value in symbol_notionals.values())
    gross_leverage = gross_notional / equity if equity > 0 else math.inf
    if use_account_gross_floor and account.gross_leverage > gross_leverage:
        gross_leverage = account.gross_leverage
        gross_notional = gross_leverage * equity
    net_share = abs(net_notional) / gross_notional if gross_notional > EPSILON else 0.0
    single_share = (
        max(abs(float(value)) for value in symbol_notionals.values()) / gross_notional
        if gross_notional > EPSILON
        else 0.0
    )
    leverage = max(min(account.leverage, limits.account_leverage_cap), 1.0)
    margin_usage_from_leverage = gross_leverage / leverage
    actual_margin_usage = account.margin / equity if equity > 0 else math.inf
    symbol_leverage = {
        symbol: abs(float(notional)) / equity if equity > 0 else math.inf
        for symbol, notional in symbol_notionals.items()
    }
    return PortfolioRiskMetrics(
        equity=equity,
        gross_notional=gross_notional,
        net_notional=net_notional,
        gross_leverage=gross_leverage,
        net_directional_exposure=net_share,
        single_instrument_exposure=single_share,
        margin_usage=max(actual_margin_usage, margin_usage_from_leverage),
        symbol_leverage=symbol_leverage,
        symbol_notionals=dict(symbol_notionals),
    )


def _order_signed_notional(intent: OrderIntent, metadata: SymbolConfig, price: float) -> float:
    contract_size = metadata.trade_contract_size
    if contract_size is None or contract_size <= 0:
        return 0.0
    sign = 1.0 if intent.side == OrderSide.BUY.value else -1.0
    return sign * intent.requested_volume * price * contract_size


def _is_risk_reducing(
    symbol: str,
    *,
    current: PortfolioRiskMetrics,
    projected: PortfolioRiskMetrics,
) -> bool:
    current_symbol = current.symbol_notionals.get(symbol, 0.0)
    projected_symbol = projected.symbol_notionals.get(symbol, 0.0)
    reverses_direction = current_symbol * projected_symbol < -EPSILON
    return (
        projected.gross_leverage <= current.gross_leverage + EPSILON
        and abs(projected_symbol) <= abs(current_symbol) + EPSILON
        and not reverses_direction
    )


def _portfolio_cap_reasons(
    metrics: PortfolioRiskMetrics,
    symbol: str,
    limits: RiskLimits,
    risk_reducing: bool,
) -> list[str]:
    reasons: list[str] = []
    if risk_reducing:
        return reasons
    if metrics.gross_leverage > limits.max_gross_leverage + EPSILON:
        reasons.append("block: projected gross leverage exceeds internal cap")
    symbol_cap = min(HARD_SYMBOL_LEVERAGE_CAP[symbol], limits.max_symbol_leverage)
    if metrics.symbol_leverage.get(symbol, 0.0) > symbol_cap + EPSILON:
        reasons.append("block: projected per-symbol leverage exceeds internal cap")
    if metrics.margin_usage > limits.max_margin_usage + EPSILON:
        reasons.append("block: projected margin usage exceeds internal cap")
    if (
        metrics.gross_leverage >= limits.concentration_min_gross_leverage
        and metrics.single_instrument_exposure > limits.max_single_instrument_share + EPSILON
    ):
        reasons.append("block: projected single-instrument exposure exceeds internal cap")
    if (
        metrics.gross_leverage >= limits.concentration_min_gross_leverage
        and metrics.net_directional_exposure > limits.max_net_directional_share + EPSILON
    ):
        reasons.append("block: projected net directional exposure exceeds internal cap")
    return reasons


def _stop_distance_reasons(
    intent: OrderIntent,
    metadata: SymbolConfig,
    market: MarketRiskState,
    order_price: float,
    risk_reducing: bool,
) -> list[str]:
    if risk_reducing:
        return []
    if intent.stop_loss is None:
        return ["block: stop loss is required for new risk"]
    spread = market.spread
    point = metadata.point
    if spread is None or point is None:
        return ["block: stop-distance inputs unavailable"]
    stop_distance = abs(order_price - intent.stop_loss)
    min_stop_distance = max(3.0 * spread, 5.0 * point)
    if stop_distance + EPSILON < min_stop_distance:
        return ["block: stop distance below minimum"]
    return []


def _order_price(intent: OrderIntent, market: MarketRiskState | None) -> float | None:
    if intent.requested_price is not None:
        return float(intent.requested_price)
    if market is None:
        return None
    if intent.side == OrderSide.BUY.value:
        return market.ask or market.last or market.mid
    return market.bid or market.last or market.mid


def _persist_risk_check(store: SQLiteStore | None, risk_check: RiskCheck) -> None:
    if store is not None:
        store.insert_risk_check(risk_check)


def _new_check_id(client_order_id: Any) -> str:
    safe_order_id = str(client_order_id or "unknown").replace("/", "")
    return f"risk-{safe_order_id}-{_utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}"


def _approval_id(client_order_id: str) -> str:
    return f"risk-approved-{client_order_id}-{uuid4().hex[:8]}"


def _model_or_mapping(value: Any) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return dict(value)
    raise RiskEngineError(f"expected pydantic model or mapping, got {type(value).__name__}")


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
    raise ValueError(f"cannot parse datetime value: {value!r}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    if not math.isfinite(number):
        return None
    return number


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enabled", "block"}


def _loads_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        return [_json_safe(item) for item in value]
    return str(value)


__all__ = [
    "HARD_SYMBOL_LEVERAGE_CAP",
    "RULE_MAX_ACCOUNT_LEVERAGE",
    "SPREAD_CAP_BPS",
    "AccountRiskState",
    "MarketRiskState",
    "PortfolioRiskMetrics",
    "PositionRiskState",
    "RiskApprovedOrder",
    "RiskBatchResult",
    "RiskContext",
    "RiskEngine",
    "RiskEngineError",
    "RiskGateDecision",
    "RiskLimits",
    "load_risk_context_from_store",
    "read_kill_switch",
]
