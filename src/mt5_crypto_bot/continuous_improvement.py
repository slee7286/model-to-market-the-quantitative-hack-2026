"""Safe continuous-improvement orchestration for the MT5 FX/crypto bot.

The loop is deliberately offline and approval-gated. It reads the local audit
database, writes reports and inactive proposals, and never connects to MT5,
never places orders, never edits runtime configuration, and never activates a
strategy version.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_crypto_bot.analytics import (
    AnalyticsConfig,
    AnalyticsError,
    AnalyticsReport,
    generate_analytics_report_from_store,
    write_analytics_reports,
)
from mt5_crypto_bot.backtest import (
    BacktestComparison,
    BacktestDataError,
    run_backtest_from_store,
    write_backtest_reports,
)
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_DATABASE_URL
from mt5_crypto_bot.schemas import StrategyParams, normalize_symbols
from mt5_crypto_bot.storage import SQLiteStore
from mt5_crypto_bot.thresholds import (
    ThresholdRecommendation,
    recommend_thresholds_from_store,
)


class ContinuousImprovementError(RuntimeError):
    """Raised when the offline improvement loop cannot produce its report."""


@dataclass(frozen=True)
class ContinuousImprovementConfig:
    """Configuration for a safe read-only improvement pass."""

    output_dir: str | Path = "reports/continuous_improvement"
    run_id: str | None = None
    store_analytics_proposals: bool = True
    store_threshold_candidate: bool = True
    include_shadow_backtest: bool = True
    write_backtest_artifacts: bool = True
    start_time_utc: datetime | None = None
    end_time_utc: datetime | None = None


@dataclass(frozen=True)
class ContinuousImprovementReport:
    """Outputs and decisions from one improvement pass."""

    generated_at_utc: datetime
    data_label: str
    safety: dict[str, Any]
    metrics: dict[str, Any]
    threshold_recommendation: ThresholdRecommendation
    analytics_report: AnalyticsReport
    backtest_summary: dict[str, Any]
    inactive_threshold_candidate: dict[str, Any] | None
    paths: dict[str, Path] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Return a compact JSON-safe summary for terminal output."""

        return {
            "generated_at_utc": self.generated_at_utc.isoformat(),
            "data_label": self.data_label,
            "safety": self.safety,
            "metrics": self.metrics,
            "threshold_recommendation": self.threshold_recommendation.as_dict(),
            "backtest_summary": self.backtest_summary,
            "inactive_threshold_candidate": self.inactive_threshold_candidate,
            "paths": {key: str(value) for key, value in self.paths.items()},
        }


