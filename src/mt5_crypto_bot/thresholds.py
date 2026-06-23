"""Offline threshold recommendation from stored live/dry-run observations.

This module reads the local SQLite audit database only. It never connects to
MT5, never places orders, and never changes the active strategy parameters.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_DATABASE_URL
from mt5_crypto_bot.schemas import normalize_symbols
from mt5_crypto_bot.storage import SQLiteStore


DEFAULT_ENTRY_GRID: tuple[float, ...] = (0.15, 0.25, 0.35, 0.55, 0.75, 1.0, 1.25, 1.5)
DEFAULT_EXIT_GRID: tuple[float, ...] = (0.05, 0.10, 0.15, 0.25, 0.35, 0.50)
MIN_RECOMMENDATION_ROWS = 20
TARGET_RECOMMENDED_TRADES = 30
MAX_REASONABLE_TRADE_SHARE = 0.35
EPSILON = 1e-12


@dataclass(frozen=True)
class ThresholdEvaluation:
    """One threshold-pair shadow evaluation."""

    entry_threshold: float
    exit_threshold: float
    rows: int
    trade_count: int
    total_return_bps: float
    max_drawdown_bps: float
    sharpe: float
    objective: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_threshold": self.entry_threshold,
            "exit_threshold": self.exit_threshold,
            "rows": self.rows,
            "trade_count": self.trade_count,
            "total_return_bps": self.total_return_bps,
            "max_drawdown_bps": self.max_drawdown_bps,
            "sharpe": self.sharpe,
            "objective": self.objective,
        }


@dataclass(frozen=True)
class ThresholdRecommendation:
    """Best available threshold recommendation from stored observations."""

    current_entry_threshold: float
    current_exit_threshold: float
    recommended_entry_threshold: float
    recommended_exit_threshold: float
    available: bool
    reason: str
    evaluated_rows: int = 0
    evaluated_pairs: int = 0
    best: ThresholdEvaluation | None = None
    current: ThresholdEvaluation | None = None
    top: tuple[ThresholdEvaluation, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "current_entry_threshold": self.current_entry_threshold,
            "current_exit_threshold": self.current_exit_threshold,
            "recommended_entry_threshold": self.recommended_entry_threshold,
            "recommended_exit_threshold": self.recommended_exit_threshold,
            "evaluated_rows": self.evaluated_rows,
            "evaluated_pairs": self.evaluated_pairs,
            "best": self.best.as_dict() if self.best else None,
            "current": self.current.as_dict() if self.current else None,
            "top": [item.as_dict() for item in self.top],
        }


def recommend_thresholds_from_store(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    current_entry_threshold: float,
    current_exit_threshold: float,
    entry_grid: Sequence[float] = DEFAULT_ENTRY_GRID,
    exit_grid: Sequence[float] = DEFAULT_EXIT_GRID,
    min_rows: int = MIN_RECOMMENDATION_ROWS,
) -> ThresholdRecommendation:
    """Recommend thresholds from stored signal scores and next-bar M5 returns.

    The evaluation is deliberately lightweight: it replays a threshold state
    machine over historical signal scores, applies an approximate spread cost,
    and ranks candidate pairs by return, drawdown, Sharpe, and trade-frequency
    discipline. This is a diagnostic recommendation only.
    """

    symbols = normalize_symbols(target_symbols)
    with SQLiteStore(database_url) as store:
        frame = _load_signal_return_frame(store, symbols)

    if frame.empty or len(frame) < min_rows:
        return ThresholdRecommendation(
            current_entry_threshold=float(current_entry_threshold),
            current_exit_threshold=float(current_exit_threshold),
            recommended_entry_threshold=float(current_entry_threshold),
            recommended_exit_threshold=float(current_exit_threshold),
            available=False,
            reason=f"insufficient signal/return rows for threshold recommendation ({len(frame)}/{min_rows})",
            evaluated_rows=len(frame),
        )

    pairs = _candidate_pairs(entry_grid, exit_grid)
    current_pair = (float(current_entry_threshold), float(current_exit_threshold))
    if current_pair[1] < current_pair[0] and current_pair not in pairs:
        pairs.append(current_pair)

    evaluations = tuple(
        sorted(
            (_evaluate_threshold_pair(frame, entry, exit_) for entry, exit_ in pairs),
            key=lambda item: item.objective,
            reverse=True,
        )
    )
    best = evaluations[0] if evaluations else None
    current = min(
        evaluations,
        key=lambda item: (
            abs(item.entry_threshold - current_pair[0]),
            abs(item.exit_threshold - current_pair[1]),
        ),
    ) if evaluations else None
    if best is None:
        return ThresholdRecommendation(
            current_entry_threshold=float(current_entry_threshold),
            current_exit_threshold=float(current_exit_threshold),
            recommended_entry_threshold=float(current_entry_threshold),
            recommended_exit_threshold=float(current_exit_threshold),
            available=False,
            reason="no valid threshold pairs were available",
            evaluated_rows=len(frame),
        )

    return ThresholdRecommendation(
        current_entry_threshold=float(current_entry_threshold),
        current_exit_threshold=float(current_exit_threshold),
        recommended_entry_threshold=best.entry_threshold,
        recommended_exit_threshold=best.exit_threshold,
        available=True,
        reason=(
            "offline recommendation only; update ENTRY_THRESHOLD/EXIT_THRESHOLD "
            "after human review and validation"
        ),
        evaluated_rows=len(frame),
        evaluated_pairs=len(evaluations),
        best=best,
        current=current,
        top=evaluations[:5],
    )


def _load_signal_return_frame(store: SQLiteStore, symbols: tuple[str, ...]) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in symbols)
    signals = pd.DataFrame(
        [
            dict(row)
            for row in store.fetch_all(
                f"""
                SELECT signal_id, created_at_utc, symbol, score, decision, features_json
                FROM signals
                WHERE symbol IN ({placeholders})
                ORDER BY symbol, created_at_utc
                """,
                symbols,
            )
        ]
    )
    bars = pd.DataFrame(
        [
            dict(row)
            for row in store.fetch_all(
                f"""
                SELECT symbol, time_utc, close, spread
                FROM bars
                WHERE symbol IN ({placeholders}) AND timeframe = 'M5'
                ORDER BY symbol, time_utc
                """,
                symbols,
            )
        ]
    )
    if signals.empty or bars.empty:
        return pd.DataFrame()

    signal_rows: list[dict[str, Any]] = []
    for _, row in signals.iterrows():
        features = _json_loads(row.get("features_json"))
        signal_rows.append(
            {
                "signal_id": row.get("signal_id"),
                "symbol": row.get("symbol"),
                "feature_time_utc": features.get("feature_time_utc") or row.get("created_at_utc"),
                "score": _as_float(row.get("score")),
                "spread_bps": _as_optional_float(features.get("spread_bps")),
            }
        )
    signal_frame = pd.DataFrame(signal_rows).dropna(subset=["symbol", "score"])
    signal_frame["feature_time_utc"] = _to_utc_series(signal_frame["feature_time_utc"])
    signal_frame = signal_frame.dropna(subset=["feature_time_utc"])

    bar_frame = bars.copy()
    bar_frame["time_utc"] = _to_utc_series(bar_frame["time_utc"])
    bar_frame["close"] = pd.to_numeric(bar_frame["close"], errors="coerce")
    bar_frame = bar_frame.dropna(subset=["symbol", "time_utc", "close"])
    bar_frame = bar_frame.sort_values(["symbol", "time_utc"]).reset_index(drop=True)
    bar_frame["next_close"] = bar_frame.groupby("symbol")["close"].shift(-1)
    bar_frame["return_bps"] = (
        (bar_frame["next_close"] / bar_frame["close"].where(bar_frame["close"].abs() > EPSILON) - 1.0)
        * 10_000.0
    )
    bar_frame = bar_frame.dropna(subset=["return_bps"])

    joined_parts: list[pd.DataFrame] = []
    for symbol in symbols:
        left = signal_frame[signal_frame["symbol"] == symbol].sort_values("feature_time_utc")
        right = bar_frame[bar_frame["symbol"] == symbol].sort_values("time_utc")
        if left.empty or right.empty:
            continue
        joined_parts.append(
            pd.merge_asof(
                left,
                right[["time_utc", "close", "return_bps"]],
                left_on="feature_time_utc",
                right_on="time_utc",
                direction="backward",
                tolerance=pd.Timedelta(minutes=10),
            )
        )
    if not joined_parts:
        return pd.DataFrame()
    joined = pd.concat(joined_parts, ignore_index=True)
    joined["spread_bps"] = pd.to_numeric(joined["spread_bps"], errors="coerce").fillna(0.0)
    joined["score"] = pd.to_numeric(joined["score"], errors="coerce")
    joined["return_bps"] = pd.to_numeric(joined["return_bps"], errors="coerce")
    return joined.dropna(subset=["score", "return_bps"]).sort_values(
        ["symbol", "feature_time_utc"]
    ).reset_index(drop=True)


def _candidate_pairs(
    entry_grid: Sequence[float],
    exit_grid: Sequence[float],
) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for entry in sorted({float(value) for value in entry_grid if _is_positive(value)}):
        for exit_ in sorted({float(value) for value in exit_grid if _is_non_negative(value)}):
            if exit_ < entry:
                pairs.append((entry, exit_))
    return pairs


def _evaluate_threshold_pair(
    frame: pd.DataFrame,
    entry_threshold: float,
    exit_threshold: float,
) -> ThresholdEvaluation:
    pnl_bps: list[float] = []
    trade_count = 0
    for _, group in frame.groupby("symbol", sort=True):
        state = 0
        ordered = group.sort_values("feature_time_utc")
        for row in ordered.itertuples(index=False):
            score = float(row.score)
            previous_state = state
            if state > 0:
                if score <= -entry_threshold:
                    state = -1
                elif score <= exit_threshold:
                    state = 0
            elif state < 0:
                if score >= entry_threshold:
                    state = 1
                elif score >= -exit_threshold:
                    state = 0
            else:
                if score >= entry_threshold:
                    state = 1
                elif score <= -entry_threshold:
                    state = -1

            state_change = abs(state - previous_state)
            if state_change:
                trade_count += 1 if state_change == 1 else 2
            spread_bps = max(float(getattr(row, "spread_bps", 0.0) or 0.0), 0.0)
            transaction_cost_bps = 0.5 * spread_bps * state_change
            pnl_bps.append(state * float(row.return_bps) - transaction_cost_bps)

    if not pnl_bps:
        return ThresholdEvaluation(entry_threshold, exit_threshold, 0, 0, 0.0, 0.0, 0.0, -math.inf)
    series = pd.Series(pnl_bps, dtype=float)
    cumulative = series.cumsum()
    drawdown = (cumulative.cummax() - cumulative).max()
    std = float(series.std(ddof=0))
    sharpe = 0.0 if std <= EPSILON else float(series.mean() / std)
    total = float(series.sum())
    max_drawdown = float(drawdown if math.isfinite(float(drawdown)) else 0.0)
    inactivity_penalty = max(0, TARGET_RECOMMENDED_TRADES - trade_count) * 1.5
    churn_penalty = max(0.0, trade_count - len(series) * MAX_REASONABLE_TRADE_SHARE) * 0.25
    objective = (
        total
        - 0.7 * max_drawdown
        + 10.0 * sharpe
        - 0.10 * trade_count
        - inactivity_penalty
        - churn_penalty
    )
    return ThresholdEvaluation(
        entry_threshold=float(entry_threshold),
        exit_threshold=float(exit_threshold),
        rows=int(len(series)),
        trade_count=int(trade_count),
        total_return_bps=total,
        max_drawdown_bps=max_drawdown,
        sharpe=sharpe,
        objective=float(objective),
    )


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_utc_series(series: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(series, utc=True, errors="coerce", format="mixed")
    except TypeError:
        return series.map(_datetime_or_nat)


def _datetime_or_nat(value: Any) -> datetime | pd.NaT:
    if isinstance(value, pd.Timestamp):
        return value.tz_convert("UTC").to_pydatetime() if value.tzinfo else value.tz_localize("UTC").to_pydatetime()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return pd.NaT
    try:
        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return pd.NaT
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    return number if math.isfinite(number) else None


def _is_positive(value: Any) -> bool:
    number = _as_optional_float(value)
    return number is not None and number > 0


def _is_non_negative(value: Any) -> bool:
    number = _as_optional_float(value)
    return number is not None and number >= 0


__all__ = [
    "DEFAULT_ENTRY_GRID",
    "DEFAULT_EXIT_GRID",
    "MIN_RECOMMENDATION_ROWS",
    "ThresholdEvaluation",
    "ThresholdRecommendation",
    "recommend_thresholds_from_store",
]
