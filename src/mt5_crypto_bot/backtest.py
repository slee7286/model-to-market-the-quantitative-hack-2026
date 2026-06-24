"""Offline M5/M15 backtesting and strategy comparison.

The backtester is deliberately independent of MT5 and execution code. It uses
completed feature snapshots, executes decisions on the next available M5 open,
and charges turnover-based spread plus slippage costs.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mt5_crypto_bot.constants import (
    ALLOWED_SYMBOLS,
    CRYPTO_SYMBOLS,
    DEFAULT_DATABASE_URL,
    DISCIPLINE_BALLAST_MAIN_SHARE,
    DISCIPLINE_BALLAST_MIN_TRIGGER_LEVERAGE,
    FOREX_SYMBOLS,
    PNL_SPRINT_ENTRY_SYMBOLS,
)
from mt5_crypto_bot.features import (
    FeatureConfig,
    FeatureEngineeringError,
    compute_feature_snapshots,
    compute_feature_snapshots_from_store,
)
from mt5_crypto_bot.schemas import normalize_symbols


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

DEFAULT_SPREAD_BPS: dict[str, float] = {
    "AUD/USD": 1.2,
    "BAR/USD": 15.0,
    "BTC/USD": 4.0,
    "EUR/CHF": 1.6,
    "EUR/GBP": 1.2,
    "EUR/USD": 0.8,
    "ETH/USD": 5.0,
    "GBP/USD": 1.2,
    "SOL/USD": 8.0,
    "USD/CAD": 1.2,
    "USD/CHF": 1.2,
    "USD/JPY": 0.9,
    "XRP/USD": 8.0,
}

SLIPPAGE_BPS: dict[str, float] = {
    "AUD/USD": 0.2,
    "BAR/USD": 5.0,
    "BTC/USD": 1.5,
    "EUR/CHF": 0.25,
    "EUR/GBP": 0.2,
    "EUR/USD": 0.15,
    "ETH/USD": 2.0,
    "GBP/USD": 0.25,
    "SOL/USD": 3.5,
    "USD/CAD": 0.2,
    "USD/CHF": 0.2,
    "USD/JPY": 0.15,
    "XRP/USD": 3.5,
}

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


class BacktestDataError(RuntimeError):
    """Raised when offline backtest data is unavailable or invalid."""


@dataclass(frozen=True)
class BacktestConfig:
    """Rules-aligned configuration for offline strategy comparison."""

    initial_equity: float = INITIAL_EQUITY
    entry_threshold: float = 1.25
    exit_threshold: float = 0.15
    max_gross_leverage: float = 28.0
    target_gross_leverage: float = 24.0
    max_margin_usage: float = 0.90
    sharpe_rule_min_observations: int = 8
    feature_config: FeatureConfig = FeatureConfig()


@dataclass(frozen=True)
class StrategySpec:
    """Offline deterministic strategy variant."""

    name: str
    display_name: str
    kind: str
    entry_threshold: float
    exit_threshold: float
    cap_multiplier: float = 1.0


@dataclass
class BacktestResult:
    """Backtest output for one strategy."""

    strategy_name: str
    display_name: str
    metrics: dict[str, Any]
    equity_curve: pd.DataFrame
    ledger: pd.DataFrame
    symbol_pnl: dict[str, float]
    side_pnl: dict[str, float]


@dataclass
class BacktestComparison:
    """Collection of strategy results and report metadata."""

    results: list[BacktestResult]
    selected_strategy: str
    best_metric_strategy: str
    data_label: str
    generated_at_utc: datetime
    is_fixture: bool = False


STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec(
        name="momo_v1",
        display_name="MVP momo_v1: BTC-regime volatility-managed momentum",
        kind="mvp",
        entry_threshold=1.25,
        exit_threshold=0.15,
    ),
    StrategySpec(
        name="volatility_managed_momentum",
        display_name="Challenger: volatility-managed time-series momentum",
        kind="vol_momentum",
        entry_threshold=1.00,
        exit_threshold=0.25,
        cap_multiplier=0.85,
    ),
    StrategySpec(
        name="donchian_trend_ensemble",
        display_name="Challenger: Donchian trend ensemble",
        kind="donchian",
        entry_threshold=0.55,
        exit_threshold=0.15,
        cap_multiplier=0.75,
    ),
    StrategySpec(
        name="intraday_reversal",
        display_name="Challenger: intraday reversal after overextension",
        kind="intraday_reversal",
        entry_threshold=1.25,
        exit_threshold=0.35,
        cap_multiplier=0.50,
    ),
)


def run_backtest_comparison(
    features: pd.DataFrame,
    *,
    config: BacktestConfig | None = None,
    data_label: str = "local_data",
    is_fixture: bool = False,
) -> BacktestComparison:
    """Run all supported strategy variants on precomputed feature snapshots."""
    backtest_config = config or BacktestConfig()
    prepared = _prepare_feature_frame(features)
    if prepared.empty:
        raise BacktestDataError("no feature rows are available for backtesting")

    results = [
        _run_single_strategy(prepared, spec, backtest_config)
        for spec in STRATEGY_SPECS
    ]
    best_metric_strategy = max(results, key=lambda result: _selection_score(result.metrics))
    selected_strategy = _select_live_mvp_strategy(results)
    return BacktestComparison(
        results=results,
        selected_strategy=selected_strategy,
        best_metric_strategy=best_metric_strategy.strategy_name,
        data_label=data_label,
        generated_at_utc=datetime.now(timezone.utc),
        is_fixture=is_fixture,
    )


def run_backtest_from_store(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    config: BacktestConfig | None = None,
    data_label: str = "sqlite_store",
) -> BacktestComparison:
    """Compute features from the local SQLite store and run comparisons."""
    backtest_config = config or BacktestConfig()
    symbols = normalize_symbols(target_symbols)
    try:
        features = compute_feature_snapshots_from_store(
            database_url,
            target_symbols=symbols,
            config=backtest_config.feature_config,
            require_data=True,
        )
    except FeatureEngineeringError as exc:
        raise BacktestDataError(str(exc)) from exc
    return run_backtest_comparison(features, config=backtest_config, data_label=data_label)


def run_backtest_from_csv(
    bars_csv_paths: Sequence[str | Path],
    *,
    ticks_csv_paths: Sequence[str | Path] | None = None,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    config: BacktestConfig | None = None,
    data_label: str = "csv_history",
) -> BacktestComparison:
    """Load organizer-style CSV bars/ticks, compute features, and compare strategies."""
    backtest_config = config or BacktestConfig()
    bars = _read_csv_paths(bars_csv_paths)
    ticks = _read_csv_paths(ticks_csv_paths or ())
    try:
        features = compute_feature_snapshots(
            bars,
            ticks=ticks,
            target_symbols=normalize_symbols(target_symbols),
            config=backtest_config.feature_config,
        )
    except FeatureEngineeringError as exc:
        raise BacktestDataError(str(exc)) from exc
    return run_backtest_comparison(features, config=backtest_config, data_label=data_label)


def write_backtest_reports(
    comparison: BacktestComparison,
    output_dir: str | Path = "reports/backtests",
    *,
    run_id: str | None = None,
) -> dict[str, Path]:
    """Write Markdown, summary CSV, equity CSV, and ledger CSV reports."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    timestamp = comparison.generated_at_utc.strftime("%Y%m%d_%H%M%S")
    stem = f"backtest_{run_id or timestamp}"

    summary_rows = [_summary_row(result) for result in comparison.results]
    summary = pd.DataFrame(summary_rows).sort_values(
        ["selection_score", "return"],
        ascending=False,
    )

    equity_parts: list[pd.DataFrame] = []
    ledger_parts: list[pd.DataFrame] = []
    for result in comparison.results:
        equity = result.equity_curve.copy()
        equity.insert(0, "strategy", result.strategy_name)
        equity_parts.append(equity)
        ledger = result.ledger.copy()
        ledger.insert(0, "strategy", result.strategy_name)
        ledger_parts.append(ledger)

    summary_path = root / f"{stem}_summary.csv"
    equity_path = root / f"{stem}_equity.csv"
    ledger_path = root / f"{stem}_ledger.csv"
    json_path = root / f"{stem}_metrics.json"
    markdown_path = root / f"{stem}.md"
    latest_path = root / "latest.md"

    summary.to_csv(summary_path, index=False)
    pd.concat(equity_parts, ignore_index=True).to_csv(equity_path, index=False)
    pd.concat(ledger_parts, ignore_index=True).to_csv(ledger_path, index=False)
    json_path.write_text(
        json.dumps(
            {
                "data_label": comparison.data_label,
                "is_fixture": comparison.is_fixture,
                "selected_strategy": comparison.selected_strategy,
                "best_metric_strategy": comparison.best_metric_strategy,
                "results": {result.strategy_name: result.metrics for result in comparison.results},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    markdown = _render_markdown_report(comparison, summary, summary_path, equity_path, ledger_path)
    markdown_path.write_text(markdown, encoding="utf-8")
    latest_path.write_text(markdown, encoding="utf-8")

    return {
        "markdown": markdown_path,
        "latest": latest_path,
        "summary_csv": summary_path,
        "equity_csv": equity_path,
        "ledger_csv": ledger_path,
        "metrics_json": json_path,
    }


def make_synthetic_fixture_market_data(
    *,
    symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    count: int = 260,
    start_utc: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build deterministic M5 bars/ticks for tests and no-data smoke validation."""
    canonical_symbols = normalize_symbols(symbols)
    start = start_utc or datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
    bases = {
        "AUD/USD": 0.66,
        "BAR/USD": 0.18,
        "BTC/USD": 65_000.0,
        "EUR/CHF": 0.94,
        "EUR/GBP": 0.84,
        "EUR/USD": 1.08,
        "ETH/USD": 3_500.0,
        "GBP/USD": 1.27,
        "SOL/USD": 145.0,
        "USD/CAD": 1.36,
        "USD/CHF": 0.88,
        "USD/JPY": 157.0,
        "XRP/USD": 0.55,
    }
    betas = {
        "AUD/USD": 0.22,
        "BAR/USD": 1.15,
        "BTC/USD": 1.00,
        "EUR/CHF": 0.10,
        "EUR/GBP": 0.12,
        "EUR/USD": 0.16,
        "ETH/USD": 1.10,
        "GBP/USD": 0.18,
        "SOL/USD": 1.45,
        "USD/CAD": -0.14,
        "USD/CHF": -0.12,
        "USD/JPY": 0.20,
        "XRP/USD": 0.95,
    }

    bars: list[dict[str, Any]] = []
    ticks: list[dict[str, Any]] = []
    for symbol in canonical_symbols:
        price = bases[symbol]
        previous_close = price
        for index in range(count):
            if index < 90:
                regime_return = 0.0012
            elif index < 165:
                regime_return = -0.0016
            else:
                regime_return = 0.0010
            idiosyncratic = 0.0018 * math.sin(index / (5.5 + len(symbol)))
            pulse = 0.0040 if index in {124, 198} else 0.0
            bar_return = betas[symbol] * regime_return + idiosyncratic + pulse
            close = max(previous_close * (1.0 + bar_return), bases[symbol] * 0.20)
            open_price = previous_close
            high = max(open_price, close) * (1.0 + 0.0015)
            low = min(open_price, close) * (1.0 - 0.0015)
            bar_time = start + timedelta(minutes=5 * index)
            spread_bps = DEFAULT_SPREAD_BPS[symbol]
            spread = close * spread_bps / 10_000.0
            bars.append(
                {
                    "symbol": symbol,
                    "timeframe": "M5",
                    "time_utc": bar_time,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "tick_volume": 100 + (index % 31) + int(abs(bar_return) * 20_000),
                    "spread": spread,
                    "real_volume": 1.0,
                    "source": "synthetic_fixture",
                }
            )
            ticks.append(
                {
                    "symbol": symbol,
                    "time_utc": bar_time + timedelta(minutes=5),
                    "bid": close - spread / 2.0,
                    "ask": close + spread / 2.0,
                    "last": close,
                    "volume": 1.0,
                    "volume_real": 1.0,
                    "source": "synthetic_fixture",
                }
            )
            previous_close = close
    return bars, ticks


def run_synthetic_fixture_backtest(
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    config: BacktestConfig | None = None,
) -> BacktestComparison:
    """Run the comparison on deterministic synthetic data for smoke validation."""
    backtest_config = config or BacktestConfig()
    bars, ticks = make_synthetic_fixture_market_data(symbols=target_symbols)
    try:
        features = compute_feature_snapshots(
            bars,
            ticks=ticks,
            target_symbols=normalize_symbols(target_symbols),
            config=backtest_config.feature_config,
        )
    except FeatureEngineeringError as exc:
        raise BacktestDataError(str(exc)) from exc
    return run_backtest_comparison(
        features,
        config=backtest_config,
        data_label="synthetic_fixture_no_live_market_evidence",
        is_fixture=True,
    )


def _run_single_strategy(
    features: pd.DataFrame,
    spec: StrategySpec,
    config: BacktestConfig,
) -> BacktestResult:
    ledgers = [
        _build_symbol_ledger(group, spec, config)
        for _, group in features.groupby("symbol", sort=True)
    ]
    ledger = pd.concat(ledgers, ignore_index=True)
    ledger = ledger.sort_values(["feature_time_utc", "symbol"]).reset_index(drop=True)
    ledger = _apply_discipline_ballast_targets(ledger, spec)
    ledger = _apply_portfolio_gross_cap(ledger, config.max_gross_leverage)
    ledger["net_return"] = ledger["gross_return"] - ledger["cost_return"]

    timeline = (
        ledger.groupby("feature_time_utc", as_index=False)
        .agg(
            portfolio_return=("net_return", "sum"),
            gross_return=("gross_return", "sum"),
            cost_return=("cost_return", "sum"),
            gross_exposure=("abs_position_leverage", "sum"),
            net_exposure=("position_leverage", "sum"),
            turnover=("turnover", "sum"),
        )
        .sort_values("feature_time_utc")
        .reset_index(drop=True)
    )
    timeline["equity_before"] = config.initial_equity * (
        1.0 + timeline["portfolio_return"].shift(1).fillna(0.0)
    ).cumprod()
    timeline["equity"] = config.initial_equity * (1.0 + timeline["portfolio_return"]).cumprod()
    timeline["drawdown"] = _drawdown(timeline["equity"])
    timeline["net_directional_share"] = _safe_divide(
        timeline["net_exposure"].abs(),
        timeline["gross_exposure"],
    ).fillna(0.0)
    timeline["margin_usage_proxy"] = timeline["gross_exposure"] / 30.0

    ledger = ledger.merge(
        timeline[["feature_time_utc", "equity_before"]],
        on="feature_time_utc",
        how="left",
    )
    ledger["pnl"] = ledger["net_return"] * ledger["equity_before"]
    ledger["gross_pnl"] = ledger["gross_return"] * ledger["equity_before"]
    ledger["cost_usd"] = ledger["cost_return"] * ledger["equity_before"]

    symbol_pnl = _float_dict(ledger.groupby("symbol")["pnl"].sum())
    side_pnl = _float_dict(ledger.groupby("pnl_side")["pnl"].sum())
    metrics = _compute_metrics(timeline, ledger, config)
    return BacktestResult(
        strategy_name=spec.name,
        display_name=spec.display_name,
        metrics=metrics,
        equity_curve=timeline,
        ledger=ledger,
        symbol_pnl=symbol_pnl,
        side_pnl=side_pnl,
    )


def _build_symbol_ledger(
    symbol_features: pd.DataFrame,
    spec: StrategySpec,
    config: BacktestConfig,
) -> pd.DataFrame:
    group = symbol_features.sort_values("feature_time_utc").reset_index(drop=True).copy()
    targets, scores, reasons = _stateful_targets(group, spec, config)
    group["signal_score"] = scores
    group["target_leverage"] = targets
    group["target_reason"] = reasons

    group["position_leverage"] = group["target_leverage"].shift(1).fillna(0.0)
    group["previous_position_leverage"] = group["position_leverage"].shift(1).fillna(0.0)
    group["turnover"] = (
        group["position_leverage"] - group["previous_position_leverage"]
    ).abs()
    group["abs_position_leverage"] = group["position_leverage"].abs()
    group["open_return_next"] = group["open"].shift(-1) / group["open"] - 1.0
    group["open_return_next"] = group["open_return_next"].replace([np.inf, -np.inf], np.nan)
    group["gross_return"] = group["position_leverage"] * group["open_return_next"].fillna(0.0)

    group["effective_spread_bps"] = _effective_spread_bps(group)
    group["slippage_bps"] = group["symbol"].map(SLIPPAGE_BPS).astype(float)
    group["half_spread_bps"] = group["effective_spread_bps"] / 2.0
    group["cost_bps"] = group["half_spread_bps"] + group["slippage_bps"]
    group["cost_return"] = group["turnover"] * group["cost_bps"] / 10_000.0
    group["pnl_side"] = np.select(
        [
            group["position_leverage"] > 0,
            group["position_leverage"] < 0,
            (group["position_leverage"] == 0) & (group["previous_position_leverage"] > 0),
            (group["position_leverage"] == 0) & (group["previous_position_leverage"] < 0),
        ],
        ["long", "short", "long", "short"],
        default="flat",
    )
    return group[
        [
            "symbol",
            "feature_time_utc",
            "open",
            "close",
            "signal_score",
            "target_leverage",
            "target_reason",
            "position_leverage",
            "previous_position_leverage",
            "turnover",
            "abs_position_leverage",
            "open_return_next",
            "gross_return",
            "effective_spread_bps",
            "slippage_bps",
            "cost_return",
            "pnl_side",
        ]
    ]


def _apply_portfolio_gross_cap(ledger: pd.DataFrame, max_gross_leverage: float) -> pd.DataFrame:
    """Scale simultaneous target vectors so gross exposure never exceeds the cap."""

    capped = ledger.copy()
    target_gross = capped.groupby("feature_time_utc")["target_leverage"].transform(
        lambda series: float(series.abs().sum())
    )
    scale = np.where(
        target_gross > max_gross_leverage,
        max_gross_leverage / target_gross.replace(0.0, np.nan),
        1.0,
    )
    scale = pd.Series(scale, index=capped.index).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    scaled = scale < 1.0 - EPSILON
    capped["target_leverage"] = capped["target_leverage"] * scale
    capped.loc[scaled, "target_reason"] = (
        capped.loc[scaled, "target_reason"].astype(str) + "; portfolio gross cap scaled"
    )
    return _recompute_ledger_from_targets(capped)


def _apply_discipline_ballast_targets(
    ledger: pd.DataFrame,
    spec: StrategySpec,
) -> pd.DataFrame:
    """Approximate live discipline ballast in offline MVP target vectors."""

    if spec.kind != "mvp" or ledger.empty:
        return ledger
    adjusted = ledger.copy()
    for _, group in adjusted.groupby("feature_time_utc", sort=True):
        sprint_group = group[group["symbol"].isin(PNL_SPRINT_ENTRY_SYMBOLS)]
        active = sprint_group[
            sprint_group["target_leverage"].abs() >= DISCIPLINE_BALLAST_MIN_TRIGGER_LEVERAGE
        ]
        if len(active) != 1:
            continue
        main_idx = active.index[0]
        main_symbol = str(adjusted.at[main_idx, "symbol"])
        original_target = float(adjusted.at[main_idx, "target_leverage"])
        candidates = sprint_group[
            (sprint_group["symbol"] != main_symbol)
            & (sprint_group["target_leverage"].abs() <= EPSILON)
            & ~sprint_group["target_reason"].astype(str).str.startswith("flat:")
        ].copy()
        if candidates.empty:
            continue
        candidates["spread_sort"] = candidates["effective_spread_bps"].replace(
            [np.inf, -np.inf],
            np.nan,
        ).fillna(math.inf)
        ballast_idx = candidates.sort_values(["spread_sort", "symbol"]).index[0]
        ballast_target = -math.copysign(
            abs(original_target) * (1.0 - DISCIPLINE_BALLAST_MAIN_SHARE),
            original_target,
        )
        adjusted.at[main_idx, "target_leverage"] = (
            original_target * DISCIPLINE_BALLAST_MAIN_SHARE
        )
        adjusted.at[main_idx, "target_reason"] = (
            str(adjusted.at[main_idx, "target_reason"])
            + "; discipline ballast main share"
        )
        adjusted.at[ballast_idx, "target_leverage"] = ballast_target
        adjusted.at[ballast_idx, "target_reason"] = (
            f"discipline ballast for {main_symbol} concentration/net exposure"
        )
    return _recompute_ledger_from_targets(adjusted)


def _recompute_ledger_from_targets(ledger: pd.DataFrame) -> pd.DataFrame:
    """Recompute next-open positions, turnover, returns, and costs after target scaling."""

    group = ledger.sort_values(["symbol", "feature_time_utc"]).reset_index(drop=True).copy()
    group["position_leverage"] = group.groupby("symbol")["target_leverage"].shift(1).fillna(0.0)
    group["previous_position_leverage"] = (
        group.groupby("symbol")["position_leverage"].shift(1).fillna(0.0)
    )
    group["turnover"] = (
        group["position_leverage"] - group["previous_position_leverage"]
    ).abs()
    group["abs_position_leverage"] = group["position_leverage"].abs()
    group["gross_return"] = group["position_leverage"] * group["open_return_next"].fillna(0.0)
    group["cost_return"] = (
        group["turnover"]
        * (group["effective_spread_bps"] / 2.0 + group["slippage_bps"])
        / 10_000.0
    )
    group["pnl_side"] = np.select(
        [
            group["position_leverage"] > 0,
            group["position_leverage"] < 0,
            (group["position_leverage"] == 0) & (group["previous_position_leverage"] > 0),
            (group["position_leverage"] == 0) & (group["previous_position_leverage"] < 0),
        ],
        ["long", "short", "long", "short"],
        default="flat",
    )
    return group.sort_values(["feature_time_utc", "symbol"]).reset_index(drop=True)


def _stateful_targets(
    group: pd.DataFrame,
    spec: StrategySpec,
    config: BacktestConfig,
) -> tuple[list[float], list[float], list[str]]:
    targets: list[float] = []
    scores: list[float] = []
    reasons: list[str] = []
    current_target = 0.0
    for _, row in group.iterrows():
        symbol = str(row["symbol"])
        score = _strategy_score(row, spec)
        scores.append(float(score) if np.isfinite(score) else 0.0)

        if not _row_is_tradeable(row):
            current_target = 0.0
            targets.append(current_target)
            reasons.append("flat: feature row not tradeable")
            continue
        if _spread_too_wide(row):
            current_target = 0.0
            targets.append(current_target)
            reasons.append("flat: spread cap")
            continue

        exit_reason = _exit_reason(row, score, current_target, spec)
        if exit_reason is not None:
            current_target = 0.0
            targets.append(current_target)
            reasons.append(exit_reason)
            continue

        if spec.kind == "mvp" and symbol not in PNL_SPRINT_ENTRY_SYMBOLS:
            targets.append(current_target)
            reasons.append("hold: PnL sprint disables new entries for symbol")
            continue

        long_gate = _long_gate(row, score, spec)
        short_gate = _short_gate(row, score, spec)
        if score >= spec.entry_threshold and long_gate:
            current_target = _target_leverage(row, score, spec, config)
            targets.append(current_target)
            reasons.append("long entry/resize")
        elif score <= -spec.entry_threshold and short_gate:
            current_target = -_target_leverage(row, score, spec, config)
            targets.append(current_target)
            reasons.append("short entry/resize")
        else:
            targets.append(current_target)
            reasons.append("hold")
    return targets, scores, reasons


def _strategy_score(row: pd.Series, spec: StrategySpec) -> float:
    if spec.kind == "mvp":
        return _as_float(row.get("final_score_raw"))
    if spec.kind == "vol_momentum":
        return (
            0.40 * _as_float(row.get("z_ret_12_m5"))
            + 0.25 * _as_float(row.get("z_ret_3_m5"))
            + 0.20 * _as_float(row.get("z_ema20_minus_ema80_over_atr"))
            + 0.15 * _as_float(row.get("z_ema20_slope_6_over_atr"))
        )
    if spec.kind == "donchian":
        return 1.5 * _as_float(row.get("donchian_ensemble"))
    if spec.kind == "intraday_reversal":
        return -_as_float(row.get("z_ret_3_m5"))
    raise BacktestDataError(f"unknown strategy kind: {spec.kind}")


def _row_is_tradeable(row: pd.Series) -> bool:
    return bool(row.get("feature_ready", False)) and not bool(row.get("shock_flag", False))


def _spread_too_wide(row: pd.Series) -> bool:
    spread_bps = _spread_bps_for_row(row)
    cap = SPREAD_CAP_BPS[str(row["symbol"])]
    return bool(np.isfinite(spread_bps) and spread_bps > cap)


def _long_gate(row: pd.Series, score: float, spec: StrategySpec) -> bool:
    del score
    if spec.kind != "intraday_reversal" and _as_float(row.get("close")) <= _as_float(row.get("ema_80")):
        return False
    if spec.kind != "intraday_reversal" and _as_float(row.get("ema20_slope_6_over_atr")) < 0:
        return False
    if spec.kind == "intraday_reversal" and _as_float(row.get("close")) < _as_float(row.get("ema_80")):
        return False
    return _btc_regime_gate(row, "long")


def _short_gate(row: pd.Series, score: float, spec: StrategySpec) -> bool:
    del score
    if spec.kind != "intraday_reversal" and _as_float(row.get("close")) >= _as_float(row.get("ema_80")):
        return False
    if spec.kind != "intraday_reversal" and _as_float(row.get("ema20_slope_6_over_atr")) > 0:
        return False
    if spec.kind == "intraday_reversal" and _as_float(row.get("close")) > _as_float(row.get("ema_80")):
        return False
    return _btc_regime_gate(row, "short")


def _btc_regime_gate(row: pd.Series, side: str) -> bool:
    symbol = str(row["symbol"])
    if symbol == "BTC/USD" or symbol not in CRYPTO_SYMBOLS:
        return True
    btc_regime = str(row.get("btc_regime", "unknown"))
    btc_trend = _as_float(row.get("btc_trend_score"))
    if symbol in {"ETH/USD", "SOL/USD"}:
        return not ((side == "long" and btc_regime == "risk_off") or (side == "short" and btc_regime == "risk_on"))
    if side == "long":
        return btc_trend > -0.25
    return btc_trend < 0.25


def _exit_reason(
    row: pd.Series,
    score: float,
    current_target: float,
    spec: StrategySpec,
) -> str | None:
    if abs(current_target) <= EPSILON:
        return None
    spread_bps = _spread_bps_for_row(row)
    if np.isfinite(spread_bps) and spread_bps > 1.5 * SPREAD_CAP_BPS[str(row["symbol"])]:
        return "exit: abnormal spread"
    close = _as_float(row.get("close"))
    ema20 = _as_float(row.get("ema_20"))
    if current_target > 0 and score <= spec.exit_threshold:
        return "exit: score faded"
    if current_target < 0 and score >= -spec.exit_threshold:
        return "exit: score faded"
    if current_target > 0 and close < ema20:
        return "exit: close below ema20"
    if current_target < 0 and close > ema20:
        return "exit: close above ema20"
    return None


def _target_leverage(
    row: pd.Series,
    score: float,
    spec: StrategySpec,
    config: BacktestConfig,
) -> float:
    symbol = str(row["symbol"])
    abs_score = abs(float(score))
    denominator = max(2.25 - spec.entry_threshold, EPSILON)
    score_scale = min(max((abs_score - spec.entry_threshold) / denominator, 0.0), 1.0)
    rv_raw = row.get("rv_1h_equiv")
    try:
        rv = float(rv_raw)
    except (TypeError, ValueError):
        rv = None
    if rv is not None and not np.isfinite(rv):
        rv = None
    vol_scale = _vol_scale(symbol, rv)
    cap = NORMAL_SYMBOL_LEVERAGE_CAP[symbol] * spec.cap_multiplier
    hard_cap = HARD_SYMBOL_LEVERAGE_CAP[symbol] * spec.cap_multiplier
    target = cap * (0.35 + 0.65 * score_scale) * vol_scale
    return min(max(target, 0.0), hard_cap, config.max_gross_leverage)


def _vol_scale(symbol: str, rv_1h_equiv: float | None) -> float:
    if rv_1h_equiv is None or not np.isfinite(rv_1h_equiv):
        return 0.75
    floor = VOLATILITY_FLOOR[symbol]
    target = TARGET_RV_1H[symbol]
    return min(max(target / max(float(rv_1h_equiv), floor), 0.35), 1.25)


def _prepare_feature_frame(features: pd.DataFrame) -> pd.DataFrame:
    if features.empty:
        return features.copy()
    required = {
        "symbol",
        "feature_time_utc",
        "open",
        "close",
        "feature_ready",
        "final_score_raw",
        "ema_20",
        "ema_80",
        "ema20_slope_6_over_atr",
        "donchian_ensemble",
        "z_ret_3_m5",
        "z_ret_12_m5",
        "z_ema20_minus_ema80_over_atr",
        "z_ema20_slope_6_over_atr",
    }
    missing = sorted(required - set(features.columns))
    if missing:
        raise BacktestDataError("feature rows are missing required columns: " + ", ".join(missing))
    frame = features.copy()
    frame["symbol"] = frame["symbol"].astype(str).map(lambda value: normalize_symbols((value,))[0])
    frame["feature_time_utc"] = pd.to_datetime(frame["feature_time_utc"], utc=True)
    for column in ("open", "close", "spread_bps", "rv_1h_equiv"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values(["symbol", "feature_time_utc"]).reset_index(drop=True)


def _effective_spread_bps(frame: pd.DataFrame) -> pd.Series:
    actual = pd.to_numeric(frame.get("spread_bps"), errors="coerce")
    default = frame["symbol"].map(DEFAULT_SPREAD_BPS).astype(float)
    return actual.where(actual.notna() & (actual > 0), default)


def _spread_bps_for_row(row: pd.Series) -> float:
    value = _as_float(row.get("spread_bps"))
    if np.isfinite(value) and value > 0:
        return value
    return DEFAULT_SPREAD_BPS[str(row["symbol"])]


def _compute_metrics(
    timeline: pd.DataFrame,
    ledger: pd.DataFrame,
    config: BacktestConfig,
) -> dict[str, Any]:
    if timeline.empty:
        raise BacktestDataError("strategy timeline is empty")
    total_return = float(timeline["equity"].iloc[-1] / config.initial_equity - 1.0)
    max_drawdown = float(timeline["drawdown"].max())
    sharpe, sharpe_observations = _sharpe_15m(timeline)
    trade_count = int((ledger["turnover"] > EPSILON).sum())
    max_symbol_share = _max_single_symbol_share(ledger)
    max_gross = float(timeline["gross_exposure"].max())
    max_margin_usage_proxy = float(timeline["margin_usage_proxy"].max())
    risk_status = "pass"
    risk_notes: list[str] = []
    if max_gross > config.max_gross_leverage:
        risk_status = "fail"
        risk_notes.append("gross leverage above internal cap")
    if max_margin_usage_proxy > config.max_margin_usage:
        risk_status = "fail"
        risk_notes.append("margin usage proxy above internal cap")
    if max_symbol_share > 0.75:
        risk_status = "warn"
        risk_notes.append("single-symbol share above target hard cap")
    if float(timeline["net_directional_share"].max()) > 0.85:
        risk_status = "warn" if risk_status == "pass" else risk_status
        risk_notes.append("net directional share above internal hard cap")

    return {
        "return": total_return,
        "max_drawdown": max_drawdown,
        "sharpe_15m": sharpe,
        "sharpe_15m_observations": sharpe_observations,
        "trade_count": trade_count,
        "average_gross_exposure": float(timeline["gross_exposure"].mean()),
        "max_gross_exposure": max_gross,
        "average_net_directional_share": float(timeline["net_directional_share"].mean()),
        "max_net_directional_share": float(timeline["net_directional_share"].max()),
        "max_single_symbol_share": max_symbol_share,
        "turnover": float(timeline["turnover"].sum()),
        "estimated_cost_usd": float(ledger["cost_usd"].sum()),
        "estimated_cost_return": float(timeline["cost_return"].sum()),
        "max_margin_usage_proxy": max_margin_usage_proxy,
        "risk_discipline_estimate": risk_status,
        "risk_notes": "; ".join(risk_notes) if risk_notes else "within internal caps",
        "selection_score": float(_selection_score_from_values(total_return, max_drawdown, sharpe, timeline["turnover"].sum())),
    }


def _sharpe_15m(timeline: pd.DataFrame) -> tuple[float, int]:
    indexed = timeline.set_index("feature_time_utc")["equity"].sort_index()
    equity_15m = indexed.resample("15min").last().dropna()
    returns = equity_15m.pct_change().dropna()
    observations = int(len(returns))
    if observations == 0:
        return 0.0, 0
    std = float(returns.std(ddof=0))
    if std <= EPSILON:
        return 0.0, observations
    return float(returns.mean() / std), observations


def _max_single_symbol_share(ledger: pd.DataFrame) -> float:
    exposure = ledger.assign(abs_exposure=ledger["position_leverage"].abs())
    totals = exposure.groupby("feature_time_utc")["abs_exposure"].transform("sum")
    share = exposure["abs_exposure"] / totals.where(totals > EPSILON)
    return float(share.fillna(0.0).max())


def _drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return ((peak - equity) / peak.where(peak > EPSILON)).fillna(0.0)


def _select_live_mvp_strategy(results: Sequence[BacktestResult]) -> str:
    del results
    return "momo_v1"


def _selection_score(metrics: Mapping[str, Any]) -> float:
    return _selection_score_from_values(
        float(metrics["return"]),
        float(metrics["max_drawdown"]),
        float(metrics["sharpe_15m"]),
        float(metrics["turnover"]),
    )


def _selection_score_from_values(
    total_return: float,
    max_drawdown: float,
    sharpe: float,
    turnover: float,
) -> float:
    return total_return - 2.0 * max_drawdown + 0.05 * sharpe - 0.0005 * turnover


def _summary_row(result: BacktestResult) -> dict[str, Any]:
    row = {
        "strategy": result.strategy_name,
        "display_name": result.display_name,
    }
    row.update(result.metrics)
    for symbol, pnl in sorted(result.symbol_pnl.items()):
        row[f"pnl_{symbol.replace('/', '')}"] = pnl
    for side, pnl in sorted(result.side_pnl.items()):
        row[f"pnl_{side}"] = pnl
    return row


def _render_markdown_report(
    comparison: BacktestComparison,
    summary: pd.DataFrame,
    summary_path: Path,
    equity_path: Path,
    ledger_path: Path,
) -> str:
    lines: list[str] = [
        "# Backtest Strategy Comparison",
        "",
        f"Generated UTC: `{comparison.generated_at_utc.isoformat()}`",
        f"Data label: `{comparison.data_label}`",
        f"Fixture data: `{str(comparison.is_fixture).lower()}`",
        "",
        "## Safety Scope",
        "",
        "- Offline backtest only; no MT5 connection, no order construction, no live orders.",
        "- Allowed symbols only: active FX/crypto symbols from `rules.md` and `constants.py`.",
        "- Signals are evaluated on completed M5 feature rows and executed on the next M5 open.",
        "- Costs subtract half-spread plus symbol-specific slippage on every notional turnover.",
        "",
        "## Strategy Selection",
        "",
        f"Selected live MVP strategy remains `{comparison.selected_strategy}`.",
        f"Best metric strategy in this run was `{comparison.best_metric_strategy}`.",
        "",
    ]
    if comparison.is_fixture:
        lines.extend(
            [
                "This report uses deterministic synthetic fixture data because no local collected",
                "market database or organizer history was available. It validates mechanics only",
                "and is not performance evidence for live trading.",
                "",
            ]
        )
    lines.extend(
        [
            "Challengers are shadow candidates only. No strategy promotion is automatic; a future",
            "human-approved workflow must review real backtest and dry-run evidence first.",
            "",
            "## Summary Metrics",
            "",
            _markdown_table(
                summary[
                    [
                        "strategy",
                        "return",
                        "max_drawdown",
                        "sharpe_15m",
                        "sharpe_15m_observations",
                        "trade_count",
                        "average_gross_exposure",
                        "max_gross_exposure",
                        "turnover",
                        "estimated_cost_usd",
                        "risk_discipline_estimate",
                    ]
                ]
            ),
            "",
            "## Symbol And Side Attribution",
            "",
        ]
    )
    for result in comparison.results:
        lines.extend(
            [
                f"### {result.strategy_name}",
                "",
                "Symbol PnL:",
                "",
                _dict_table(result.symbol_pnl, "symbol", "pnl_usd"),
                "",
                "Side PnL:",
                "",
                _dict_table(result.side_pnl, "side", "pnl_usd"),
                "",
            ]
        )
    lines.extend(
        [
            "## Artifacts",
            "",
            f"- Summary CSV: `{summary_path}`",
            f"- Equity CSV: `{equity_path}`",
            f"- Ledger CSV: `{ledger_path}`",
            "",
            "## Rule-Aligned Notes",
            "",
            "- Return uses account equity change from the USD 1,000,000 initial balance.",
            "- Max drawdown uses peak-to-trough equity drawdown.",
            "- Sharpe is a non-annualized 15-minute account-equity return approximation.",
            "- Margin usage is approximated as gross leverage divided by the 30x account cap.",
            "- Risk discipline status is an internal estimate, not an official competition ruling.",
        ]
    )
    return "\n".join(lines) + "\n"


def _markdown_table(frame: pd.DataFrame) -> str:
    formatted = frame.copy()
    for column in formatted.columns:
        if pd.api.types.is_float_dtype(formatted[column]):
            formatted[column] = formatted[column].map(lambda value: f"{value:.6g}")
    columns = [str(column) for column in formatted.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for _, row in formatted.iterrows():
        values = [str(row[column]).replace("\n", " ") for column in formatted.columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _dict_table(values: Mapping[str, float], key_name: str, value_name: str) -> str:
    if not values:
        return f"| {key_name} | {value_name} |\n| --- | --- |"
    frame = pd.DataFrame(
        [{key_name: key, value_name: f"{value:.2f}"} for key, value in sorted(values.items())]
    )
    return _markdown_table(frame)


def _read_csv_paths(paths: Sequence[str | Path]) -> pd.DataFrame:
    if not paths:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for path_value in paths:
        path = Path(path_value)
        if not path.exists():
            raise BacktestDataError(f"CSV file not found: {path}")
        frames.append(pd.read_csv(path))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _float_dict(series: pd.Series) -> dict[str, float]:
    return {str(key): float(value) for key, value in series.items()}


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(number):
        return 0.0
    return number


def _safe_divide(numerator: Any, denominator: Any) -> pd.Series:
    num = pd.Series(numerator) if not isinstance(numerator, pd.Series) else numerator
    den = pd.Series(denominator, index=num.index) if not isinstance(denominator, pd.Series) else denominator
    den = den.reindex(num.index)
    return num / den.where(den.abs() > EPSILON)


__all__ = [
    "BacktestComparison",
    "BacktestConfig",
    "BacktestDataError",
    "BacktestResult",
    "INITIAL_EQUITY",
    "STRATEGY_SPECS",
    "StrategySpec",
    "make_synthetic_fixture_market_data",
    "run_backtest_comparison",
    "run_backtest_from_csv",
    "run_backtest_from_store",
    "run_synthetic_fixture_backtest",
    "write_backtest_reports",
]
