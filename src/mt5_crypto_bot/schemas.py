"""Pydantic schemas for the dry-run-first MT5 crypto bot."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_STRATEGY_VERSION


AllowedSymbol = Literal["BAR/USD", "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"]


class StrictBaseModel(BaseModel):
    """Base model with predictable validation and serialization behavior."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=True)


class TradeMode(str, Enum):
    """Execution modes currently allowed by unattended, non-live automation."""

    DRY_RUN = "dry_run"
    PAPER = "paper"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    DAY = "day"


class SignalDecision(str, Enum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"
    BLOCK = "block"


class ExecutionStatus(str, Enum):
    DRY_RUN = "dry_run"
    CHECKED = "checked"
    REJECTED = "rejected"
    FILLED = "filled"
    PARTIAL = "partial"
    FAILED = "failed"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class FillSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


def normalize_symbol(value: Any) -> str:
    """Normalize and validate a canonical trading symbol."""
    if not isinstance(value, str):
        raise TypeError("symbol must be a string")
    symbol = value.strip().upper()
    if symbol not in ALLOWED_SYMBOLS:
        allowed = ", ".join(ALLOWED_SYMBOLS)
        raise ValueError(f"symbol {symbol!r} is not allowed; expected one of: {allowed}")
    return symbol


def normalize_symbols(value: Any) -> tuple[str, ...]:
    """Parse and validate a target-symbol collection from env or Python values."""
    if value is None or value == "":
        return ALLOWED_SYMBOLS
    if isinstance(value, str):
        raw_symbols = [part.strip() for part in value.split(",")]
    else:
        raw_symbols = list(value)

    symbols = tuple(normalize_symbol(symbol) for symbol in raw_symbols if str(symbol).strip())
    if not symbols:
        raise ValueError("target_symbols must include at least one allowed symbol")
    return symbols


class SymbolConfig(StrictBaseModel):
    """Canonical-to-broker symbol configuration and broker metadata."""

    symbol: str
    broker_symbol: str | None = None
    enabled: bool = True
    description: str | None = None
    digits: int | None = Field(default=None, ge=0)
    point: float | None = Field(default=None, gt=0)
    trade_tick_size: float | None = Field(default=None, gt=0)
    trade_tick_value: float | None = None
    trade_contract_size: float | None = Field(default=None, gt=0)
    volume_min: float | None = Field(default=None, ge=0)
    volume_max: float | None = Field(default=None, ge=0)
    volume_step: float | None = Field(default=None, gt=0)
    spread: float | None = Field(default=None, ge=0)
    trade_mode: int | None = None
    filling_mode: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)

    @model_validator(mode="after")
    def validate_volume_bounds(self) -> SymbolConfig:
        if (
            self.volume_min is not None
            and self.volume_max is not None
            and self.volume_min > self.volume_max
        ):
            raise ValueError("volume_min cannot exceed volume_max")
        return self


class StrategyParams(StrictBaseModel):
    """Research-backed starting parameters for the MVP strategy."""

    strategy_version: str = DEFAULT_STRATEGY_VERSION
    signal_timeframe: str = "M5"
    entry_threshold: float = Field(default=1.25, gt=0)
    exit_threshold: float = Field(default=0.35, ge=0)
    ema_fast: int = Field(default=20, gt=0)
    ema_slow: int = Field(default=80, gt=0)
    atr_period: int = Field(default=14, gt=0)
    atr_stop_multiple: float = Field(default=1.6, gt=0)
    take_profit_multiple: float = Field(default=2.4, gt=0)
    trailing_stop_multiple: float = Field(default=1.2, gt=0)
    time_stop_minutes: int = Field(default=180, gt=0)
    max_spread_bps_btc_eth: float = Field(default=8.0, gt=0)
    max_spread_bps_sol_xrp_bar: float = Field(default=25.0, gt=0)
    risk_per_trade: float = Field(default=0.004, gt=0, le=0.01)
    max_gross_leverage: float = Field(default=8.0, gt=0, le=12.0)
    max_symbol_leverage: float = Field(default=2.0, gt=0, le=8.0)
    max_margin_usage: float = Field(default=0.60, gt=0, lt=0.90)
    daily_drawdown_stop: float = Field(default=0.06, gt=0, lt=0.50)
    total_drawdown_stop: float = Field(default=0.10, gt=0, lt=0.50)

    @model_validator(mode="after")
    def validate_thresholds(self) -> StrategyParams:
        if self.exit_threshold >= self.entry_threshold:
            raise ValueError("exit_threshold must be lower than entry_threshold")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be lower than ema_slow")
        return self


