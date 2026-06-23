"""Deterministic feature engineering for completed FX/crypto bar snapshots.

The feature layer is read-only. It consumes stored bars, ticks, and optional
order-book snapshots, then emits one row per canonical symbol and completed M5
signal bar. All rolling windows are backward-looking and Donchian boundaries use
prior bars only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, CRYPTO_SYMBOLS, DEFAULT_DATABASE_URL
from mt5_crypto_bot.schemas import normalize_symbol, normalize_symbols
from mt5_crypto_bot.storage import SQLiteStore, parse_sqlite_path


TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "H1": 60,
}

FEATURE_TIMEFRAME = "M5"
EPSILON = 1e-12
BTC_SYMBOL = "BTC/USD"

SHOCK_FLOORS: dict[str, float] = {
    "AUD/USD": 0.0030,
    "BAR/USD": 0.0150,
    "BTC/USD": 0.0075,
    "EUR/CHF": 0.0025,
    "EUR/GBP": 0.0025,
    "EUR/USD": 0.0025,
    "ETH/USD": 0.0075,
    "GBP/USD": 0.0035,
    "SOL/USD": 0.0125,
    "USD/CAD": 0.0030,
    "USD/CHF": 0.0030,
    "USD/JPY": 0.0030,
    "XRP/USD": 0.0125,
}

CORE_READY_COLUMNS: tuple[str, ...] = (
    "z_ret_3_m5",
    "z_ret_12_m5",
    "z_ema20_minus_ema80_over_atr",
    "donchian_ensemble",
    "z_ema20_slope_6_over_atr",
    "atr_14",
    "rv_48_m5",
)


class FeatureEngineeringError(RuntimeError):
    """Raised when feature snapshots cannot be computed safely."""


@dataclass(frozen=True)
class FeatureConfig:
    """Feature parameters frozen from ``docs/strategy_design_freeze.md``."""

    warmup_bars: int = 96
    zscore_window: int = 96
    zscore_min_periods: int = 48
    beta_window: int = 96
    beta_min_periods: int = 48
    atr_period: int = 14
    ema_spans: tuple[int, ...] = (8, 20, 55, 80)
    donchian_lookbacks: tuple[int, ...] = (12, 24, 48)
    realized_vol_windows: tuple[int, ...] = (12, 24, 48)
    signal_timeframe: str = FEATURE_TIMEFRAME
    book_imbalance_threshold: float = 0.15

    def __post_init__(self) -> None:
        if self.signal_timeframe != FEATURE_TIMEFRAME:
            raise FeatureEngineeringError("only M5 signal features are supported in momo_v1")
        positive_fields = (
            self.warmup_bars,
            self.zscore_window,
            self.zscore_min_periods,
            self.beta_window,
            self.beta_min_periods,
            self.atr_period,
        )
        if any(value <= 0 for value in positive_fields):
            raise FeatureEngineeringError("feature windows must be positive")
        if self.zscore_min_periods > self.zscore_window:
            raise FeatureEngineeringError("zscore_min_periods cannot exceed zscore_window")
        if self.beta_min_periods > self.beta_window:
            raise FeatureEngineeringError("beta_min_periods cannot exceed beta_window")


def compute_feature_snapshots(
    bars: pd.DataFrame | Iterable[Mapping[str, Any]],
    *,
    ticks: pd.DataFrame | Iterable[Mapping[str, Any]] | None = None,
    order_book_snapshots: pd.DataFrame | Iterable[Mapping[str, Any]] | None = None,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    config: FeatureConfig | None = None,
    now_utc: datetime | None = None,
) -> pd.DataFrame:
    """Compute deterministic M5 feature snapshots from market-data rows.

    ``bars`` may contain M1, M5, M15, and H1 rows. M5 rows are used directly
    when present; otherwise M1 rows are resampled into completed M5 bars. M15 and
    H1 rows are merged as additional backward-looking context when available.
    """
    feature_config = config or FeatureConfig()
    symbols = normalize_symbols(target_symbols)
    bars_frame = _bars_to_frame(bars, symbols)
    signal_bars = _signal_bars_from_market_bars(
        bars_frame,
        target_symbols=symbols,
        now_utc=now_utc,
    )
    if signal_bars.empty:
        return _empty_feature_frame()

    per_symbol = [
        _compute_single_symbol_features(group, feature_config)
        for _, group in signal_bars.groupby("symbol", sort=True)
    ]
    features = pd.concat(per_symbol, ignore_index=True)
    features = _attach_higher_timeframe_context(features, bars_frame, feature_config, symbols)
    features = _attach_tick_features(features, ticks, symbols, now_utc=now_utc)
    features = _attach_order_book_features(features, order_book_snapshots, symbols)
    features = _attach_btc_context_and_relative_strength(features, symbols, feature_config)
    features = _compute_final_scores(features, feature_config)
    features = _finalize_feature_frame(features)
    return features


def compute_feature_snapshots_from_store(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    config: FeatureConfig | None = None,
    require_data: bool = True,
    now_utc: datetime | None = None,
) -> pd.DataFrame:
    """Load stored market data from SQLite and compute feature snapshots."""
    database_path = parse_sqlite_path(database_url)
    if database_path != ":memory:" and not Path(database_path).exists() and require_data:
        raise FeatureEngineeringError(
            f"SQLite database not found at {database_path}; run the collector first"
        )

    symbols = normalize_symbols(target_symbols)
    placeholders = ",".join("?" for _ in symbols)
    with SQLiteStore(database_url) as store:
        bars = store.fetch_all(
            f"""
            SELECT symbol, timeframe, time_utc, open, high, low, close,
                   tick_volume, spread, real_volume, source
            FROM bars
            WHERE symbol IN ({placeholders})
              AND timeframe IN ('M1', 'M5', 'M15', 'H1')
            ORDER BY symbol, timeframe, time_utc
            """,
            symbols,
        )
        if not bars and require_data:
            raise FeatureEngineeringError("no M1/M5/M15/H1 bars found in local storage")

        ticks = store.fetch_all(
            f"""
            SELECT symbol, time_utc, time_msc, bid, ask, last, volume, volume_real
            FROM ticks
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, time_utc
            """,
            symbols,
        )
        order_book = store.fetch_all(
            f"""
            SELECT symbol, observed_at_utc, side, level, price, volume, volume_dbl
            FROM order_book_snapshots
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, observed_at_utc, side, level
            """,
            symbols,
        )

    return compute_feature_snapshots(
        [dict(row) for row in bars],
        ticks=[dict(row) for row in ticks],
        order_book_snapshots=[dict(row) for row in order_book],
        target_symbols=symbols,
        config=config,
        now_utc=now_utc,
    )


def latest_feature_snapshots(features: pd.DataFrame) -> pd.DataFrame:
    """Return the latest computed feature row for each symbol."""
    if features.empty:
        return features.copy()
    sorted_frame = features.sort_values(["symbol", "feature_time_utc"])
    return sorted_frame.groupby("symbol", as_index=False, sort=True).tail(1).reset_index(drop=True)


def write_feature_snapshots(
    features: pd.DataFrame,
    output_path: str | Path,
    *,
    latest_only: bool = False,
) -> Path:
    """Write feature snapshots to CSV, JSON, or JSONL based on file suffix."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    output = latest_feature_snapshots(features) if latest_only else features
    output = output.copy()
    for column in ("bar_time_utc", "feature_time_utc", "tick_time_utc"):
        if column in output.columns:
            output[column] = output[column].map(_iso_or_empty)

    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(output.to_json(orient="records", indent=2), encoding="utf-8")
    elif suffix == ".jsonl":
        path.write_text(output.to_json(orient="records", lines=True), encoding="utf-8")
    else:
        output.to_csv(path, index=False)
    return path


