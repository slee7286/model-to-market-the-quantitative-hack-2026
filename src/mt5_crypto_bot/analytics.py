"""Offline analytics and continuous-improvement reports.

This module reads the local audit database only. It never contacts MT5, never
places orders, and never activates proposed strategy parameters. Parameter
proposals are stored as inactive strategy versions for human review.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from mt5_crypto_bot.backtest import BacktestDataError, run_backtest_from_store
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_DATABASE_URL, DEFAULT_STRATEGY_VERSION
from mt5_crypto_bot.retune import (
    ParameterProposal,
    generate_coarse_parameter_proposals,
    manual_approval_workflow_markdown,
    store_parameter_proposals,
)
from mt5_crypto_bot.schemas import normalize_symbols
from mt5_crypto_bot.storage import SQLiteStore


INITIAL_EQUITY = 1_000_000.0
EPSILON = 1e-12


class AnalyticsError(RuntimeError):
    """Raised when analytics input or output cannot be produced."""


@dataclass(frozen=True)
class AnalyticsConfig:
    """Configuration for offline metric calculation."""

    initial_equity: float = INITIAL_EQUITY
    sharpe_interval_minutes: int = 15
    sharpe_rule_min_observations: int = 8
    output_dir: str = "reports/analytics"


@dataclass(frozen=True)
class AnalyticsReport:
    """Complete offline analytics report payload."""

    generated_at_utc: datetime
    data_label: str
    metrics: dict[str, Any]
    equity_curve: pd.DataFrame
    symbol_attribution: pd.DataFrame
    side_attribution: pd.DataFrame
    signal_bucket_performance: pd.DataFrame
    spread_slippage_proxy: dict[str, Any]
    reject_block_reasons: pd.DataFrame
    champion_challenger: dict[str, Any]
    parameter_proposals: tuple[ParameterProposal, ...] = ()
    manual_approval_workflow: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def generate_analytics_report_from_store(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    config: AnalyticsConfig | None = None,
    store_proposals: bool = True,
    include_shadow_evaluation: bool = True,
    now_utc: datetime | None = None,
) -> AnalyticsReport:
    """Generate an offline analytics report from SQLite audit tables."""

    analytics_config = config or AnalyticsConfig()
    symbols = normalize_symbols(target_symbols)
    generated_at = _datetime_from_any(now_utc) if now_utc else datetime.now(timezone.utc)

    with SQLiteStore(database_url) as store:
        frames = _load_analytics_frames(store, symbols)
        equity_curve = build_equity_curve(
            frames["account_snapshots"],
            frames["fills"],
            generated_at_utc=generated_at,
            config=analytics_config,
        )
        metrics = compute_performance_metrics(
            equity_curve,
            frames["fills"],
            frames["orders"],
            config=analytics_config,
        )
        symbol_attribution = compute_symbol_attribution(
            frames["signals"], frames["orders"], frames["fills"], symbols
        )
        side_attribution = compute_side_attribution(frames["orders"], frames["fills"])
        signal_buckets = compute_signal_bucket_performance(
            frames["signals"], frames["orders"], frames["fills"]
        )
        spread_proxy = compute_spread_slippage_proxy(frames["signals"], frames["orders"], frames["fills"])
        reject_reasons = compute_reject_block_reasons(
            frames["signals"], frames["risk_checks"], frames["orders"]
        )
        champion_challenger = (
            evaluate_champion_challengers(database_url, symbols)
            if include_shadow_evaluation
            else {"available": False, "reason": "shadow evaluation skipped"}
        )
        proposals = generate_coarse_parameter_proposals(
            metrics=metrics,
            signal_bucket_rows=_frame_records(signal_buckets),
            reject_reason_rows=_frame_records(reject_reasons),
            generated_at_utc=generated_at,
        )
        if store_proposals and proposals:
            store_parameter_proposals(store, proposals)
            metrics["inactive_parameter_proposals_stored"] = len(proposals)
        else:
            metrics["inactive_parameter_proposals_stored"] = 0

    notes = _report_notes(frames, champion_challenger)
    return AnalyticsReport(
        generated_at_utc=generated_at,
        data_label=f"sqlite:{database_url}",
        metrics=metrics,
        equity_curve=equity_curve,
        symbol_attribution=symbol_attribution,
        side_attribution=side_attribution,
        signal_bucket_performance=signal_buckets,
        spread_slippage_proxy=spread_proxy,
        reject_block_reasons=reject_reasons,
        champion_challenger=champion_challenger,
        parameter_proposals=tuple(proposals),
        manual_approval_workflow=manual_approval_workflow_markdown(),
        notes=notes,
    )


def build_equity_curve(
    account_snapshots: pd.DataFrame,
    fills: pd.DataFrame,
    *,
    generated_at_utc: datetime,
    config: AnalyticsConfig | None = None,
) -> pd.DataFrame:
    """Build a rule-aligned equity curve from account snapshots or fills."""

    analytics_config = config or AnalyticsConfig()
    if not account_snapshots.empty:
        frame = account_snapshots.copy()
        frame["time_utc"] = pd.to_datetime(frame["observed_at_utc"], utc=True)
        frame["equity"] = pd.to_numeric(frame["equity"], errors="coerce")
        frame = frame.dropna(subset=["time_utc", "equity"]).sort_values("time_utc")
        if not frame.empty:
            start_time = frame["time_utc"].iloc[0] - pd.Timedelta(
                minutes=analytics_config.sharpe_interval_minutes
            )
            start = pd.DataFrame(
                [{"time_utc": start_time, "equity": analytics_config.initial_equity}]
            )
            return (
                pd.concat([start, frame[["time_utc", "equity"]]], ignore_index=True)
                .drop_duplicates("time_utc", keep="last")
                .sort_values("time_utc")
                .reset_index(drop=True)
            )

    if not fills.empty:
        frame = fills.copy()
        frame["time_utc"] = pd.to_datetime(frame["time_utc"], utc=True)
        frame["profit"] = pd.to_numeric(frame["profit"], errors="coerce").fillna(0.0)
        frame = frame.dropna(subset=["time_utc"]).sort_values("time_utc")
        if not frame.empty:
            grouped = frame.groupby("time_utc", as_index=False)["profit"].sum()
            grouped["equity"] = analytics_config.initial_equity + grouped["profit"].cumsum()
            start_time = grouped["time_utc"].iloc[0] - pd.Timedelta(
                minutes=analytics_config.sharpe_interval_minutes
            )
            start = pd.DataFrame(
                [{"time_utc": start_time, "equity": analytics_config.initial_equity}]
            )
            return pd.concat(
                [start, grouped[["time_utc", "equity"]]], ignore_index=True
            ).reset_index(drop=True)

    return pd.DataFrame(
        [{"time_utc": pd.Timestamp(generated_at_utc), "equity": analytics_config.initial_equity}]
    )


def compute_performance_metrics(
    equity_curve: pd.DataFrame,
    fills: pd.DataFrame,
    orders: pd.DataFrame,
    *,
    config: AnalyticsConfig | None = None,
) -> dict[str, Any]:
    """Compute return, drawdown, 15-minute Sharpe, and trade-count metrics."""

    analytics_config = config or AnalyticsConfig()
    curve = equity_curve.copy()
    if curve.empty:
        raise AnalyticsError("equity curve is empty")
    curve["equity"] = pd.to_numeric(curve["equity"], errors="coerce")
    curve = curve.dropna(subset=["equity"]).sort_values("time_utc")
    if curve.empty:
        raise AnalyticsError("equity curve has no numeric equity values")

    final_equity = float(curve["equity"].iloc[-1])
    total_return = final_equity / analytics_config.initial_equity - 1.0
    max_drawdown = float(_drawdown(curve["equity"]).max())
    sharpe, sharpe_observations = _sharpe_15m(curve, analytics_config)

    real_fill_count = int(len(fills))
    dry_run_order_count = (
        int((orders["status"].astype(str) == "dry_run").sum())
        if not orders.empty and "status" in orders
        else 0
    )
    filled_order_count = (
        int(orders["status"].astype(str).isin(["filled", "partial"]).sum())
        if not orders.empty and "status" in orders
        else 0
    )
    trade_count = real_fill_count if real_fill_count > 0 else dry_run_order_count + filled_order_count

    return {
        "initial_equity": analytics_config.initial_equity,
        "final_equity": final_equity,
        "return": float(total_return),
        "max_drawdown": max_drawdown,
        "sharpe_15m": sharpe,
        "sharpe_15m_observations": sharpe_observations,
        "sharpe_rank_cap_applies": sharpe_observations < analytics_config.sharpe_rule_min_observations,
        "trade_count": int(trade_count),
        "real_fill_count": real_fill_count,
        "dry_run_order_count": dry_run_order_count,
        "filled_order_count": filled_order_count,
    }


def compute_symbol_attribution(
    signals: pd.DataFrame,
    orders: pd.DataFrame,
    fills: pd.DataFrame,
    symbols: Sequence[str],
) -> pd.DataFrame:
    """Aggregate PnL, dry-run activity, and signal counts by symbol."""

    base = pd.DataFrame({"symbol": list(symbols)})
    signal_counts = _count_by(signals, "symbol", "signal_count")
    order_counts = _count_by(orders, "symbol", "order_count")
    dry_counts = _count_by(
        orders[orders["status"].astype(str) == "dry_run"] if not orders.empty else orders,
        "symbol",
        "dry_run_order_count",
    )
    if not fills.empty:
        fill_frame = fills.copy()
        fill_frame["profit"] = pd.to_numeric(fill_frame["profit"], errors="coerce").fillna(0.0)
        pnl = fill_frame.groupby("symbol", as_index=False).agg(
            fill_count=("profit", "size"),
            pnl_usd=("profit", "sum"),
        )
    else:
        pnl = pd.DataFrame(columns=["symbol", "fill_count", "pnl_usd"])

    frame = (
        base.merge(signal_counts, on="symbol", how="left")
        .merge(order_counts, on="symbol", how="left")
        .merge(dry_counts, on="symbol", how="left")
        .merge(pnl, on="symbol", how="left")
    )
    for column in ("signal_count", "order_count", "dry_run_order_count", "fill_count"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(int)
    frame["pnl_usd"] = pd.to_numeric(frame["pnl_usd"], errors="coerce").fillna(0.0)
    return frame.sort_values("symbol").reset_index(drop=True)


def compute_side_attribution(orders: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    """Aggregate dry-run/order counts and realized fill PnL by side."""

    order_counts = _count_by(orders, "side", "order_count")
    dry_counts = _count_by(
        orders[orders["status"].astype(str) == "dry_run"] if not orders.empty else orders,
        "side",
        "dry_run_order_count",
    )
    if not fills.empty:
        fill_frame = fills.copy()
        fill_frame["profit"] = pd.to_numeric(fill_frame["profit"], errors="coerce").fillna(0.0)
        pnl = fill_frame.groupby("side", as_index=False).agg(
            fill_count=("profit", "size"),
            pnl_usd=("profit", "sum"),
        )
    else:
        pnl = pd.DataFrame(columns=["side", "fill_count", "pnl_usd"])
    sides = sorted(
        set(order_counts.get("side", pd.Series(dtype=str)).dropna().astype(str))
        | set(pnl.get("side", pd.Series(dtype=str)).dropna().astype(str))
        | {"buy", "sell"}
    )
    frame = pd.DataFrame({"side": sides})
    frame = frame.merge(order_counts, on="side", how="left").merge(
        dry_counts, on="side", how="left"
    ).merge(pnl, on="side", how="left")
    for column in ("order_count", "dry_run_order_count", "fill_count"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(int)
    frame["pnl_usd"] = pd.to_numeric(frame["pnl_usd"], errors="coerce").fillna(0.0)
    return frame.sort_values("side").reset_index(drop=True)


def compute_signal_bucket_performance(
    signals: pd.DataFrame,
    orders: pd.DataFrame,
    fills: pd.DataFrame,
) -> pd.DataFrame:
    """Measure activity and realized PnL by signal-score bucket."""

    if signals.empty:
        return pd.DataFrame(
            columns=[
                "score_bucket",
                "signal_count",
                "enter_count",
                "block_count",
                "order_count",
                "dry_run_order_count",
                "fill_count",
                "pnl_usd",
            ]
        )
    signal_frame = signals.copy()
    signal_frame["score"] = pd.to_numeric(signal_frame["score"], errors="coerce").fillna(0.0)
    signal_frame["score_bucket"] = signal_frame["score"].map(bucket_signal_score)

    order_counts = (
        orders.groupby("signal_id", as_index=False).agg(
            order_count=("client_order_id", "size"),
            dry_run_order_count=("status", lambda values: int((values.astype(str) == "dry_run").sum())),
        )
        if not orders.empty
        else pd.DataFrame(columns=["signal_id", "order_count", "dry_run_order_count"])
    )
    fill_by_signal = _fills_with_signal_id(orders, fills)
    if not fill_by_signal.empty:
        fill_pnl = fill_by_signal.groupby("signal_id", as_index=False).agg(
            fill_count=("profit", "size"),
            pnl_usd=("profit", "sum"),
        )
    else:
        fill_pnl = pd.DataFrame(columns=["signal_id", "fill_count", "pnl_usd"])

    joined = signal_frame.merge(order_counts, on="signal_id", how="left").merge(
        fill_pnl, on="signal_id", how="left"
    )
    for column in ("order_count", "dry_run_order_count", "fill_count"):
        joined[column] = pd.to_numeric(joined[column], errors="coerce").fillna(0).astype(int)
    joined["pnl_usd"] = pd.to_numeric(joined["pnl_usd"], errors="coerce").fillna(0.0)
    joined["enter_flag"] = joined["decision"].astype(str) == "enter"
    joined["block_flag"] = joined["decision"].astype(str) == "block"

    grouped = joined.groupby("score_bucket", as_index=False).agg(
        signal_count=("signal_id", "size"),
        enter_count=("enter_flag", "sum"),
        block_count=("block_flag", "sum"),
        order_count=("order_count", "sum"),
        dry_run_order_count=("dry_run_order_count", "sum"),
        fill_count=("fill_count", "sum"),
        pnl_usd=("pnl_usd", "sum"),
    )
    grouped["avg_pnl_per_fill"] = grouped.apply(
        lambda row: float(row["pnl_usd"]) / row["fill_count"] if row["fill_count"] else 0.0,
        axis=1,
    )
    grouped["score_bucket"] = pd.Categorical(
        grouped["score_bucket"],
        categories=[
            "strong_short",
            "weak_short",
            "neutral",
            "weak_long",
            "strong_long",
        ],
        ordered=True,
    )
    return grouped.sort_values("score_bucket").reset_index(drop=True)


def bucket_signal_score(score: float) -> str:
    """Map signal scores into stable diagnostic buckets."""

    value = float(score) if math.isfinite(float(score)) else 0.0
    if value <= -1.5:
        return "strong_short"
    if value <= -0.35:
        return "weak_short"
    if value < 0.35:
        return "neutral"
    if value < 1.5:
        return "weak_long"
    return "strong_long"


def compute_spread_slippage_proxy(
    signals: pd.DataFrame,
    orders: pd.DataFrame,
    fills: pd.DataFrame,
) -> dict[str, Any]:
    """Estimate spread and slippage diagnostics from stored dry-run/fill data."""

    feature_frame = _signal_features(signals)
    joined_orders = orders.merge(
        feature_frame[["signal_id", "spread_bps"]],
        on="signal_id",
        how="left",
    ) if not orders.empty else pd.DataFrame(columns=["spread_bps"])
    spread_values = pd.to_numeric(joined_orders.get("spread_bps"), errors="coerce").dropna()

    fill_frame = _fills_with_orders(orders, fills)
    if not fill_frame.empty:
        fill_frame["slippage_bps"] = pd.to_numeric(fill_frame.get("slippage_bps"), errors="coerce")
        missing_slippage = fill_frame["slippage_bps"].isna()
        if {"price", "requested_price"}.issubset(fill_frame.columns):
            requested = pd.to_numeric(fill_frame["requested_price"], errors="coerce")
            filled = pd.to_numeric(fill_frame["price"], errors="coerce")
            estimated = (filled - requested).abs() / requested.where(requested.abs() > EPSILON) * 10_000
            fill_frame.loc[missing_slippage, "slippage_bps"] = estimated[missing_slippage]
        slippage_values = fill_frame["slippage_bps"].dropna()
    else:
        slippage_values = pd.Series(dtype=float)

    requested_notional_proxy = 0.0
    if not joined_orders.empty:
        requested_notional_proxy = float(
            (
                pd.to_numeric(joined_orders.get("requested_volume"), errors="coerce").fillna(0.0)
                * pd.to_numeric(joined_orders.get("requested_price"), errors="coerce").fillna(0.0)
            ).sum()
        )

    return {
        "order_count_with_spread": int(len(spread_values)),
        "average_spread_bps": _series_mean(spread_values),
        "max_spread_bps": _series_max(spread_values),
        "average_half_spread_cost_bps": _series_mean(spread_values / 2.0),
        "fill_count_with_slippage": int(len(slippage_values)),
        "average_slippage_bps": _series_mean(slippage_values),
        "max_slippage_bps": _series_max(slippage_values),
        "requested_notional_proxy": requested_notional_proxy,
    }


def compute_reject_block_reasons(
    signals: pd.DataFrame,
    risk_checks: pd.DataFrame,
    orders: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate strategy block, risk reject, and order reject reasons."""

    rows: list[dict[str, Any]] = []
    if not signals.empty:
        blocked = signals[signals["decision"].astype(str) == "block"]
        for reason, count in blocked["reason"].fillna("unspecified").value_counts().items():
            rows.append({"source": "signal_block", "reason": str(reason), "count": int(count)})
    if not risk_checks.empty:
        failed = risk_checks[~risk_checks["passed"].astype(bool)]
        for reason, count in failed["reason"].fillna("unspecified").value_counts().items():
            rows.append({"source": "risk_reject", "reason": str(reason), "count": int(count)})
    if not orders.empty:
        rejected = orders[orders["status"].astype(str).isin(["rejected", "failed"])]
        for status, count in rejected["status"].fillna("unknown").value_counts().items():
            rows.append({"source": "order_status", "reason": str(status), "count": int(count)})
    if not rows:
        return pd.DataFrame(columns=["source", "reason", "count"])
    return pd.DataFrame(rows).sort_values(["source", "count"], ascending=[True, False]).reset_index(drop=True)