class Signal(StrictBaseModel):
    """Strategy output for a completed signal cycle."""

    signal_id: str
    created_at_utc: datetime
    strategy_version: str
    symbol: str
    timeframe: str
    direction: Direction
    score: float
    target_leverage: float = Field(ge=0)
    target_volume: float | None = Field(default=None, ge=0)
    target_price: float | None = Field(default=None, gt=0)
    features: dict[str, Any] = Field(default_factory=dict)
    decision: SignalDecision
    reason: str | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)


class RiskCheck(StrictBaseModel):
    """Pre-trade or portfolio-level risk decision."""

    check_id: str | None = None
    signal_id: str | None = None
    checked_at_utc: datetime
    passed: bool
    symbol: str | None = None
    equity: float | None = Field(default=None, ge=0)
    balance: float | None = None
    margin: float | None = Field(default=None, ge=0)
    margin_usage: float | None = Field(default=None, ge=0)
    gross_leverage: float | None = Field(default=None, ge=0)
    net_directional_exposure: float | None = Field(default=None, ge=0)
    single_instrument_exposure: float | None = Field(default=None, ge=0)
    max_drawdown: float | None = Field(default=None, ge=0)
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_optional_symbol(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        return normalize_symbol(value)


class OrderIntent(StrictBaseModel):
    """Validated order intent produced after strategy and risk checks."""

    client_order_id: str
    signal_id: str | None = None
    created_at_utc: datetime
    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    requested_volume: float = Field(gt=0)
    requested_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    deviation_points: int = Field(default=20, ge=0)
    time_in_force: TimeInForce = TimeInForce.IOC
    strategy_version: str = DEFAULT_STRATEGY_VERSION
    magic: int = Field(default=20260621, ge=0)
    comment: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)


class ExecutionResult(StrictBaseModel):
    """Result of dry-run execution or future guarded broker execution."""

    client_order_id: str
    executed_at_utc: datetime
    trade_mode: TradeMode = TradeMode.DRY_RUN
    status: ExecutionStatus
    symbol: str
    requested_volume: float = Field(gt=0)
    filled_volume: float = Field(default=0.0, ge=0)
    requested_price: float | None = Field(default=None, gt=0)
    average_fill_price: float | None = Field(default=None, gt=0)
    mt5_order_ticket: int | None = None
    mt5_deal_ticket: int | None = None
    retcode: int | None = None
    message: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)


class AccountSnapshot(StrictBaseModel):
    """Sanitized account state snapshot."""

    observed_at_utc: datetime
    balance: float = Field(ge=0)
    equity: float = Field(ge=0)
    profit: float = 0.0
    margin: float = Field(default=0.0, ge=0)
    margin_free: float | None = None
    margin_level: float | None = None
    gross_leverage: float = Field(default=0.0, ge=0)
    max_drawdown: float = Field(default=0.0, ge=0)
    sharpe_15m: float | None = None
    currency: str = "USD"
    raw: dict[str, Any] = Field(default_factory=dict)


class PositionSnapshot(StrictBaseModel):
    """Open-position snapshot from MT5 or dry-run state."""

    observed_at_utc: datetime
    symbol: str
    ticket: int | None = None
    side: PositionSide
    volume: float = Field(ge=0)
    price_open: float | None = Field(default=None, gt=0)
    price_current: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    profit: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)


class Fill(StrictBaseModel):
    """Executed deal/fill record."""

    deal_ticket: int | None = None
    order_ticket: int | None = None
    position_id: int | None = None
    symbol: str
    filled_at_utc: datetime
    side: FillSide
    volume: float = Field(gt=0)
    price: float = Field(gt=0)
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    slippage_bps: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)