def run_continuous_improvement_from_store(
    database_url: str | Path = DEFAULT_DATABASE_URL,
    *,
    target_symbols: Sequence[str] | str | None = ALLOWED_SYMBOLS,
    base_params: StrategyParams,
    config: ContinuousImprovementConfig | None = None,
    now_utc: datetime | None = None,
) -> ContinuousImprovementReport:
    """Run the safe continuous-improvement loop from local SQLite data only."""

    improvement_config = config or ContinuousImprovementConfig()
    symbols = normalize_symbols(target_symbols)
    generated_at = _datetime_from_any(now_utc) if now_utc else datetime.now(timezone.utc)
    run_id = improvement_config.run_id or generated_at.strftime("%Y%m%d_%H%M%S")
    root = Path(improvement_config.output_dir)
    root.mkdir(parents=True, exist_ok=True)

    try:
        analytics = generate_analytics_report_from_store(
            database_url,
            target_symbols=symbols,
            config=AnalyticsConfig(
                start_time_utc=improvement_config.start_time_utc,
                end_time_utc=improvement_config.end_time_utc,
            ),
            store_proposals=improvement_config.store_analytics_proposals,
            include_shadow_evaluation=improvement_config.include_shadow_backtest,
            now_utc=generated_at,
        )
    except AnalyticsError as exc:
        raise ContinuousImprovementError(str(exc)) from exc

    analytics_paths = write_analytics_reports(
        analytics,
        root / "analytics",
        run_id=run_id,
    )
    threshold = recommend_thresholds_from_store(
        database_url,
        target_symbols=symbols,
        current_entry_threshold=base_params.entry_threshold,
        current_exit_threshold=base_params.exit_threshold,
        start_time_utc=improvement_config.start_time_utc,
        end_time_utc=improvement_config.end_time_utc,
    )
    threshold_candidate = (
        _store_inactive_threshold_candidate(
            database_url,
            recommendation=threshold,
            base_params=base_params,
            generated_at_utc=generated_at,
        )
        if improvement_config.store_threshold_candidate
        else None
    )
    backtest_summary: dict[str, Any]
    backtest_paths: dict[str, Path] = {}
    if improvement_config.include_shadow_backtest and improvement_config.write_backtest_artifacts:
        backtest_summary, backtest_paths = _run_backtest_artifacts(
            database_url,
            target_symbols=symbols,
            output_dir=root / "backtests",
            run_id=run_id,
        )
    elif improvement_config.include_shadow_backtest:
        backtest_summary = _analytics_backtest_summary(analytics)
    else:
        backtest_summary = {
            "available": False,
            "reason": "shadow backtest skipped by configuration",
            "promotion_allowed": False,
        }

    candidate_env_path = _write_candidate_env(
        root,
        run_id=run_id,
        recommendation=threshold,
        candidate=threshold_candidate,
    )
    summary_path = root / f"continuous_improvement_{run_id}_summary.json"
    markdown_path = root / f"continuous_improvement_{run_id}.md"
    latest_path = root / "latest.md"
    safety = _safety_payload()
    report = ContinuousImprovementReport(
        generated_at_utc=generated_at,
        data_label=f"sqlite:{database_url}",
        safety=safety,
        metrics=dict(analytics.metrics),
        threshold_recommendation=threshold,
        analytics_report=analytics,
        backtest_summary=backtest_summary,
        inactive_threshold_candidate=threshold_candidate,
        paths={
            "markdown": markdown_path,
            "latest": latest_path,
            "summary_json": summary_path,
            "candidate_env": candidate_env_path,
            **{f"analytics_{key}": value for key, value in analytics_paths.items()},
            **{f"backtest_{key}": value for key, value in backtest_paths.items()},
        },
    )
    summary_path.write_text(
        json.dumps(_json_safe(report.summary()), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    markdown = _render_improvement_markdown(report)
    markdown_path.write_text(markdown, encoding="utf-8")
    latest_path.write_text(markdown, encoding="utf-8")
    return report


def _store_inactive_threshold_candidate(
    database_url: str | Path,
    *,
    recommendation: ThresholdRecommendation,
    base_params: StrategyParams,
    generated_at_utc: datetime,
) -> dict[str, Any] | None:
    if not recommendation.available:
        return None
    if (
        recommendation.recommended_entry_threshold == base_params.entry_threshold
        and recommendation.recommended_exit_threshold == base_params.exit_threshold
    ):
        return None
    candidate = base_params.model_copy(
        update={
            "entry_threshold": recommendation.recommended_entry_threshold,
            "exit_threshold": recommendation.recommended_exit_threshold,
        }
    )
    version = _candidate_version(candidate, generated_at_utc)
    params = candidate.model_dump(mode="json")
    params.update(
        {
            "strategy_version": version,
            "parent_strategy_version": base_params.strategy_version,
            "proposal_status": "inactive_manual_review_required",
            "proposal_source": "continuous_improvement_threshold_recommendation",
            "requires_manual_approval": True,
            "promotion_allowed": False,
            "generated_at_utc": generated_at_utc.astimezone(timezone.utc).isoformat(),
            "threshold_recommendation": recommendation.as_dict(),
        }
    )
    with SQLiteStore(database_url) as store:
        store.upsert_strategy_version(params, active=False)
    return {
        "strategy_version": version,
        "parent_strategy_version": base_params.strategy_version,
        "entry_threshold": candidate.entry_threshold,
        "exit_threshold": candidate.exit_threshold,
        "active": False,
        "requires_manual_approval": True,
        "promotion_allowed": False,
    }


def _run_backtest_artifacts(
    database_url: str | Path,
    *,
    target_symbols: tuple[str, ...],
    output_dir: str | Path,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    try:
        comparison = run_backtest_from_store(database_url, target_symbols=target_symbols)
    except BacktestDataError as exc:
        return (
            {
                "available": False,
                "reason": str(exc),
                "promotion_allowed": False,
            },
            {},
        )
    paths = write_backtest_reports(comparison, output_dir, run_id=run_id)
    return _backtest_summary(comparison), paths


def _analytics_backtest_summary(report: AnalyticsReport) -> dict[str, Any]:
    payload = dict(report.champion_challenger)
    payload["promotion_allowed"] = False
    payload["manual_approval_required"] = True
    return payload


def _backtest_summary(comparison: BacktestComparison) -> dict[str, Any]:
    rows = []
    for result in comparison.results:
        rows.append(
            {
                "strategy": result.strategy_name,
                "return": result.metrics.get("return"),
                "max_drawdown": result.metrics.get("max_drawdown"),
                "sharpe_15m": result.metrics.get("sharpe_15m"),
                "trade_count": result.metrics.get("trade_count"),
                "selection_score": result.metrics.get("selection_score"),
                "risk_discipline_estimate": result.metrics.get("risk_discipline_estimate"),
            }
        )
    return {
        "available": True,
        "selected_strategy": comparison.selected_strategy,
        "best_metric_strategy": comparison.best_metric_strategy,
        "promotion_allowed": False,
        "manual_approval_required": True,
        "results": rows,
    }


def _write_candidate_env(
    root: Path,
    *,
    run_id: str,
    recommendation: ThresholdRecommendation,
    candidate: Mapping[str, Any] | None,
) -> Path:
    path = root / f"continuous_improvement_{run_id}_candidate.env"
    lines = [
        "# Review-only candidate generated by the offline continuous-improvement loop.",
        "# This file is not loaded automatically and does not change live behavior.",
        "# Apply only after manual review, validation, and a separate approval decision.",
        f"ENTRY_THRESHOLD={recommendation.recommended_entry_threshold:g}",
        f"EXIT_THRESHOLD={recommendation.recommended_exit_threshold:g}",
        "",
        f"# recommendation_available={str(recommendation.available).lower()}",
        f"# reason={recommendation.reason}",
        f"# inactive_strategy_version={candidate.get('strategy_version') if candidate else 'none'}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _safety_payload() -> dict[str, Any]:
    return {
        "offline_only": True,
        "mt5_connection": False,
        "order_check_called": False,
        "order_send_called": False,
        "live_config_modified": False,
        "strategy_auto_promoted": False,
        "manual_approval_required": True,
        "allowed_symbols_only": list(ALLOWED_SYMBOLS),
        "rules_priority": "rules.md remains the highest-priority source of truth",
    }


def _render_improvement_markdown(report: ContinuousImprovementReport) -> str:
    threshold = report.threshold_recommendation
    metrics = report.metrics
    paths = report.paths
    lines = [
        "# Continuous Improvement Report",
        "",
        f"Generated UTC: `{report.generated_at_utc.isoformat()}`",
        f"Data label: `{report.data_label}`",
        "",
        "## Safety Contract",
        "",
        "- Offline SQLite analysis only; no MT5 connection was opened.",
        "- No `order_check`, `order_send`, or live order placement can happen in this loop.",
        "- No `.env`, approval file, or live runner setting was modified.",
        "- Strategy changes are written only as inactive candidates and require manual approval.",
        "- Allowed instruments remain the active 13-symbol FX/crypto universe from `rules.md` and `constants.py`.",
        "- All 13 active FX/crypto instruments can generate fresh entries when validation gates pass.",
        "",
        "## Best Operating Pattern",
        "",
        "| Phase | What runs | What can change |",
        "| --- | --- | --- |",
        "| During live bot | Lightweight read-only improvement snapshot every N cycles, if enabled | Nothing live; report only |",
        "| Immediately after live bot | Full analytics, threshold recommendation, backtest, inactive proposal write | Inactive candidate rows/reports only |",
        "| Before next live session | Human reviews artifacts, dry-runs candidate, then edits config if approved | Manual config only |",
        "",
        "## Current Performance",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Return | {_format_number(metrics.get('return'))} |",
        f"| Max drawdown | {_format_number(metrics.get('max_drawdown'))} |",
        f"| 15-minute Sharpe | {_format_number(metrics.get('sharpe_15m'))} |",
        f"| 15-minute observations | {metrics.get('sharpe_15m_observations')} |",
        f"| Trade count | {metrics.get('trade_count')} |",
        f"| Real fill count | {metrics.get('real_fill_count')} |",
        f"| Filled order count | {metrics.get('filled_order_count')} |",
        "",
        "## Threshold Recommendation",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Available | `{str(threshold.available).lower()}` |",
        f"| Current entry | `{threshold.current_entry_threshold:g}` |",
        f"| Current exit | `{threshold.current_exit_threshold:g}` |",
        f"| Recommended entry | `{threshold.recommended_entry_threshold:g}` |",
        f"| Recommended exit | `{threshold.recommended_exit_threshold:g}` |",
        f"| Rows evaluated | `{threshold.evaluated_rows}` |",
        f"| Pairs evaluated | `{threshold.evaluated_pairs}` |",
        f"| Reason | {threshold.reason} |",
        "",
    ]
    if threshold.best is not None:
        lines.extend(
            [
                "Best threshold-pair diagnostics:",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Return bps | {threshold.best.total_return_bps:.6g} |",
                f"| Max drawdown bps | {threshold.best.max_drawdown_bps:.6g} |",
                f"| Sharpe | {threshold.best.sharpe:.6g} |",
                f"| Trades | {threshold.best.trade_count} |",
                "",
            ]
        )
    lines.extend(
        [
            "## Inactive Candidate",
            "",
            _dict_table(report.inactive_threshold_candidate or {"created": False, "reason": "recommendation matches current settings or is unavailable"}),
            "",
            "## Champion/Challenger",
            "",
            _dict_table(report.backtest_summary, flatten_results=True),
            "",
            "## Artifacts",
            "",
        ]
    )
    for key, path in sorted(paths.items()):
        lines.append(f"- {key}: `{path}`")
    lines.extend(
        [
            "",
            "## Manual Promotion Checklist",
            "",
            "1. Review `latest.md`, analytics, threshold diagnostics, order rejections, fill slippage, and backtest results.",
            "2. Reject the candidate if it increases leverage, margin usage, concentration, red-line, or risk-discipline risk.",
            "3. Run a bounded dry-run or shadow session using the candidate threshold values.",
            "4. Only after review, manually update `ENTRY_THRESHOLD` and `EXIT_THRESHOLD` in `.env` or deployment variables.",
            "5. Restart the bot in a bounded live window and re-run this improvement loop afterward.",
        ]
    )
    return "\n".join(lines) + "\n"


def _dict_table(values: Mapping[str, Any], *, flatten_results: bool = False) -> str:
    rows: list[tuple[str, Any]] = []
    for key, value in values.items():
        if flatten_results and key == "results" and isinstance(value, list):
            for item in value:
                rows.append((f"result:{item.get('strategy')}", json.dumps(_json_safe(item), sort_keys=True)))
            continue
        rows.append((str(key), value))
    if not rows:
        return "| field | value |\n| --- | --- |"
    lines = ["| field | value |", "| --- | --- |"]
    for key, value in rows:
        rendered = json.dumps(_json_safe(value), sort_keys=True) if isinstance(value, Mapping | list | tuple) else str(_json_safe(value))
        lines.append(f"| {key} | {rendered} |")
    return "\n".join(lines)


def _candidate_version(candidate: StrategyParams, generated_at_utc: datetime) -> str:
    timestamp = generated_at_utc.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    return (
        f"ci_threshold_{candidate.strategy_version}_"
        f"e{candidate.entry_threshold:g}_x{candidate.exit_threshold:g}_{timestamp}"
    ).replace(".", "p")


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.6g}"


def _datetime_from_any(value: Any) -> datetime:
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
    raise ContinuousImprovementError(f"cannot parse datetime value: {value!r}")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return str(value)


__all__ = [
    "ContinuousImprovementConfig",
    "ContinuousImprovementError",
    "ContinuousImprovementReport",
    "run_continuous_improvement_from_store",
]