def compute_and_export_feature_snapshots(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    output_path: str | Path = "reports/features/feature_snapshots.csv",
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    config: FeatureConfig | None = None,
    latest_only: bool = False,
) -> tuple[pd.DataFrame, Path]:
    """Compute snapshots from local storage and export them to disk."""
    features = compute_feature_snapshots_from_store(
        database_url,
        target_symbols=target_symbols,
        config=config,
        require_data=True,
    )
    path = write_feature_snapshots(features, output_path, latest_only=latest_only)
    return features, path


def _bars_to_frame(
    bars: pd.DataFrame | Iterable[Mapping[str, Any]],
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    frame = _materialize_frame(bars)
    if frame.empty:
        return _empty_bars_frame()

    if "time_utc" not in frame.columns and "time" in frame.columns:
        frame["time_utc"] = frame["time"]
    required = {"symbol", "timeframe", "time_utc", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FeatureEngineeringError(
            "bar rows are missing required columns: " + ", ".join(missing)
        )

    frame = frame.copy()
    frame["symbol"] = frame["symbol"].map(normalize_symbol)
    frame = frame[frame["symbol"].isin(symbols)]
    frame["timeframe"] = frame["timeframe"].astype(str).str.upper()
    frame = frame[frame["timeframe"].isin(TIMEFRAME_MINUTES)]
    frame["time_utc"] = _parse_utc_series(frame["time_utc"])
    for column in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = np.nan

    frame = frame.dropna(subset=["time_utc", "open", "high", "low", "close"])
    frame = frame.sort_values(["symbol", "timeframe", "time_utc"])
    return frame.drop_duplicates(["symbol", "timeframe", "time_utc"], keep="last")


def _signal_bars_from_market_bars(
    bars: pd.DataFrame,
    *,
    target_symbols: tuple[str, ...],
    now_utc: datetime | None,
) -> pd.DataFrame:
    m5 = bars[bars["timeframe"] == FEATURE_TIMEFRAME].copy()
    symbols_with_m5 = set(m5["symbol"].unique())
    missing_symbols = [symbol for symbol in target_symbols if symbol not in symbols_with_m5]
    if missing_symbols:
        m1 = bars[(bars["timeframe"] == "M1") & (bars["symbol"].isin(missing_symbols))]
        if not m1.empty:
            m5_from_m1 = _resample_m1_to_m5(m1)
            m5 = pd.concat([m5, m5_from_m1], ignore_index=True)

    if m5.empty:
        return m5

    m5 = m5.sort_values(["symbol", "time_utc"]).drop_duplicates(
        ["symbol", "time_utc"],
        keep="last",
    )
    m5["bar_time_utc"] = m5["time_utc"]
    m5["feature_time_utc"] = m5["bar_time_utc"] + pd.to_timedelta(5, unit="minute")
    if now_utc is not None:
        now = _to_utc_timestamp(now_utc)
        m5 = m5[m5["feature_time_utc"] <= now]
    m5["timeframe"] = FEATURE_TIMEFRAME
    return m5.reset_index(drop=True)


def _resample_m1_to_m5(m1: pd.DataFrame) -> pd.DataFrame:
    output: list[pd.DataFrame] = []
    for symbol, group in m1.groupby("symbol", sort=True):
        indexed = group.sort_values("time_utc").set_index("time_utc")
        resampled = indexed.resample("5min", label="left", closed="left").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "tick_volume": "sum",
                "spread": "last",
                "real_volume": "sum",
            }
        )
        resampled = resampled.dropna(subset=["open", "high", "low", "close"])
        resampled["symbol"] = symbol
        resampled["timeframe"] = FEATURE_TIMEFRAME
        resampled["source"] = "resampled_m1"
        output.append(resampled.reset_index().rename(columns={"time_utc": "time_utc"}))
    if not output:
        return _empty_bars_frame()
    return pd.concat(output, ignore_index=True)


