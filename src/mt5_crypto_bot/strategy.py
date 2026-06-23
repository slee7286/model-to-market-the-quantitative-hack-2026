"""Dry-run strategy engine for the frozen ``momo_v1`` FX/crypto strategy.

The strategy engine is intentionally independent of MT5. It consumes completed
feature snapshots, emits auditable :class:`Signal` objects, and may build
``OrderIntent`` objects for later risk/execution phases. It never sends orders.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mt5_crypto_bot.constants import (
    ALLOWED_SYMBOLS,
    BROKER_STOP_DISTANCE_BUFFER_POINTS,
    CRYPTO_SYMBOLS,
    DEFAULT_DATABASE_URL,
    DEFAULT_STRATEGY_VERSION,
    DISCIPLINE_BALLAST_MAIN_SHARE,
    DISCIPLINE_BALLAST_MIN_TRIGGER_LEVERAGE,
    FOREX_SYMBOLS,
    MAX_ORDER_INTENT_CHUNKS_PER_SIGNAL,
    PNL_SPRINT_ENTRY_SYMBOLS,
)
from mt5_crypto_bot.features import (
    FeatureConfig,
    FeatureEngineeringError,
    compute_feature_snapshots_from_store,
    latest_feature_snapshots,
)
from mt5_crypto_bot.schemas import (
    Direction,
    OrderIntent,
    OrderSide,
    Signal,
    SignalDecision,
    StrategyParams,
    SymbolConfig,
    normalize_symbol,
    normalize_symbols,
)
from mt5_crypto_bot.storage import SQLiteStore


INITIAL_EQUITY = 1_000_000.0
EPSILON = 1e-12

SPREAD_CAP_BPS: dict[str, float] = {
    "AUD/USD": 3.0,
    "BAR/USD": 25.0,
    "BTC/USD": 8.0,
    "EUR/CHF": 4.0,
    "EUR/GBP": 3.0,
    "EUR/USD": 2.0,
    "ETH/USD": 8.0,
    "GBP/USD": 3.0,
    "SOL/USD": 15.0,
    "USD/CAD": 3.0,
    "USD/CHF": 3.0,
    "USD/JPY": 2.0,
    "XRP/USD": 15.0,
}

ABNORMAL_SPREAD_EXIT_MULTIPLE = 1.5

NORMAL_SYMBOL_LEVERAGE_CAP: dict[str, float] = {
    **{symbol: 28.00 for symbol in FOREX_SYMBOLS},
    "BAR/USD": 27.00,
    "BTC/USD": 27.00,
    "ETH/USD": 27.00,
    "SOL/USD": 27.00,
    "XRP/USD": 27.00,
}

HARD_SYMBOL_LEVERAGE_CAP: dict[str, float] = {
    **{symbol: 28.00 for symbol in FOREX_SYMBOLS},
    "BAR/USD": 27.00,
    "BTC/USD": 27.00,
    "ETH/USD": 27.00,
    "SOL/USD": 27.00,
    "XRP/USD": 27.00,
}

TARGET_RV_1H: dict[str, float] = {
    "AUD/USD": 0.0012,
    "BAR/USD": 0.0070,
    "BTC/USD": 0.0080,
    "EUR/CHF": 0.0007,
    "EUR/GBP": 0.0008,
    "EUR/USD": 0.0009,
    "ETH/USD": 0.0085,
    "GBP/USD": 0.0011,
    "SOL/USD": 0.0110,
    "USD/CAD": 0.0009,
    "USD/CHF": 0.0009,
    "USD/JPY": 0.0010,
    "XRP/USD": 0.0100,
}

VOLATILITY_FLOOR: dict[str, float] = {
    "AUD/USD": 0.00035,
    "BAR/USD": 0.0030,
    "BTC/USD": 0.0025,
    "EUR/CHF": 0.00025,
    "EUR/GBP": 0.00025,
    "EUR/USD": 0.00030,
    "ETH/USD": 0.0025,
    "GBP/USD": 0.00035,
    "SOL/USD": 0.0035,
    "USD/CAD": 0.00030,
    "USD/CHF": 0.00030,
    "USD/JPY": 0.00030,
    "XRP/USD": 0.0035,
}

REQUIRED_METADATA_FIELDS: tuple[str, ...] = (
    "broker_symbol",
    "digits",
    "point",
    "trade_contract_size",
    "volume_min",
    "volume_max",
    "volume_step",
    "trade_mode",
    "filling_mode",
)


class StrategyEngineError(RuntimeError):
    """Raised when the dry-run strategy engine cannot evaluate safely."""


@dataclass(frozen=True)
class StrategyContext:
    """Runtime state needed to convert feature snapshots into targets."""

    symbol_metadata: Mapping[str, SymbolConfig | Mapping[str, Any]] = field(
        default_factory=dict
    )
    equity: float = INITIAL_EQUITY
    current_position_leverage: Mapping[str, float] = field(default_factory=dict)
    current_position_volume: Mapping[str, float] = field(default_factory=dict)
    max_drawdown: float = 0.0
    now_utc: datetime | None = None
    enforce_freshness: bool = True


@dataclass(frozen=True)
class StrategyCycleResult:
    """Signals and order intents produced by one dry-run strategy cycle."""

    signals: tuple[Signal, ...]
    order_intents: tuple[OrderIntent, ...]
    feature_rows: int
    persisted_signals: int = 0

    @property
    def entered(self) -> int:
        return sum(1 for signal in self.signals if signal.decision == SignalDecision.ENTER)

    @property
    def exited(self) -> int:
        return sum(1 for signal in self.signals if signal.decision == SignalDecision.EXIT)

    @property
    def held(self) -> int:
        return sum(1 for signal in self.signals if signal.decision == SignalDecision.HOLD)

    @property
    def blocked(self) -> int:
        return sum(1 for signal in self.signals if signal.decision == SignalDecision.BLOCK)

    def summary(self) -> dict[str, Any]:
        return {
            "feature_rows": self.feature_rows,
            "signals": len(self.signals),
            "persisted_signals": self.persisted_signals,
            "order_intents": len(self.order_intents),
            "entered": self.entered,
            "exited": self.exited,
            "held": self.held,
            "blocked": self.blocked,
        }


class DryRunStrategyEngine:
    """Convert M5 feature snapshots into dry-run signals and order intents."""

    def __init__(
        self,
        params: StrategyParams | None = None,
        *,
        strategy_version: str | None = None,
        magic: int = 20260621,
    ) -> None:
        self.params = params or StrategyParams(
            strategy_version=strategy_version or DEFAULT_STRATEGY_VERSION
        )
        if strategy_version is not None and self.params.strategy_version != strategy_version:
            self.params = self.params.model_copy(update={"strategy_version": strategy_version})
        self.magic = magic

    def generate_signals(
        self,
        features: pd.DataFrame | Iterable[Mapping[str, Any]],
        *,
        context: StrategyContext | None = None,
        store: SQLiteStore | None = None,
        latest_only: bool = True,
    ) -> StrategyCycleResult:
        """Evaluate feature rows and optionally persist every generated signal."""
        frame = _features_to_frame(features)
        if latest_only and not frame.empty:
            frame = latest_feature_snapshots(frame)
        if frame.empty:
            return StrategyCycleResult((), (), 0, 0)

        strategy_context = context or StrategyContext()
        signals: list[Signal] = []
        order_intents: list[OrderIntent] = []
        rows: list[dict[str, Any]] = []
        for _, row in frame.sort_values(["feature_time_utc", "symbol"]).iterrows():
            row_data = _row_mapping(row)
            rows.append(row_data)
            signal, intent = self.evaluate_row(row_data, strategy_context)
            signals.append(signal)
            if intent is not None:
                order_intents.append(intent)

        signals, order_intents = self._apply_discipline_ballast(
            rows=rows,
            signals=signals,
            order_intents=order_intents,
            context=strategy_context,
        )
        order_intents = list(
            chunk_order_intents_for_broker_limits(
                order_intents,
                strategy_context.symbol_metadata,
            )
        )

        persisted = 0
        if store is not None:
            for signal in signals:
                store.upsert_signal(signal)
                persisted += 1
        return StrategyCycleResult(
            signals=tuple(signals),
            order_intents=tuple(order_intents),
            feature_rows=len(frame),
            persisted_signals=persisted,
        )

    def evaluate_row(
        self,
        row: Mapping[str, Any] | pd.Series,
        context: StrategyContext | None = None,
    ) -> tuple[Signal, OrderIntent | None]:
        """Evaluate one feature snapshot row."""
        strategy_context = context or StrategyContext()
        data = _row_mapping(row)
        symbol = normalize_symbol(data.get("symbol"))
        feature_time = _datetime_from_any(data.get("feature_time_utc"))
        now = _context_now(strategy_context)
        score = compute_momo_score(data)
        metadata = _metadata_for(strategy_context.symbol_metadata, symbol)
        current_leverage = float(strategy_context.current_position_leverage.get(symbol, 0.0))
        current_volume = float(strategy_context.current_position_volume.get(symbol, 0.0))
        feature_payload = _feature_payload(
            data,
            score,
            entry_threshold=self.params.entry_threshold,
            exit_threshold=self.params.exit_threshold,
        )

        block_reason = _activation_block_reason(
            data,
            symbol=symbol,
            metadata=metadata,
            now_utc=now,
            feature_time=feature_time,
            context=strategy_context,
        )
        if block_reason is not None:
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=Direction.FLAT,
                score=score,
                target_leverage=0.0,
                target_volume=None,
                target_price=None,
                features=feature_payload,
                decision=SignalDecision.BLOCK,
                reason=block_reason,
            )
            return signal, None

        exit_reason = _exit_reason(
            data, score, current_leverage, exit_threshold=self.params.exit_threshold
        )
        if exit_reason is not None:
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=Direction.FLAT,
                score=score,
                target_leverage=0.0,
                target_volume=0.0,
                target_price=_exit_price(data, current_leverage),
                features=feature_payload,
                decision=SignalDecision.EXIT,
                reason=exit_reason,
            )
            intent = self._order_intent(
                signal=signal,
                row=data,
                metadata=metadata,
                side=OrderSide.SELL if current_leverage > 0 else OrderSide.BUY,
                requested_volume=_exit_volume(
                    symbol,
                    row=data,
                    metadata=metadata,
                    current_leverage=abs(current_leverage),
                    current_volume=current_volume,
                    equity=strategy_context.equity,
                ),
                stop_loss=None,
                take_profit=None,
            )
            return signal, intent

        candidate_side = _candidate_side(score, self.params.entry_threshold)
        if candidate_side is None:
            held_direction, held_leverage = _held_position_target(current_leverage)
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=held_direction,
                score=score,
                target_leverage=held_leverage,
                target_volume=current_volume if current_volume > 0 else None,
                target_price=None,
                features=feature_payload,
                decision=SignalDecision.HOLD,
                reason="hold: score inside entry thresholds",
            )
            return signal, None

        if symbol not in PNL_SPRINT_ENTRY_SYMBOLS:
            if abs(current_leverage) > EPSILON:
                held_direction, held_leverage = _held_position_target(current_leverage)
                signal = self._signal(
                    symbol=symbol,
                    now_utc=now,
                    feature_time=feature_time,
                    direction=held_direction,
                    score=score,
                    target_leverage=held_leverage,
                    target_volume=current_volume if current_volume > 0 else None,
                    target_price=None,
                    features=feature_payload,
                    decision=SignalDecision.HOLD,
                    reason="hold: PnL sprint blocks adding exposure for this symbol",
                )
                return signal, None
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=Direction.FLAT,
                score=score,
                target_leverage=0.0,
                target_volume=None,
                target_price=None,
                features=feature_payload,
                decision=SignalDecision.BLOCK,
                reason="block: PnL sprint disables new entries for this symbol",
            )
            return signal, None

        entry_block = _entry_block_reason(data, symbol, candidate_side)
        if entry_block is not None:
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=Direction.FLAT,
                score=score,
                target_leverage=0.0,
                target_volume=None,
                target_price=None,
                features=feature_payload,
                decision=SignalDecision.BLOCK,
                reason=entry_block,
            )
            return signal, None

        target_leverage = _target_leverage(
            symbol,
            score,
            rv_1h_equiv=_as_optional_float(data.get("rv_1h_equiv")),
            params=self.params,
            max_drawdown=strategy_context.max_drawdown,
        )
        if target_leverage <= EPSILON:
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=Direction.FLAT,
                score=score,
                target_leverage=0.0,
                target_volume=None,
                target_price=None,
                features=feature_payload,
                decision=SignalDecision.BLOCK,
                reason="block: target leverage is zero",
            )
            return signal, None

        signed_target = target_leverage if candidate_side == Direction.LONG else -target_leverage
        if _already_at_or_beyond_target(current_leverage, signed_target):
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=candidate_side,
                score=score,
                target_leverage=abs(current_leverage),
                target_volume=current_volume if current_volume > 0 else None,
                target_price=None,
                features=feature_payload,
                decision=SignalDecision.HOLD,
                reason="hold: existing exposure already at or beyond target",
            )
            return signal, None

        sizing = _entry_sizing(
            symbol,
            row=data,
            side=candidate_side,
            metadata=metadata,
            target_leverage=target_leverage,
            current_leverage=current_leverage,
            current_volume=current_volume,
            equity=strategy_context.equity,
            params=self.params,
        )
        if sizing.block_reason is not None:
            signal = self._signal(
                symbol=symbol,
                now_utc=now,
                feature_time=feature_time,
                direction=Direction.FLAT,
                score=score,
                target_leverage=0.0,
                target_volume=None,
                target_price=None,
                features=feature_payload,
                decision=SignalDecision.BLOCK,
                reason=sizing.block_reason,
            )
            return signal, None

        signal = self._signal(
            symbol=symbol,
            now_utc=now,
            feature_time=feature_time,
            direction=candidate_side,
            score=score,
            target_leverage=target_leverage,
            target_volume=sizing.target_volume,
            target_price=sizing.entry_price,
            features=feature_payload,
            decision=SignalDecision.ENTER,
            reason=f"{candidate_side.value} entry/resize",
        )
        intent = self._order_intent(
            signal=signal,
            row=data,
            metadata=metadata,
            side=OrderSide.BUY if candidate_side == Direction.LONG else OrderSide.SELL,
            requested_volume=sizing.order_volume,
            stop_loss=sizing.stop_loss,
            take_profit=sizing.take_profit,
        )
        return signal, intent

    def _apply_discipline_ballast(
        self,
        *,
        rows: Sequence[Mapping[str, Any]],
        signals: list[Signal],
        order_intents: list[OrderIntent],
        context: StrategyContext,
    ) -> tuple[list[Signal], list[OrderIntent]]:
        """Split a lone high-conviction entry into main + offset ballast legs."""

        entry_signals = {
            signal.signal_id: signal
            for signal in signals
            if _enum_value(signal.decision) == SignalDecision.ENTER.value
            and signal.symbol in PNL_SPRINT_ENTRY_SYMBOLS
            and _enum_value(signal.direction) in {Direction.LONG.value, Direction.SHORT.value}
        }
        entry_intents = [intent for intent in order_intents if intent.signal_id in entry_signals]
        if len(entry_intents) != 1:
            return signals, order_intents

        main_intent = entry_intents[0]
        main_signal = entry_signals[str(main_intent.signal_id)]
        main_target = float(main_signal.target_leverage)
        if main_target < DISCIPLINE_BALLAST_MIN_TRIGGER_LEVERAGE:
            return signals, order_intents

        current_other_gross = sum(
            abs(float(leverage))
            for symbol, leverage in context.current_position_leverage.items()
            if normalize_symbol(symbol) != main_signal.symbol
        )
        ballast_target = main_target * (1.0 - DISCIPLINE_BALLAST_MAIN_SHARE)
        if current_other_gross >= ballast_target * 0.8:
            return signals, order_intents

        row_by_symbol = {normalize_symbol(row.get("symbol")): row for row in rows}
        main_row = row_by_symbol.get(main_signal.symbol)
        main_metadata = _metadata_for(context.symbol_metadata, main_signal.symbol)
        if main_row is None or main_metadata is None:
            return signals, order_intents

        ballast_symbol = _choose_ballast_symbol(
            rows=row_by_symbol,
            context=context,
            excluded_symbol=main_signal.symbol,
        )
        if ballast_symbol is None:
            return signals, order_intents
        ballast_row = row_by_symbol[ballast_symbol]
        ballast_metadata = _metadata_for(context.symbol_metadata, ballast_symbol)
        if ballast_metadata is None:
            return signals, order_intents

        main_direction = (
            Direction.LONG
            if _enum_value(main_signal.direction) == Direction.LONG.value
            else Direction.SHORT
        )
        ballast_direction = Direction.SHORT if main_direction == Direction.LONG else Direction.LONG
        now = _context_now(context)
        ballast_feature_time = _datetime_from_any(ballast_row.get("feature_time_utc"))
        if _activation_block_reason(
            ballast_row,
            symbol=ballast_symbol,
            metadata=ballast_metadata,
            now_utc=now,
            feature_time=ballast_feature_time,
            context=context,
        ) is not None:
            return signals, order_intents

        adjusted_main_target = max(main_target * DISCIPLINE_BALLAST_MAIN_SHARE, 0.0)
        main_sizing = _entry_sizing(
            main_signal.symbol,
            row=main_row,
            side=main_direction,
            metadata=main_metadata,
            target_leverage=adjusted_main_target,
            current_leverage=float(context.current_position_leverage.get(main_signal.symbol, 0.0)),
            current_volume=float(context.current_position_volume.get(main_signal.symbol, 0.0)),
            equity=context.equity,
            params=self.params,
        )
        ballast_sizing = _entry_sizing(
            ballast_symbol,
            row=ballast_row,
            side=ballast_direction,
            metadata=ballast_metadata,
            target_leverage=ballast_target,
            current_leverage=float(context.current_position_leverage.get(ballast_symbol, 0.0)),
            current_volume=float(context.current_position_volume.get(ballast_symbol, 0.0)),
            equity=context.equity,
            params=self.params,
        )
        if main_sizing.block_reason is not None or ballast_sizing.block_reason is not None:
            return signals, order_intents

        adjusted_features = dict(main_signal.features)
        adjusted_features["discipline_ballast_main_share"] = DISCIPLINE_BALLAST_MAIN_SHARE
        adjusted_features["discipline_ballast_symbol"] = ballast_symbol
        adjusted_main_signal = main_signal.model_copy(
            update={
                "target_leverage": adjusted_main_target,
                "target_volume": main_sizing.target_volume,
                "target_price": main_sizing.entry_price,
                "features": adjusted_features,
                "reason": (
                    f"{main_direction.value} entry/resize; discipline ballast keeps "
                    f"main target near {DISCIPLINE_BALLAST_MAIN_SHARE:.0%} of gross"
                ),
            }
        )
        adjusted_main_intent = self._order_intent(
            signal=adjusted_main_signal,
            row=main_row,
            metadata=main_metadata,
            side=OrderSide.BUY if main_direction == Direction.LONG else OrderSide.SELL,
            requested_volume=main_sizing.order_volume,
            stop_loss=main_sizing.stop_loss,
            take_profit=main_sizing.take_profit,
        )

        ballast_score = compute_momo_score(ballast_row)
        ballast_features = _feature_payload(
            ballast_row,
            ballast_score,
            entry_threshold=self.params.entry_threshold,
            exit_threshold=self.params.exit_threshold,
        )
        ballast_features.update(
            {
                "discipline_ballast": True,
                "ballast_for_signal_id": main_signal.signal_id,
                "ballast_for_symbol": main_signal.symbol,
                "ballast_main_share": DISCIPLINE_BALLAST_MAIN_SHARE,
            }
        )
        ballast_signal = self._signal(
            symbol=ballast_symbol,
            now_utc=now,
            feature_time=ballast_feature_time,
            direction=ballast_direction,
            score=ballast_score,
            target_leverage=ballast_target,
            target_volume=ballast_sizing.target_volume,
            target_price=ballast_sizing.entry_price,
            features=ballast_features,
            decision=SignalDecision.ENTER,
            reason=(
                f"{ballast_direction.value} discipline ballast for "
                f"{main_signal.symbol} concentration/net exposure"
            ),
        ).model_copy(
            update={
                "signal_id": f"{main_signal.signal_id}-ballast-{ballast_symbol.replace('/', '')}"
            }
        )
        base_ballast_intent = self._order_intent(
            signal=ballast_signal,
            row=ballast_row,
            metadata=ballast_metadata,
            side=OrderSide.BUY if ballast_direction == Direction.LONG else OrderSide.SELL,
            requested_volume=ballast_sizing.order_volume,
            stop_loss=ballast_sizing.stop_loss,
            take_profit=ballast_sizing.take_profit,
        )
        ballast_intent = base_ballast_intent.model_copy(
            update={
                "metadata": {
                    **base_ballast_intent.metadata,
                    "discipline_ballast": True,
                    "ballast_for_signal_id": main_signal.signal_id,
                    "ballast_for_symbol": main_signal.symbol,
                    "ballast_main_share": DISCIPLINE_BALLAST_MAIN_SHARE,
                }
            }
        )

        updated_signals = [
            adjusted_main_signal if signal.signal_id == main_signal.signal_id else signal
            for signal in signals
        ]
        updated_signals.append(ballast_signal)

        updated_intents = [
            adjusted_main_intent if intent.client_order_id == main_intent.client_order_id else intent
            for intent in order_intents
        ]
        return updated_signals, [ballast_intent] + updated_intents

    def _signal(
        self,
        *,
        symbol: str,
        now_utc: datetime,
        feature_time: datetime,
        direction: Direction,
        score: float,
        target_leverage: float,
        target_volume: float | None,
        target_price: float | None,
        features: dict[str, Any],
        decision: SignalDecision,
        reason: str,
    ) -> Signal:
        return Signal(
            signal_id=_signal_id(self.params.strategy_version, symbol, feature_time),
            created_at_utc=now_utc,
            strategy_version=self.params.strategy_version,
            symbol=symbol,
            timeframe=self.params.signal_timeframe,
            direction=direction,
            score=float(score),
            target_leverage=float(max(target_leverage, 0.0)),
            target_volume=target_volume,
            target_price=target_price,
            features=features,
            decision=decision,
            reason=reason,
        )

    def _order_intent(
        self,
        *,
        signal: Signal,
        row: Mapping[str, Any],
        metadata: SymbolConfig,
        side: OrderSide,
        requested_volume: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> OrderIntent:
        symbol = signal.symbol
        requested_price = _entry_price(row, side)
        return OrderIntent(
            client_order_id=f"intent-{signal.signal_id}",
            signal_id=signal.signal_id,
            created_at_utc=signal.created_at_utc,
            symbol=symbol,
            side=side,
            requested_volume=requested_volume,
            requested_price=requested_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_version=self.params.strategy_version,
            magic=self.magic,
            comment=f"{self.params.strategy_version}:{signal.signal_id}",
            metadata={
                "dry_run_only": True,
                "broker_symbol": metadata.broker_symbol,
                "signal_decision": signal.decision,
                "target_leverage": signal.target_leverage,
                "feature_time_utc": signal.features.get("feature_time_utc"),
            },
        )


@dataclass(frozen=True)
class _SizingResult:
    target_volume: float | None = None
    order_volume: float = 0.0
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    block_reason: str | None = None


def compute_momo_score(row: Mapping[str, Any]) -> float:
    """Compute the frozen momo_v1 score, falling back to stored final score."""
    weighted_inputs = (
        "z_ret_3_m5",
        "z_ret_12_m5",
        "z_ema20_minus_ema80_over_atr",
        "donchian_ensemble",
        "z_ema20_slope_6_over_atr",
    )
    if not any(_is_finite(row.get(column)) for column in weighted_inputs):
        return _as_float(row.get("final_score_raw"))

    trend_pre_volume = (
        0.25 * _as_float(row.get("z_ret_3_m5"))
        + 0.25 * _as_float(row.get("z_ret_12_m5"))
        + 0.20 * _as_float(row.get("z_ema20_minus_ema80_over_atr"))
        + 0.15 * _as_float(row.get("donchian_ensemble"))
        + 0.10 * _as_float(row.get("z_ema20_slope_6_over_atr"))
    )
    volume_zscore = _as_float(row.get("volume_zscore"))
    if volume_zscore >= 0.5 and trend_pre_volume > 0:
        volume_confirmation = 1.0
    elif volume_zscore >= 0.5 and trend_pre_volume < 0:
        volume_confirmation = -1.0
    else:
        volume_confirmation = 0.0
    trend_score = trend_pre_volume + 0.05 * volume_confirmation

    symbol = normalize_symbol(row.get("symbol"))
    if symbol == "BTC/USD" or symbol in FOREX_SYMBOLS:
        return float(trend_score)
    return float(0.75 * trend_score + 0.25 * _as_float(row.get("relative_score")))


def load_symbol_metadata_from_store(
    store: SQLiteStore,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
) -> dict[str, SymbolConfig]:
    """Load latest broker metadata rows needed for dry-run target sizing."""
    symbols = normalize_symbols(target_symbols)
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
    metadata: dict[str, SymbolConfig] = {}
    for row in rows:
        raw_json = row["raw_json"]
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except json.JSONDecodeError:
            raw = {}
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
            raw=raw,
        )
        metadata[config.symbol] = config
    return metadata


def load_strategy_context_from_store(
    store: SQLiteStore,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    *,
    now_utc: datetime | None = None,
    enforce_freshness: bool = True,
) -> StrategyContext:
    """Build a dry-run strategy context from local audit storage."""
    symbols = normalize_symbols(target_symbols)
    metadata = load_symbol_metadata_from_store(store, symbols)
    account = store.fetch_one(
        """
        SELECT equity, max_drawdown
        FROM account_snapshots
        ORDER BY observed_at_utc DESC
        LIMIT 1
        """
    )
    equity = float(account["equity"]) if account and account["equity"] is not None else INITIAL_EQUITY
    max_drawdown = (
        float(account["max_drawdown"])
        if account and account["max_drawdown"] is not None
        else 0.0
    )
    current_leverage, current_volume = _load_position_targets(store, symbols, metadata, equity)
    return StrategyContext(
        symbol_metadata=metadata,
        equity=equity,
        current_position_leverage=current_leverage,
        current_position_volume=current_volume,
        max_drawdown=max_drawdown,
        now_utc=now_utc,
        enforce_freshness=enforce_freshness,
    )


def run_strategy_once_from_store(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    params: StrategyParams | None = None,
    now_utc: datetime | None = None,
    latest_only: bool = True,
    feature_config: FeatureConfig | None = None,
    enforce_freshness: bool = True,
) -> StrategyCycleResult:
    """Compute stored features, generate dry-run signals, and persist them."""
    symbols = normalize_symbols(target_symbols)
    try:
        features = compute_feature_snapshots_from_store(
            database_url,
            target_symbols=symbols,
            config=feature_config,
            require_data=True,
            now_utc=now_utc,
        )
    except FeatureEngineeringError as exc:
        raise StrategyEngineError(str(exc)) from exc

    with SQLiteStore(database_url) as store:
        context = load_strategy_context_from_store(
            store,
            symbols,
            now_utc=now_utc,
            enforce_freshness=enforce_freshness,
        )
        engine = DryRunStrategyEngine(params=params)
        store.upsert_strategy_version(engine.params, active=True, approved_by="strategy_engine")
        return engine.generate_signals(
            features,
            context=context,
            store=store,
            latest_only=latest_only,
        )


def _features_to_frame(features: pd.DataFrame | Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    frame = features.copy() if isinstance(features, pd.DataFrame) else pd.DataFrame(
        [dict(row) for row in features]
    )
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "feature_time_utc"])
    missing = {"symbol", "feature_time_utc"} - set(frame.columns)
    if missing:
        raise StrategyEngineError(
            "feature rows are missing required columns: " + ", ".join(sorted(missing))
        )
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].map(normalize_symbol)
    frame["feature_time_utc"] = pd.to_datetime(frame["feature_time_utc"], utc=True)
    return frame.sort_values(["symbol", "feature_time_utc"]).reset_index(drop=True)


def _activation_block_reason(
    row: Mapping[str, Any],
    *,
    symbol: str,
    metadata: SymbolConfig | None,
    now_utc: datetime,
    feature_time: datetime,
    context: StrategyContext,
) -> str | None:
    if metadata is None:
        return "block: missing confirmed symbol metadata"
    missing_metadata = _missing_metadata_fields(metadata)
    if missing_metadata:
        return "block: incomplete symbol metadata: " + ", ".join(missing_metadata)
    if not _as_bool(row.get("feature_ready")):
        return "block: feature warmup incomplete"
    if bool(row.get("shock_flag", False)):
        return "block: shock flag"
    if context.enforce_freshness:
        bar_age = (now_utc - feature_time).total_seconds()
        if bar_age > 15 * 60:
            return "block: latest completed M5 bar is stale"
        if bar_age < -60:
            return "block: feature timestamp is in the future"
        tick_age = _as_optional_float(row.get("tick_age_seconds"))
        if tick_age is None:
            return "block: latest tick unavailable"
        if tick_age > 120:
            return "block: latest tick is stale"
    bid = _as_optional_float(row.get("bid"))
    ask = _as_optional_float(row.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return "block: current bid/ask unavailable"
    spread_bps = _as_optional_float(row.get("spread_bps"))
    if spread_bps is None:
        return "block: spread bps unavailable"
    if spread_bps > SPREAD_CAP_BPS[symbol]:
        return "block: spread cap"
    if symbol in CRYPTO_SYMBOLS and symbol != "BTC/USD":
        btc_regime = str(row.get("btc_regime", "unknown"))
        if btc_regime == "unknown" or not _is_finite(row.get("btc_trend_score")):
            return "block: BTC regime unavailable"
    return None


def _entry_block_reason(
    row: Mapping[str, Any],
    symbol: str,
    side: Direction,
) -> str | None:
    close = _as_float(row.get("close"))
    ema80 = _as_float(row.get("ema_80"))
    slope = _as_float(row.get("ema20_slope_6_over_atr"))
    if side == Direction.LONG:
        if close <= ema80:
            return "block: long requires close above ema80"
        if slope < 0:
            return "block: long requires non-negative ema20 slope"
        if not _btc_regime_gate(row, symbol, "long"):
            return "block: BTC regime gate for long"
    else:
        if close >= ema80:
            return "block: short requires close below ema80"
        if slope > 0:
            return "block: short requires non-positive ema20 slope"
        if not _btc_regime_gate(row, symbol, "short"):
            return "block: BTC regime gate for short"
    return None


def _btc_regime_gate(row: Mapping[str, Any], symbol: str, side: str) -> bool:
    if symbol == "BTC/USD" or symbol not in CRYPTO_SYMBOLS:
        return True
    btc_regime = str(row.get("btc_regime", "unknown"))
    btc_trend = _as_float(row.get("btc_trend_score"))
    if symbol in {"ETH/USD", "SOL/USD"}:
        if side == "long":
            return btc_regime != "risk_off"
        return btc_regime != "risk_on"
    if side == "long":
        return btc_trend > -0.25
    return btc_trend < 0.25


def _exit_reason(
    row: Mapping[str, Any],
    score: float,
    current_leverage: float,
    *,
    exit_threshold: float,
) -> str | None:
    if abs(current_leverage) <= EPSILON:
        return None
    symbol = normalize_symbol(row.get("symbol"))
    spread_bps = _as_optional_float(row.get("spread_bps"))
    if spread_bps is not None and spread_bps > ABNORMAL_SPREAD_EXIT_MULTIPLE * SPREAD_CAP_BPS[symbol]:
        return "exit: abnormal spread"
    close = _as_float(row.get("close"))
    ema20 = _as_float(row.get("ema_20"))
    if current_leverage > 0:
        if score <= exit_threshold:
            return "exit: score faded"
        if close < ema20:
            return "exit: close below ema20"
    if current_leverage < 0:
        if score >= -exit_threshold:
            return "exit: score faded"
        if close > ema20:
            return "exit: close above ema20"
    return None


def _candidate_side(score: float, entry_threshold: float) -> Direction | None:
    if score >= entry_threshold:
        return Direction.LONG
    if score <= -entry_threshold:
        return Direction.SHORT
    return None


def _choose_ballast_symbol(
    *,
    rows: Mapping[str, Mapping[str, Any]],
    context: StrategyContext,
    excluded_symbol: str,
) -> str | None:
    candidates: list[tuple[float, str]] = []
    now = _context_now(context)
    for symbol in PNL_SPRINT_ENTRY_SYMBOLS:
        if symbol == excluded_symbol:
            continue
        row = rows.get(symbol)
        metadata = _metadata_for(context.symbol_metadata, symbol)
        if row is None or metadata is None:
            continue
        feature_time = _datetime_from_any(row.get("feature_time_utc"))
        if _activation_block_reason(
            row,
            symbol=symbol,
            metadata=metadata,
            now_utc=now,
            feature_time=feature_time,
            context=context,
        ) is not None:
            continue
        spread_bps = _as_optional_float(row.get("spread_bps"))
        candidates.append((spread_bps if spread_bps is not None else math.inf, symbol))
    if not candidates:
        return None
    return sorted(candidates)[0][1]


def chunk_order_intents_for_broker_limits(
    order_intents: Iterable[OrderIntent],
    metadata_map: Mapping[str, SymbolConfig | Mapping[str, Any]],
    *,
    max_chunks_per_signal: int = MAX_ORDER_INTENT_CHUNKS_PER_SIGNAL,
) -> tuple[OrderIntent, ...]:
    """Split oversized order requests without treating max volume as position cap."""

    chunked: list[OrderIntent] = []
    for intent in order_intents:
        metadata = _metadata_for(metadata_map, intent.symbol)
        if metadata is None:
            chunked.append(intent)
            continue
        chunked.extend(
            _chunk_order_intent_for_broker_limit(
                intent,
                metadata,
                max_chunks=max_chunks_per_signal,
            )
        )
    return tuple(chunked)


def _chunk_order_intent_for_broker_limit(
    intent: OrderIntent,
    metadata: SymbolConfig,
    *,
    max_chunks: int,
) -> tuple[OrderIntent, ...]:
    max_volume = metadata.volume_max
    if max_volume is None or max_volume <= 0:
        return (intent,)
    if intent.requested_volume <= max_volume + EPSILON:
        return (intent,)
    chunk_size = _round_volume(float(max_volume), metadata)
    if chunk_size is None or chunk_size <= 0:
        return (intent,)

    min_volume = float(metadata.volume_min or 0.0)
    remaining = float(intent.requested_volume)
    volumes: list[float] = []
    while remaining > EPSILON and len(volumes) < max_chunks:
        raw_chunk = min(remaining, chunk_size)
        rounded_chunk = _round_volume(raw_chunk, metadata)
        if rounded_chunk is None or rounded_chunk <= EPSILON:
            break
        if min_volume > 0 and rounded_chunk + EPSILON < min_volume:
            break
        volumes.append(float(rounded_chunk))
        remaining = max(0.0, remaining - rounded_chunk)

    if not volumes:
        return (intent,)

    emitted = len(volumes)
    deferred = max(0.0, remaining)
    result: list[OrderIntent] = []
    for index, volume in enumerate(volumes, start=1):
        suffix = f"part{index:03d}of{emitted:03d}"
        metadata_update = {
            **intent.metadata,
            "chunked_order": True,
            "chunk_index": index,
            "chunk_count_emitted": emitted,
            "chunk_max_per_signal": max_chunks,
            "original_requested_volume": intent.requested_volume,
            "broker_order_volume_max": max_volume,
            "deferred_requested_volume": deferred,
        }
        result.append(
            intent.model_copy(
                update={
                    "client_order_id": f"{intent.client_order_id}-{suffix}",
                    "requested_volume": volume,
                    "comment": f"{intent.comment or ''}:{suffix}"[-31:],
                    "metadata": metadata_update,
                }
            )
        )
    return tuple(result)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _target_leverage(
    symbol: str,
    score: float,
    *,
    rv_1h_equiv: float | None,
    params: StrategyParams,
    max_drawdown: float,
) -> float:
    del max_drawdown
    # Ramp position size from 0 at the entry threshold to full over the next 1.0
    # of score, so a lower entry threshold still sizes small near the boundary.
    score_scale = _clamp((abs(score) - params.entry_threshold) / 1.0, 0.0, 1.0)
    vol_scale = _vol_scale(symbol, rv_1h_equiv)
    normal_cap = min(NORMAL_SYMBOL_LEVERAGE_CAP[symbol], params.max_symbol_leverage)
    hard_cap = min(HARD_SYMBOL_LEVERAGE_CAP[symbol], params.max_symbol_leverage)
    target = normal_cap * (0.35 + 0.65 * score_scale) * vol_scale
    return min(max(target, 0.0), hard_cap, params.max_gross_leverage)


def _entry_sizing(
    symbol: str,
    *,
    row: Mapping[str, Any],
    side: Direction,
    metadata: SymbolConfig,
    target_leverage: float,
    current_leverage: float,
    current_volume: float,
    equity: float,
    params: StrategyParams,
) -> _SizingResult:
    order_side = OrderSide.BUY if side == Direction.LONG else OrderSide.SELL
    entry_price = _entry_price(row, order_side)
    if entry_price is None:
        return _SizingResult(block_reason="block: entry price unavailable")
    atr = _as_optional_float(row.get("atr_14"))
    spread = _as_optional_float(row.get("spread"))
    point = metadata.point
    if atr is None or spread is None or point is None:
        return _SizingResult(block_reason="block: stop inputs unavailable")
    min_stop_distance = max(3.0 * spread, _metadata_min_stop_distance(metadata))
    stop_distance = max(params.atr_stop_multiple * atr, min_stop_distance)
    take_profit_distance = max(params.take_profit_multiple * atr, min_stop_distance)
    target_volume = _volume_for_leverage(
        target_leverage,
        price=entry_price,
        metadata=metadata,
        equity=equity,
    )
    if target_volume is None:
        return _SizingResult(block_reason="block: target volume cannot be calculated")
    if target_volume < float(metadata.volume_min or 0.0):
        return _SizingResult(block_reason="block: computed target volume below broker minimum")
    signed_current = current_leverage
    if side == Direction.LONG:
        order_volume = target_volume - current_volume if signed_current > 0 else target_volume + current_volume
        stop_loss = _round_price(entry_price - stop_distance, metadata)
        take_profit = _round_price(entry_price + take_profit_distance, metadata)
    else:
        order_volume = target_volume - current_volume if signed_current < 0 else target_volume + current_volume
        stop_loss = _round_price(entry_price + stop_distance, metadata)
        take_profit = _round_price(entry_price - take_profit_distance, metadata)
    order_volume = _round_volume(order_volume, metadata)
    if order_volume is None or order_volume <= 0:
        return _SizingResult(block_reason="block: order volume rounds to zero")
    if stop_loss is None or take_profit is None or stop_loss <= 0 or take_profit <= 0:
        return _SizingResult(block_reason="block: rounded stop or take-profit is invalid")
    return _SizingResult(
        target_volume=target_volume,
        order_volume=order_volume,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def _volume_for_leverage(
    leverage: float,
    *,
    price: float,
    metadata: SymbolConfig,
    equity: float,
) -> float | None:
    contract_size = metadata.trade_contract_size
    if contract_size is None or contract_size <= 0 or price <= 0 or equity <= 0:
        return None
    raw_volume = equity * leverage / (price * contract_size)
    rounded = _round_volume(raw_volume, metadata)
    if rounded is None:
        return None
    return rounded


def _exit_volume(
    symbol: str,
    *,
    row: Mapping[str, Any],
    metadata: SymbolConfig,
    current_leverage: float,
    current_volume: float,
    equity: float,
) -> float:
    if current_volume > 0:
        rounded_current = _round_volume(current_volume, metadata)
        if rounded_current and rounded_current > 0:
            return rounded_current
    price = _as_optional_float(row.get("close")) or _as_optional_float(row.get("last")) or 1.0
    calculated = _volume_for_leverage(
        current_leverage,
        price=price,
        metadata=metadata,
        equity=equity,
    )
    if calculated is None or calculated <= 0:
        min_volume = metadata.volume_min or 0.01
        return float(min_volume)
    del symbol
    return calculated


def _entry_price(row: Mapping[str, Any], side: OrderSide) -> float | None:
    if side == OrderSide.BUY:
        price = _as_optional_float(row.get("ask"))
    else:
        price = _as_optional_float(row.get("bid"))
    if price is None:
        price = _as_optional_float(row.get("close"))
    return price


def _exit_price(row: Mapping[str, Any], current_leverage: float) -> float | None:
    side = OrderSide.SELL if current_leverage > 0 else OrderSide.BUY
    return _entry_price(row, side)


def _round_volume(value: float, metadata: SymbolConfig) -> float | None:
    step = metadata.volume_step
    if step is None or step <= 0 or not math.isfinite(value):
        return None
    rounded = math.floor((value + EPSILON) / step) * step
    min_volume = metadata.volume_min
    if min_volume is not None and rounded < min_volume:
        return rounded
    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    return round(rounded, decimals + 2)


def _round_price(value: float, metadata: SymbolConfig) -> float | None:
    point = metadata.point
    if point is None or point <= 0 or not math.isfinite(value):
        return None
    rounded = round(round(value / point) * point, int(metadata.digits or 8))
    return float(rounded)


def _metadata_min_stop_distance(metadata: SymbolConfig) -> float:
    point = metadata.point
    if point is None or point <= 0:
        return 0.0
    raw = metadata.raw if isinstance(metadata.raw, Mapping) else {}
    stops_level = _as_optional_float(raw.get("trade_stops_level")) or 0.0
    return max(5.0 * point, (stops_level + BROKER_STOP_DISTANCE_BUFFER_POINTS) * point)


def _held_position_target(current_leverage: float) -> tuple[Direction, float]:
    if current_leverage > EPSILON:
        return Direction.LONG, abs(current_leverage)
    if current_leverage < -EPSILON:
        return Direction.SHORT, abs(current_leverage)
    return Direction.FLAT, 0.0


def _already_at_or_beyond_target(current_leverage: float, signed_target: float) -> bool:
    if signed_target > 0:
        return current_leverage >= signed_target - EPSILON
    return current_leverage <= signed_target + EPSILON


def _vol_scale(symbol: str, rv_1h_equiv: float | None) -> float:
    if rv_1h_equiv is None or not math.isfinite(rv_1h_equiv):
        return 0.75
    return _clamp(
        TARGET_RV_1H[symbol] / max(float(rv_1h_equiv), VOLATILITY_FLOOR[symbol]),
        0.35,
        1.25,
    )


def _missing_metadata_fields(metadata: SymbolConfig) -> list[str]:
    missing: list[str] = []
    for field_name in REQUIRED_METADATA_FIELDS:
        value = getattr(metadata, field_name)
        if value is None or value == "":
            missing.append(field_name)
    return missing


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


def _load_position_targets(
    store: SQLiteStore,
    symbols: tuple[str, ...],
    metadata: Mapping[str, SymbolConfig],
    equity: float,
) -> tuple[dict[str, float], dict[str, float]]:
    placeholders = ",".join("?" for _ in symbols)
    rows = store.fetch_all(
        f"""
        SELECT p.symbol, p.side, p.volume, p.price_current
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
    leverage: dict[str, float] = {}
    volume: dict[str, float] = {}
    for row in rows:
        symbol = normalize_symbol(row["symbol"])
        side = str(row["side"] or "flat").lower()
        row_volume = float(row["volume"] or 0.0)
        price_current = float(row["price_current"] or 0.0)
        contract_size = metadata.get(symbol).trade_contract_size if symbol in metadata else None
        signed = 0.0
        if equity > 0 and contract_size and price_current > 0 and row_volume > 0:
            signed = row_volume * price_current * contract_size / equity
            if side == "short":
                signed *= -1.0
            elif side != "long":
                signed = 0.0
        leverage[symbol] = signed
        volume[symbol] = row_volume
    return leverage, volume