def evaluate_champion_challengers(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
) -> dict[str, Any]:
    """Run stored-data champion/challenger shadow evaluation when possible."""

    symbols = normalize_symbols(target_symbols)
    try:
        comparison = run_backtest_from_store(database_url, target_symbols=symbols)
    except BacktestDataError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "champion": DEFAULT_STRATEGY_VERSION,
            "promotion_allowed": False,
        }

    rows = []
    for result in comparison.results:
        rows.append(
            {
                "strategy": result.strategy_name,
                "return": float(result.metrics["return"]),
                "max_drawdown": float(result.metrics["max_drawdown"]),
                "sharpe_15m": float(result.metrics["sharpe_15m"]),
                "trade_count": int(result.metrics["trade_count"]),
                "risk_discipline_estimate": result.metrics["risk_discipline_estimate"],
            }
        )
    return {
        "available": True,
        "champion": comparison.selected_strategy,
        "best_metric_strategy": comparison.best_metric_strategy,
        "promotion_allowed": False,
        "manual_approval_required": True,
        "results": rows,
    }


def write_analytics_reports(
    report: AnalyticsReport,
    output_dir: str | Path = "reports/analytics",
    *,
    run_id: str | None = None,
) -> dict[str, Path]:
    """Write Markdown/CSV/JSON analytics artifacts."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    timestamp = report.generated_at_utc.strftime("%Y%m%d_%H%M%S")
    stem = f"analytics_{run_id or timestamp}"

    markdown_path = root / f"{stem}.md"
    latest_path = root / "latest.md"
    metrics_path = root / f"{stem}_metrics.json"
    equity_path = root / f"{stem}_equity.csv"
    symbol_path = root / f"{stem}_symbol_attribution.csv"
    side_path = root / f"{stem}_side_attribution.csv"
    bucket_path = root / f"{stem}_signal_buckets.csv"
    reasons_path = root / f"{stem}_reject_reasons.csv"
    proposals_path = root / f"{stem}_parameter_proposals.json"

    report.equity_curve.to_csv(equity_path, index=False)
    report.symbol_attribution.to_csv(symbol_path, index=False)
    report.side_attribution.to_csv(side_path, index=False)
    report.signal_bucket_performance.to_csv(bucket_path, index=False)
    report.reject_block_reasons.to_csv(reasons_path, index=False)
    metrics_path.write_text(
        json.dumps(_report_json_payload(report), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    proposals_path.write_text(
        json.dumps(
            [proposal.to_record() for proposal in report.parameter_proposals],
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    markdown = _render_markdown_report(
        report,
        metrics_path=metrics_path,
        equity_path=equity_path,
        symbol_path=symbol_path,
        side_path=side_path,
        bucket_path=bucket_path,
        reasons_path=reasons_path,
        proposals_path=proposals_path,
    )
    markdown_path.write_text(markdown, encoding="utf-8")
    latest_path.write_text(markdown, encoding="utf-8")
    return {
        "markdown": markdown_path,
        "latest": latest_path,
        "metrics_json": metrics_path,
        "equity_csv": equity_path,
        "symbol_attribution_csv": symbol_path,
        "side_attribution_csv": side_path,
        "signal_buckets_csv": bucket_path,
        "reject_reasons_csv": reasons_path,
        "parameter_proposals_json": proposals_path,
    }


def _load_analytics_frames(store: SQLiteStore, symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    placeholders = ",".join("?" for _ in symbols)
    return {
        "account_snapshots": _fetch_frame(
            store,
            "SELECT * FROM account_snapshots ORDER BY observed_at_utc",
        ),
        "fills": _fetch_frame(
            store,
            f"SELECT * FROM fills WHERE symbol IN ({placeholders}) ORDER BY time_utc",
            symbols,
        ),
        "orders": _fetch_frame(
            store,
            f"SELECT * FROM orders WHERE symbol IN ({placeholders}) ORDER BY submitted_at_utc",
            symbols,
        ),
        "signals": _fetch_frame(
            store,
            f"SELECT * FROM signals WHERE symbol IN ({placeholders}) ORDER BY created_at_utc",
            symbols,
        ),
        "risk_checks": _fetch_frame(
            store,
            f"""
            SELECT *
            FROM risk_checks
            WHERE symbol IS NULL OR symbol IN ({placeholders})
            ORDER BY checked_at_utc
            """,
            symbols,
        ),
    }


def _fetch_frame(
    store: SQLiteStore,
    sql: str,
    parameters: Sequence[Any] = (),
) -> pd.DataFrame:
    rows = store.fetch_all(sql, parameters)
    return pd.DataFrame([dict(row) for row in rows])


def _drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return ((peak - equity) / peak.where(peak > EPSILON)).fillna(0.0)


def _sharpe_15m(curve: pd.DataFrame, config: AnalyticsConfig) -> tuple[float, int]:
    frame = curve.copy()
    frame["time_utc"] = pd.to_datetime(frame["time_utc"], utc=True)
    indexed = frame.set_index("time_utc")["equity"].sort_index()
    equity_15m = indexed.resample(f"{config.sharpe_interval_minutes}min").last().dropna()
    returns = equity_15m.pct_change().dropna()
    observations = int(len(returns))
    if observations == 0:
        return 0.0, 0
    std = float(returns.std(ddof=0))
    if std <= EPSILON:
        return 0.0, observations
    return float(returns.mean() / std), observations


def _count_by(frame: pd.DataFrame, column: str, output_name: str) -> pd.DataFrame:
    if frame.empty or column not in frame:
        return pd.DataFrame(columns=[column, output_name])
    return frame.groupby(column, as_index=False).size().rename(columns={"size": output_name})


def _signal_features(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame(columns=["signal_id", "spread_bps"])
    rows: list[dict[str, Any]] = []
    for _, row in signals.iterrows():
        try:
            features = json.loads(row.get("features_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            features = {}
        rows.append(
            {
                "signal_id": row.get("signal_id"),
                "spread_bps": _as_float_or_none(features.get("spread_bps")),
                "feature_time_utc": features.get("feature_time_utc"),
            }
        )
    return pd.DataFrame(rows)


def _fills_with_orders(orders: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    if orders.empty or fills.empty:
        return pd.DataFrame()
    fill_frame = fills.copy()
    order_frame = orders.copy()
    joined_parts: list[pd.DataFrame] = []
    if "order_ticket" in fill_frame and "mt5_order_ticket" in order_frame:
        by_order = fill_frame.merge(
            order_frame[["signal_id", "side", "requested_price", "mt5_order_ticket"]],
            left_on="order_ticket",
            right_on="mt5_order_ticket",
            how="left",
        )
        joined_parts.append(by_order)
    if "deal_ticket" in fill_frame and "mt5_deal_ticket" in order_frame:
        by_deal = fill_frame.merge(
            order_frame[["signal_id", "side", "requested_price", "mt5_deal_ticket"]],
            left_on="deal_ticket",
            right_on="mt5_deal_ticket",
            how="left",
        )
        joined_parts.append(by_deal)
    if not joined_parts:
        return fill_frame
    joined = pd.concat(joined_parts, ignore_index=True)
    if "fill_id" in joined:
        joined = joined.drop_duplicates("fill_id", keep="first")
    return joined


def _fills_with_signal_id(orders: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    joined = _fills_with_orders(orders, fills)
    if joined.empty:
        return pd.DataFrame(columns=["signal_id", "profit"])
    joined["profit"] = pd.to_numeric(joined.get("profit"), errors="coerce").fillna(0.0)
    return joined.dropna(subset=["signal_id"])


def _series_mean(series: pd.Series) -> float | None:
    return None if series.empty else float(series.mean())


def _series_max(series: pd.Series) -> float | None:
    return None if series.empty else float(series.max())


def _as_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return [
        {str(key): _json_safe(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]


def _report_json_payload(report: AnalyticsReport) -> dict[str, Any]:
    return {
        "generated_at_utc": report.generated_at_utc.isoformat(),
        "data_label": report.data_label,
        "metrics": report.metrics,
        "spread_slippage_proxy": report.spread_slippage_proxy,
        "champion_challenger": report.champion_challenger,
        "parameter_proposals": [proposal.to_record() for proposal in report.parameter_proposals],
        "notes": list(report.notes),
    }


def _render_markdown_report(
    report: AnalyticsReport,
    *,
    metrics_path: Path,
    equity_path: Path,
    symbol_path: Path,
    side_path: Path,
    bucket_path: Path,
    reasons_path: Path,
    proposals_path: Path,
) -> str:
    metrics = report.metrics
    lines: list[str] = [
        "# Offline Analytics Report",
        "",
        f"Generated UTC: `{report.generated_at_utc.isoformat()}`",
        f"Data label: `{report.data_label}`",
        "",
        "## Safety Scope",
        "",
        "- Offline analytics only; no MT5 connection and no live orders.",
        "- Proposed parameters are stored as inactive strategy versions only.",
        "- No leverage, margin, symbol-cap, or risk-limit increase is proposed automatically.",
        "- Manual approval is required before any strategy version can become active.",
        "",
        "## Core Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Return | {_format_percent(metrics['return'])} |",
        f"| Max drawdown | {_format_percent(metrics['max_drawdown'])} |",
        f"| 15-minute Sharpe | {metrics['sharpe_15m']:.6g} |",
        f"| 15-minute observations | {metrics['sharpe_15m_observations']} |",
        f"| Trade count | {metrics['trade_count']} |",
        f"| Real fill count | {metrics['real_fill_count']} |",
        f"| Dry-run order count | {metrics['dry_run_order_count']} |",
        "",
        "## Attribution",
        "",
        "Symbol attribution:",
        "",
        _markdown_table(report.symbol_attribution),
        "",
        "Side attribution:",
        "",
        _markdown_table(report.side_attribution),
        "",
        "## Signal Buckets",
        "",
        _markdown_table(report.signal_bucket_performance),
        "",
        "## Spread And Slippage Proxy",
        "",
        _dict_markdown_table(report.spread_slippage_proxy),
        "",
        "## Reject And Block Reasons",
        "",
        _markdown_table(report.reject_block_reasons),
        "",
        "## Champion/Challenger Shadow Evaluation",
        "",
        _dict_markdown_table(report.champion_challenger, flatten_results=True),
        "",
        "No challenger promotion is automatic. Any promotion requires real-data validation",
        "and a separate manual approval workflow.",
        "",
        "## Inactive Parameter Proposals",
        "",
    ]
    if report.parameter_proposals:
        proposal_rows = pd.DataFrame([proposal.to_record() for proposal in report.parameter_proposals])
        lines.append(_markdown_table(proposal_rows[["strategy_version", "parent_strategy_version", "rationale", "active", "requires_manual_approval"]]))
    else:
        lines.append("No parameter proposals were generated.")
    lines.extend(
        [
            "",
            "## Manual Approval Workflow",
            "",
            report.manual_approval_workflow,
            "",
            "## Notes",
            "",
        ]
    )
    lines.extend(f"- {note}" for note in report.notes)
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            f"- Metrics JSON: `{metrics_path}`",
            f"- Equity CSV: `{equity_path}`",
            f"- Symbol attribution CSV: `{symbol_path}`",
            f"- Side attribution CSV: `{side_path}`",
            f"- Signal bucket CSV: `{bucket_path}`",
            f"- Reject reason CSV: `{reasons_path}`",
            f"- Parameter proposal JSON: `{proposals_path}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| field | value |\n| --- | --- |"
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


def _dict_markdown_table(values: Mapping[str, Any], *, flatten_results: bool = False) -> str:
    rows: list[dict[str, Any]] = []
    for key, value in values.items():
        if flatten_results and key == "results" and isinstance(value, list):
            for item in value:
                rows.append({"field": f"result:{item.get('strategy')}", "value": json.dumps(item, sort_keys=True)})
            continue
        rows.append({"field": key, "value": _json_safe(value)})
    return _markdown_table(pd.DataFrame(rows))


def _format_percent(value: float) -> str:
    return f"{100.0 * float(value):.4f}%"


def _report_notes(frames: Mapping[str, pd.DataFrame], champion_challenger: Mapping[str, Any]) -> tuple[str, ...]:
    notes: list[str] = []
    if frames["fills"].empty:
        notes.append("No real fill rows were found; realized PnL attribution is zero until fills are imported.")
    if frames["orders"].empty:
        notes.append("No order rows were found; run dry-run execution before using order diagnostics.")
    if not champion_challenger.get("available"):
        notes.append("Champion/challenger shadow evaluation was unavailable: " + str(champion_challenger.get("reason")))
    return tuple(notes)


def _datetime_from_any(value: Any) -> datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    raise AnalyticsError(f"cannot parse datetime value: {value!r}")


def _json_safe(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return str(value)


__all__ = [
    "AnalyticsConfig",
    "AnalyticsError",
    "AnalyticsReport",
    "INITIAL_EQUITY",
    "bucket_signal_score",
    "build_equity_curve",
    "compute_performance_metrics",
    "compute_reject_block_reasons",
    "compute_side_attribution",
    "compute_signal_bucket_performance",
    "compute_spread_slippage_proxy",
    "compute_symbol_attribution",
    "evaluate_champion_challengers",
    "generate_analytics_report_from_store",
    "write_analytics_reports",
]