def _compute_single_symbol_features(group: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
    frame = group.sort_values("feature_time_utc").reset_index(drop=True).copy()
    frame["bar_index"] = np.arange(len(frame), dtype=int)

    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    prev_close = close.shift(1)

    for horizon in (1, 3, 12, 48):
        frame[f"ret_{horizon}_m5"] = close.pct_change(horizon)
    frame["ret_15m"] = frame["ret_3_m5"]
    frame["ret_1h"] = frame["ret_12_m5"]
    frame["ret_4h"] = frame["ret_48_m5"]
    frame["z_ret_3_m5"] = _rolling_zscore(
        frame["ret_3_m5"],
        config.zscore_window,
        config.zscore_min_periods,
    )
    frame["z_ret_12_m5"] = _rolling_zscore(
        frame["ret_12_m5"],
        config.zscore_window,
        config.zscore_min_periods,
    )

    true_range = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    frame["true_range"] = true_range
    frame["atr_14"] = true_range.rolling(config.atr_period, min_periods=config.atr_period).mean()

    for span in config.ema_spans:
        frame[f"ema_{span}"] = close.ewm(span=span, adjust=False, min_periods=span).mean()
    frame["ema20_minus_ema80_over_atr"] = _safe_divide(
        frame["ema_20"] - frame["ema_80"],
        frame["atr_14"],
    )
    frame["z_ema20_minus_ema80_over_atr"] = _rolling_zscore(
        frame["ema20_minus_ema80_over_atr"],
        config.zscore_window,
        config.zscore_min_periods,
    )
    frame["ema20_slope_3_over_atr"] = _safe_divide(
        frame["ema_20"] - frame["ema_20"].shift(3),
        frame["atr_14"],
    )
    frame["ema20_slope_6_over_atr"] = _safe_divide(
        frame["ema_20"] - frame["ema_20"].shift(6),
        frame["atr_14"],
    )
    frame["z_ema20_slope_6_over_atr"] = _rolling_zscore(
        frame["ema20_slope_6_over_atr"],
        config.zscore_window,
        config.zscore_min_periods,
    )
    frame["above_ema80"] = np.where(close > frame["ema_80"], 1.0, 0.0)
    frame.loc[frame["ema_80"].isna(), "above_ema80"] = np.nan

    for lookback in config.donchian_lookbacks:
        prior_high = high.shift(1).rolling(lookback, min_periods=lookback).max()
        prior_low = low.shift(1).rolling(lookback, min_periods=lookback).min()
        frame[f"rolling_high_{lookback}"] = prior_high
        frame[f"rolling_low_{lookback}"] = prior_low
        range_position = _safe_divide(close - prior_low, prior_high - prior_low)
        frame[f"high_low_position_{lookback}"] = range_position
        frame[f"distance_to_high_{lookback}_over_atr"] = _safe_divide(
            prior_high - close,
            frame["atr_14"],
        )
        frame[f"distance_to_low_{lookback}_over_atr"] = _safe_divide(
            close - prior_low,
            frame["atr_14"],
        )
        range_score = (range_position - 0.5).clip(lower=-0.5, upper=0.5)
        frame[f"donchian_{lookback}"] = np.select(
            [close > prior_high, close < prior_low],
            [1.0, -1.0],
            default=range_score,
        )
        missing_channel = prior_high.isna() | prior_low.isna()
        frame.loc[missing_channel, f"donchian_{lookback}"] = np.nan
    frame["donchian_ensemble"] = frame[
        [f"donchian_{lookback}" for lookback in config.donchian_lookbacks]
    ].mean(axis=1)

    for window in config.realized_vol_windows:
        frame[f"rv_{window}_m5"] = frame["ret_1_m5"].rolling(
            window,
            min_periods=window,
        ).std(ddof=0)
    frame["rv_1h_equiv"] = frame["rv_48_m5"] * np.sqrt(12.0)

    shock_floor = frame["symbol"].map(SHOCK_FLOORS).fillna(0.0125)
    shock_threshold = np.maximum(3.0 * frame["rv_48_m5"], shock_floor)
    frame["shock_floor"] = shock_floor
    frame["shock_flag"] = (frame["ret_1_m5"].abs() > shock_threshold).fillna(False)

    if "tick_volume" in frame.columns:
        frame["volume_zscore"] = _rolling_zscore(
            frame["tick_volume"],
            config.zscore_window,
            config.zscore_min_periods,
        )
    else:
        frame["volume_zscore"] = np.nan

    score_base = (
        0.25 * frame["z_ret_3_m5"].fillna(0.0)
        + 0.25 * frame["z_ret_12_m5"].fillna(0.0)
        + 0.20 * frame["z_ema20_minus_ema80_over_atr"].fillna(0.0)
        + 0.15 * frame["donchian_ensemble"].fillna(0.0)
        + 0.10 * frame["z_ema20_slope_6_over_atr"].fillna(0.0)
    )
    frame["trend_score_pre_volume"] = score_base
    frame["volume_confirmation"] = np.select(
        [
            (frame["volume_zscore"] >= 0.5) & (score_base > 0),
            (frame["volume_zscore"] >= 0.5) & (score_base < 0),
        ],
        [1.0, -1.0],
        default=0.0,
    )
    frame["trend_score"] = score_base + 0.05 * frame["volume_confirmation"]

    frame["feature_warmup_complete"] = (frame["bar_index"] + 1) >= config.warmup_bars
    frame["feature_ready"] = (
        frame["feature_warmup_complete"]
        & frame[list(CORE_READY_COLUMNS)].notna().all(axis=1)
    )
    return frame


def _attach_higher_timeframe_context(
    features: pd.DataFrame,
    bars: pd.DataFrame,
    config: FeatureConfig,
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    del config
    output = features.copy()
    for timeframe in ("M15", "H1"):
        context = bars[(bars["timeframe"] == timeframe) & (bars["symbol"].isin(symbols))].copy()
        prefix = timeframe.lower()
        if context.empty:
            output[f"{prefix}_close"] = np.nan
            output[f"{prefix}_ret_1_bar"] = np.nan
            continue
        minutes = TIMEFRAME_MINUTES[timeframe]
        context = context.sort_values(["symbol", "time_utc"])
        context[f"{prefix}_feature_time_utc"] = (
            context["time_utc"] + pd.to_timedelta(minutes, unit="minute")
        )
        context[f"{prefix}_close"] = context["close"]
        context[f"{prefix}_ret_1_bar"] = context.groupby("symbol")["close"].pct_change(1)
        output = _merge_asof_by_symbol(
            output,
            context[
                [
                    "symbol",
                    f"{prefix}_feature_time_utc",
                    f"{prefix}_close",
                    f"{prefix}_ret_1_bar",
                ]
            ],
            right_time_column=f"{prefix}_feature_time_utc",
            value_columns=(f"{prefix}_close", f"{prefix}_ret_1_bar"),
        )
    return output


def _attach_tick_features(
    features: pd.DataFrame,
    ticks: pd.DataFrame | Iterable[Mapping[str, Any]] | None,
    symbols: tuple[str, ...],
    *,
    now_utc: datetime | None = None,
) -> pd.DataFrame:
    output = features.copy()
    tick_frame = _ticks_to_frame(ticks, symbols)
    tick_columns = ("tick_time_utc", "bid", "ask", "last", "spread", "spread_bps")
    output = output.drop(columns=[column for column in tick_columns if column in output], errors="ignore")
    if tick_frame.empty:
        for column in tick_columns:
            output[column] = np.nan
        output["tick_age_seconds"] = np.nan
        return output

    output = _merge_asof_by_symbol(
        output,
        tick_frame[["symbol", *tick_columns]],
        right_time_column="tick_time_utc",
        value_columns=tick_columns,
    )
    if now_utc is not None:
        now = _to_utc_timestamp(now_utc)
        eligible_ticks = tick_frame[tick_frame["tick_time_utc"] <= now].copy()
        if not eligible_ticks.empty:
            latest_ticks = (
                eligible_ticks.sort_values(["symbol", "tick_time_utc"])
                .groupby("symbol", as_index=False, sort=False)
                .tail(1)
                .set_index("symbol")
            )
            latest_indices = output.sort_values(["symbol", "feature_time_utc"]).groupby(
                "symbol",
                sort=False,
            ).tail(1).index
            for index in latest_indices:
                symbol = output.at[index, "symbol"]
                if symbol not in latest_ticks.index:
                    continue
                latest_tick = latest_ticks.loc[symbol]
                current_tick_time = output.at[index, "tick_time_utc"]
                if pd.notna(current_tick_time) and current_tick_time >= latest_tick["tick_time_utc"]:
                    continue
                for column in tick_columns:
                    output.at[index, column] = latest_tick[column]
        age_reference = pd.Series(now, index=output.index)
    else:
        age_reference = output["feature_time_utc"]
    output["tick_age_seconds"] = (age_reference - output["tick_time_utc"]).dt.total_seconds()
    output.loc[output["tick_age_seconds"] < 0, "tick_age_seconds"] = np.nan
    return output


def _attach_order_book_features(
    features: pd.DataFrame,
    order_book_snapshots: pd.DataFrame | Iterable[Mapping[str, Any]] | None,
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    output = features.copy()
    book = _order_book_to_imbalance_frame(order_book_snapshots, symbols)
    book_columns = ("book_time_utc", "top5_bid_volume", "top5_ask_volume", "book_imbalance")
    if book.empty:
        for column in book_columns:
            output[column] = np.nan
        return output
    return _merge_asof_by_symbol(
        output,
        book[["symbol", *book_columns]],
        right_time_column="book_time_utc",
        value_columns=book_columns,
    )


def _attach_btc_context_and_relative_strength(
    features: pd.DataFrame,
    symbols: tuple[str, ...],
    config: FeatureConfig,
) -> pd.DataFrame:
    output = features.sort_values(["symbol", "feature_time_utc"]).copy()
    btc = output[output["symbol"] == BTC_SYMBOL].copy()
    if btc.empty:
        for column in (
            "btc_ret_1_m5",
            "btc_ret_3_m5",
            "btc_ret_12_m5",
            "btc_ret_48_m5",
            "btc_trend_score",
            "btc_above_ema80",
        ):
            output[column] = np.nan
        output["btc_regime"] = "unknown"
        output["beta_to_btc"] = np.where(output["symbol"] == BTC_SYMBOL, 1.0, np.nan)
        output["relative_ret_12_m5"] = np.nan
        output["relative_score"] = np.nan
        return output

    btc_context = btc[
        [
            "feature_time_utc",
            "ret_1_m5",
            "ret_3_m5",
            "ret_12_m5",
            "ret_48_m5",
            "trend_score",
            "above_ema80",
        ]
    ].rename(
        columns={
            "ret_1_m5": "btc_ret_1_m5",
            "ret_3_m5": "btc_ret_3_m5",
            "ret_12_m5": "btc_ret_12_m5",
            "ret_48_m5": "btc_ret_48_m5",
            "trend_score": "btc_trend_score",
            "above_ema80": "btc_above_ema80",
        }
    )
    output = pd.merge_asof(
        output.sort_values("feature_time_utc"),
        btc_context.sort_values("feature_time_utc"),
        on="feature_time_utc",
        direction="backward",
    )
    output["btc_regime"] = np.select(
        [output["btc_trend_score"] >= 0.75, output["btc_trend_score"] <= -0.75],
        ["risk_on", "risk_off"],
        default="neutral",
    )

    output = _attach_beta_to_btc(output, symbols, config)
    output["relative_ret_12_m5"] = np.where(
        output["symbol"] == BTC_SYMBOL,
        0.0,
        output["ret_12_m5"] - output["beta_to_btc"] * output["btc_ret_12_m5"],
    )
    output["relative_score"] = output.groupby("symbol", group_keys=False)[
        "relative_ret_12_m5"
    ].apply(
        lambda series: _rolling_zscore(
            series,
            config.zscore_window,
            config.zscore_min_periods,
        )
    )
    output.loc[output["symbol"] == BTC_SYMBOL, "relative_score"] = 0.0
    return output


def _attach_beta_to_btc(
    features: pd.DataFrame,
    symbols: tuple[str, ...],
    config: FeatureConfig,
) -> pd.DataFrame:
    output = features.copy()
    if BTC_SYMBOL not in symbols:
        output["beta_to_btc"] = np.nan
        return output

    pivot = output.pivot_table(
        index="feature_time_utc",
        columns="symbol",
        values="ret_1_m5",
        aggfunc="last",
    ).sort_index()
    if BTC_SYMBOL not in pivot.columns:
        output["beta_to_btc"] = np.nan
        return output

    btc_returns = pivot[BTC_SYMBOL]
    btc_variance = btc_returns.rolling(
        config.beta_window,
        min_periods=config.beta_min_periods,
    ).var(ddof=0)

    beta_frames: list[pd.DataFrame] = []
    for symbol in symbols:
        if symbol == BTC_SYMBOL:
            beta = pd.Series(1.0, index=pivot.index)
        elif symbol in pivot.columns:
            covariance = pivot[symbol].rolling(
                config.beta_window,
                min_periods=config.beta_min_periods,
            ).cov(btc_returns)
            beta = _safe_divide(covariance, btc_variance).clip(lower=-2.5, upper=3.5)
        else:
            beta = pd.Series(np.nan, index=pivot.index)
        beta_frames.append(
            pd.DataFrame(
                {
                    "feature_time_utc": beta.index,
                    "symbol": symbol,
                    "beta_to_btc": beta.to_numpy(),
                }
            )
        )

    beta_frame = pd.concat(beta_frames, ignore_index=True)
    return output.merge(beta_frame, on=["symbol", "feature_time_utc"], how="left")


def _compute_final_scores(features: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
    output = features.copy()
    score_base = (
        0.25 * output["z_ret_3_m5"].fillna(0.0)
        + 0.25 * output["z_ret_12_m5"].fillna(0.0)
        + 0.20 * output["z_ema20_minus_ema80_over_atr"].fillna(0.0)
        + 0.15 * output["donchian_ensemble"].fillna(0.0)
        + 0.10 * output["z_ema20_slope_6_over_atr"].fillna(0.0)
    )
    output["trend_score_pre_volume"] = score_base
    output["volume_confirmation"] = np.select(
        [
            (output["volume_zscore"] >= 0.5) & (score_base > 0),
            (output["volume_zscore"] >= 0.5) & (score_base < 0),
        ],
        [1.0, -1.0],
        default=0.0,
    )
    output["trend_score"] = score_base + 0.05 * output["volume_confirmation"]
    crypto_relative_symbols = set(CRYPTO_SYMBOLS) - {BTC_SYMBOL}
    output["final_score_raw"] = np.where(
        output["symbol"].isin(crypto_relative_symbols),
        0.75 * output["trend_score"] + 0.25 * output["relative_score"].fillna(0.0),
        output["trend_score"],
    )
    output["shadow_final_score"] = _book_adjusted_score(
        output["final_score_raw"],
        output.get("book_imbalance"),
        config.book_imbalance_threshold,
    )
    output = _attach_cross_sectional_rank(output)
    return output


def _attach_cross_sectional_rank(features: pd.DataFrame) -> pd.DataFrame:
    output = features.copy()
    ranks = output.groupby("feature_time_utc")["final_score_raw"].rank(
        ascending=False,
        method="first",
    )
    counts = output.groupby("feature_time_utc")["final_score_raw"].transform("count")
    output["cross_sectional_rank"] = ranks
    output["cross_sectional_percentile"] = np.where(
        counts > 1,
        1.0 - ((ranks - 1.0) / (counts - 1.0)),
        1.0,
    )
    return output


def _finalize_feature_frame(features: pd.DataFrame) -> pd.DataFrame:
    output = features.sort_values(["feature_time_utc", "symbol"]).reset_index(drop=True)
    preferred_columns = [
        "symbol",
        "timeframe",
        "bar_time_utc",
        "feature_time_utc",
        "bar_index",
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
        "ret_1_m5",
        "ret_3_m5",
        "ret_12_m5",
        "ret_48_m5",
        "ret_15m",
        "ret_1h",
        "ret_4h",
        "z_ret_3_m5",
        "z_ret_12_m5",
        "ema_8",
        "ema_20",
        "ema_55",
        "ema_80",
        "ema20_minus_ema80_over_atr",
        "z_ema20_minus_ema80_over_atr",
        "ema20_slope_3_over_atr",
        "ema20_slope_6_over_atr",
        "z_ema20_slope_6_over_atr",
        "above_ema80",
        "donchian_12",
        "donchian_24",
        "donchian_48",
        "donchian_ensemble",
        "high_low_position_48",
        "atr_14",
        "rv_12_m5",
        "rv_24_m5",
        "rv_48_m5",
        "rv_1h_equiv",
        "shock_floor",
        "shock_flag",
        "volume_zscore",
        "volume_confirmation",
        "bid",
        "ask",
        "last",
        "spread",
        "spread_bps",
        "tick_time_utc",
        "tick_age_seconds",
        "book_time_utc",
        "top5_bid_volume",
        "top5_ask_volume",
        "book_imbalance",
        "m15_close",
        "m15_ret_1_bar",
        "h1_close",
        "h1_ret_1_bar",
        "btc_ret_1_m5",
        "btc_ret_3_m5",
        "btc_ret_12_m5",
        "btc_ret_48_m5",
        "btc_trend_score",
        "btc_above_ema80",
        "btc_regime",
        "beta_to_btc",
        "relative_ret_12_m5",
        "relative_score",
        "trend_score_pre_volume",
        "trend_score",
        "final_score_raw",
        "shadow_final_score",
        "cross_sectional_rank",
        "cross_sectional_percentile",
        "feature_warmup_complete",
        "feature_ready",
    ]
    other_columns = [column for column in output.columns if column not in preferred_columns]
    ordered_columns = [
        column for column in preferred_columns if column in output.columns
    ] + other_columns
    return output[ordered_columns]


def _ticks_to_frame(
    ticks: pd.DataFrame | Iterable[Mapping[str, Any]] | None,
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    if ticks is None:
        return pd.DataFrame()
    frame = _materialize_frame(ticks)
    if frame.empty:
        return frame
    if "time_utc" not in frame.columns:
        if "time_msc" in frame.columns:
            frame["time_utc"] = pd.to_numeric(frame["time_msc"], errors="coerce") / 1000
            frame["time_utc"] = pd.to_datetime(frame["time_utc"], unit="s", utc=True)
        elif "time" in frame.columns:
            frame["time_utc"] = frame["time"]
        else:
            raise FeatureEngineeringError("tick rows need time_utc, time_msc, or time")
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].map(normalize_symbol)
    frame = frame[frame["symbol"].isin(symbols)]
    frame["tick_time_utc"] = _parse_utc_series(frame["time_utc"])
    for column in ("bid", "ask", "last"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame["spread"] = (frame["ask"] - frame["bid"]).clip(lower=0.0)
    mid = (frame["ask"] + frame["bid"]) / 2.0
    frame["spread_bps"] = _safe_divide(frame["spread"], mid) * 10_000.0
    frame = frame.dropna(subset=["tick_time_utc"])
    frame = frame.sort_values(["symbol", "tick_time_utc"])
    return frame.drop_duplicates(["symbol", "tick_time_utc"], keep="last")


def _order_book_to_imbalance_frame(
    order_book_snapshots: pd.DataFrame | Iterable[Mapping[str, Any]] | None,
    symbols: tuple[str, ...],
) -> pd.DataFrame:
    if order_book_snapshots is None:
        return pd.DataFrame()
    frame = _materialize_frame(order_book_snapshots)
    if frame.empty:
        return frame
    required = {"symbol", "observed_at_utc", "side", "level"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FeatureEngineeringError(
            "order-book rows are missing required columns: " + ", ".join(missing)
        )

    frame = frame.copy()
    frame["symbol"] = frame["symbol"].map(normalize_symbol)
    frame = frame[frame["symbol"].isin(symbols)]
    frame["book_time_utc"] = _parse_utc_series(frame["observed_at_utc"])
    frame["side"] = frame["side"].astype(str).str.lower()
    frame["level"] = pd.to_numeric(frame["level"], errors="coerce")
    volume_source = frame["volume_dbl"] if "volume_dbl" in frame.columns else frame.get("volume")
    frame["book_volume"] = pd.to_numeric(volume_source, errors="coerce").fillna(0.0)
    frame = frame[(frame["side"].isin(("bid", "ask"))) & (frame["level"] <= 5)]
    grouped = (
        frame.groupby(["symbol", "book_time_utc", "side"], as_index=False)["book_volume"]
        .sum()
        .pivot(index=["symbol", "book_time_utc"], columns="side", values="book_volume")
        .reset_index()
        .rename_axis(None, axis=1)
    )
    if grouped.empty:
        return grouped
    grouped["top5_bid_volume"] = grouped.get("bid", 0.0)
    grouped["top5_ask_volume"] = grouped.get("ask", 0.0)
    total = grouped["top5_bid_volume"] + grouped["top5_ask_volume"]
    grouped["book_imbalance"] = _safe_divide(
        grouped["top5_bid_volume"] - grouped["top5_ask_volume"],
        total,
    )
    return grouped.sort_values(["symbol", "book_time_utc"])


def _merge_asof_by_symbol(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    right_time_column: str,
    value_columns: Sequence[str],
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, group in left.groupby("symbol", sort=False):
        right_group = right[right["symbol"] == symbol].sort_values(right_time_column)
        group = group.sort_values("feature_time_utc")
        if right_group.empty:
            merged = group.copy()
        else:
            right_columns = [
                "symbol",
                right_time_column,
                *[column for column in value_columns if column != right_time_column],
            ]
            merged = pd.merge_asof(
                group,
                right_group[right_columns],
                left_on="feature_time_utc",
                right_on=right_time_column,
                by="symbol",
                direction="backward",
            )
            if right_time_column not in value_columns and right_time_column in merged.columns:
                merged = merged.drop(columns=[right_time_column])
        parts.append(merged)
    if not parts:
        return left.copy()
    result = pd.concat(parts, ignore_index=True)
    for column in value_columns:
        if column not in result.columns:
            result[column] = np.nan
    return result


def _book_adjusted_score(
    final_score: pd.Series,
    book_imbalance: pd.Series | None,
    threshold: float,
) -> pd.Series:
    if book_imbalance is None:
        return final_score.copy()
    score_sign = np.sign(final_score.fillna(0.0))
    book_sign = np.sign(book_imbalance.fillna(0.0))
    adjustment = np.where(
        (book_imbalance.abs() >= threshold) & (score_sign != 0) & (book_sign == score_sign),
        0.10 * score_sign,
        np.where(
            (book_imbalance.abs() >= threshold)
            & (score_sign != 0)
            & (book_sign != 0)
            & (book_sign != score_sign),
            -0.10 * score_sign,
            0.0,
        ),
    )
    return final_score + adjustment


def _rolling_zscore(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    rolling = series.rolling(window=window, min_periods=min_periods)
    mean = rolling.mean()
    std = rolling.std(ddof=0)
    return _safe_divide(series - mean, std)


def _safe_divide(numerator: Any, denominator: Any) -> pd.Series:
    num = pd.Series(numerator) if not isinstance(numerator, pd.Series) else numerator
    if isinstance(denominator, pd.Series):
        den = denominator
    else:
        den = pd.Series(denominator, index=num.index)
    den = den.reindex(num.index)
    result = num / den.where(den.abs() > EPSILON)
    return result.replace([np.inf, -np.inf], np.nan)


def _materialize_frame(rows: pd.DataFrame | Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    if isinstance(rows, pd.DataFrame):
        return rows.copy()
    return pd.DataFrame([dict(row) for row in rows])


def _empty_bars_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "symbol",
            "timeframe",
            "time_utc",
            "open",
            "high",
            "low",
            "close",
            "tick_volume",
            "spread",
            "real_volume",
        ]
    )


def _empty_feature_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["symbol", "timeframe", "bar_time_utc", "feature_time_utc"])


def _to_utc_timestamp(value: datetime) -> pd.Timestamp:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return pd.Timestamp(value.astimezone(timezone.utc))


def _parse_utc_series(values: Any) -> pd.Series:
    """Parse mixed ISO timestamps from SQLite/MT5 into UTC pandas timestamps."""
    return pd.to_datetime(values, utc=True, format="ISO8601", errors="coerce")


def _iso_or_empty(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def default_feature_output_path(now_utc: datetime | None = None) -> Path:
    """Return a timestamped default feature export path."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Path("reports/features") / f"feature_snapshots_{timestamp}.csv"


__all__ = [
    "BTC_SYMBOL",
    "CORE_READY_COLUMNS",
    "FEATURE_TIMEFRAME",
    "SHOCK_FLOORS",
    "FeatureConfig",
    "FeatureEngineeringError",
    "compute_and_export_feature_snapshots",
    "compute_feature_snapshots",
    "compute_feature_snapshots_from_store",
    "default_feature_output_path",
    "latest_feature_snapshots",
    "write_feature_snapshots",
]