def _feature_payload(
    row: Mapping[str, Any],
    score: float,
    *,
    entry_threshold: float,
    exit_threshold: float,
) -> dict[str, Any]:
    payload = {str(key): _json_safe(value) for key, value in row.items()}
    payload["strategy_score"] = score
    payload["entry_threshold"] = float(entry_threshold)
    payload["exit_threshold"] = float(exit_threshold)
    return payload


def _row_mapping(row: Mapping[str, Any] | pd.Series) -> dict[str, Any]:
    if isinstance(row, pd.Series):
        return dict(row.to_dict())
    return dict(row)


def _context_now(context: StrategyContext) -> datetime:
    if context.now_utc is not None:
        return _datetime_from_any(context.now_utc)
    return datetime.now(timezone.utc)


def _signal_id(strategy_version: str, symbol: str, feature_time: datetime) -> str:
    timestamp = feature_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    compact_symbol = symbol.replace("/", "")
    return f"sig-{strategy_version}-{compact_symbol}-{timestamp}"


def _datetime_from_any(value: Any) -> datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
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
    raise StrategyEngineError(f"cannot parse datetime value: {value!r}")


def _as_bool(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.bool_):
        return bool(value.item())
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _as_float(value: Any) -> float:
    number = _as_optional_float(value)
    return 0.0 if number is None else number


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _is_finite(value: Any) -> bool:
    return _as_optional_float(value) is not None


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def _json_safe(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return _datetime_from_any(value).isoformat()
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if value is None or isinstance(value, str | int | float | bool):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        return [_json_safe(item) for item in value]
    return str(value)


__all__ = [
    "ABNORMAL_SPREAD_EXIT_MULTIPLE",
    "HARD_SYMBOL_LEVERAGE_CAP",
    "INITIAL_EQUITY",
    "NORMAL_SYMBOL_LEVERAGE_CAP",
    "REQUIRED_METADATA_FIELDS",
    "SPREAD_CAP_BPS",
    "TARGET_RV_1H",
    "VOLATILITY_FLOOR",
    "DryRunStrategyEngine",
    "StrategyContext",
    "StrategyCycleResult",
    "StrategyEngineError",
    "chunk_order_intents_for_broker_limits",
    "compute_momo_score",
    "load_strategy_context_from_store",
    "load_symbol_metadata_from_store",
    "run_strategy_once_from_store",
]
