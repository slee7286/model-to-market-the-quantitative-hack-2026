"""Inactive coarse-grid parameter proposals.

Retuning in this project is advisory only. The functions here never activate a
strategy version and never propose higher leverage, margin usage, symbol caps,
or risk-per-trade values than the current frozen parameters.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mt5_crypto_bot.constants import DEFAULT_STRATEGY_VERSION
from mt5_crypto_bot.schemas import StrategyParams
from mt5_crypto_bot.storage import SQLiteStore


@dataclass(frozen=True)
class CoarseGrid:
    """Small retuning grid from the research/design freeze."""

    entry_thresholds: tuple[float, ...] = (1.0, 1.25, 1.5)
    exit_thresholds: tuple[float, ...] = (0.05, 0.10, 0.15, 0.25, 0.35, 0.50, 0.75)
    atr_stop_multiples: tuple[float, ...] = (1.2, 1.6, 2.0)
    take_profit_multiples: tuple[float, ...] = (1.8, 2.4, 3.0)
    max_proposals: int = 6


@dataclass(frozen=True)
class ParameterProposal:
    """Manual-review-only strategy parameter proposal."""

    strategy_version: str
    parent_strategy_version: str
    params: dict[str, Any]
    rationale: str
    generated_at_utc: datetime
    active: bool = False
    requires_manual_approval: bool = True

    def to_record(self) -> dict[str, Any]:
        """Return a JSON/SQLite-friendly proposal record."""

        return {
            "strategy_version": self.strategy_version,
            "parent_strategy_version": self.parent_strategy_version,
            "params": self.params,
            "rationale": self.rationale,
            "generated_at_utc": self.generated_at_utc.astimezone(timezone.utc).isoformat(),
            "active": self.active,
            "requires_manual_approval": self.requires_manual_approval,
        }


def generate_coarse_parameter_proposals(
    *,
    metrics: Mapping[str, Any],
    signal_bucket_rows: Sequence[Mapping[str, Any]] = (),
    reject_reason_rows: Sequence[Mapping[str, Any]] = (),
    base_params: StrategyParams | None = None,
    grid: CoarseGrid | None = None,
    generated_at_utc: datetime | None = None,
) -> tuple[ParameterProposal, ...]:
    """Generate inactive proposals from a coarse, bounded parameter grid."""

    base = base_params or StrategyParams()
    coarse_grid = grid or CoarseGrid()
    generated_at = generated_at_utc or datetime.now(timezone.utc)
    candidates = []
    for entry, exit_, stop, take_profit in itertools.product(
        coarse_grid.entry_thresholds,
        coarse_grid.exit_thresholds,
        coarse_grid.atr_stop_multiples,
        coarse_grid.take_profit_multiples,
    ):
        if exit_ >= entry:
            continue
        candidate = base.model_copy(
            update={
                "entry_threshold": entry,
                "exit_threshold": exit_,
                "atr_stop_multiple": stop,
                "take_profit_multiple": take_profit,
            }
        )
        if _same_tunable_params(base, candidate):
            continue
        if not proposal_is_risk_safe(base, candidate):
            continue
        rationale = _proposal_rationale(
            metrics=metrics,
            reject_reason_rows=reject_reason_rows,
            signal_bucket_rows=signal_bucket_rows,
            candidate=candidate,
            base=base,
        )
        score = _proposal_score(
            metrics=metrics,
            reject_reason_rows=reject_reason_rows,
            signal_bucket_rows=signal_bucket_rows,
            candidate=candidate,
            base=base,
        )
        candidates.append((score, candidate, rationale))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[: coarse_grid.max_proposals]
    proposals: list[ParameterProposal] = []
    for _, candidate, rationale in selected:
        version = _proposal_version(candidate, generated_at)
        params = candidate.model_dump(mode="json")
        params.update(
            {
                "strategy_version": version,
                "parent_strategy_version": base.strategy_version,
                "proposal_status": "inactive_manual_review_required",
                "coarse_grid_only": True,
                "requires_manual_approval": True,
            }
        )
        proposals.append(
            ParameterProposal(
                strategy_version=version,
                parent_strategy_version=base.strategy_version,
                params=params,
                rationale=rationale,
                generated_at_utc=generated_at,
            )
        )
    return tuple(proposals)


def proposal_is_risk_safe(base: StrategyParams, candidate: StrategyParams) -> bool:
    """Return true only when risk/leverage limits are not increased."""

    return (
        candidate.risk_per_trade <= base.risk_per_trade
        and candidate.max_gross_leverage <= base.max_gross_leverage
        and candidate.max_symbol_leverage <= base.max_symbol_leverage
        and candidate.max_margin_usage <= base.max_margin_usage
    )


def store_parameter_proposals(
    store: SQLiteStore,
    proposals: Sequence[ParameterProposal],
) -> int:
    """Persist proposals as inactive strategy version rows."""

    for proposal in proposals:
        payload = dict(proposal.params)
        payload.update(
            {
                "rationale": proposal.rationale,
                "generated_at_utc": proposal.generated_at_utc.astimezone(timezone.utc).isoformat(),
                "active": False,
                "approved_by": None,
                "approved_at_utc": None,
            }
        )
        store.upsert_strategy_version(payload, active=False)
    return len(proposals)


def manual_approval_workflow_markdown() -> str:
    """Document the manual approval gate for retuning proposals."""

    return "\n".join(
        [
            "1. Review the inactive `strategy_versions` rows created by analytics.",
            "2. Backtest the candidate on non-fixture history and compare it with `momo_v1`.",
            "3. Run a bounded dry-run/shadow session and inspect risk blocks and spread costs.",
            "4. Reject any candidate that increases leverage, margin usage, risk per trade, or symbol caps without explicit human approval.",
            "5. Promote a candidate only through a separate manual approval change that marks exactly one strategy version active.",
        ]
    )


def _same_tunable_params(base: StrategyParams, candidate: StrategyParams) -> bool:
    return (
        base.entry_threshold == candidate.entry_threshold
        and base.exit_threshold == candidate.exit_threshold
        and base.atr_stop_multiple == candidate.atr_stop_multiple
        and base.take_profit_multiple == candidate.take_profit_multiple
    )


def _proposal_score(
    *,
    metrics: Mapping[str, Any],
    reject_reason_rows: Sequence[Mapping[str, Any]],
    signal_bucket_rows: Sequence[Mapping[str, Any]],
    candidate: StrategyParams,
    base: StrategyParams,
) -> float:
    score = 0.0
    trade_count = int(metrics.get("trade_count") or 0)
    failed_blocks = _reason_count(reject_reason_rows, "block") + _reason_count(
        reject_reason_rows, "reject"
    )
    spread_blocks = _reason_count(reject_reason_rows, "spread")
    strong_pnl = _bucket_pnl(signal_bucket_rows, "strong_long") + _bucket_pnl(
        signal_bucket_rows, "strong_short"
    )

    if trade_count < 30:
        score += 2.0 if candidate.entry_threshold < base.entry_threshold else -0.5
    if failed_blocks > 0:
        score += 2.0 if candidate.entry_threshold >= base.entry_threshold else -1.0
        score += 0.5 if candidate.exit_threshold <= base.exit_threshold else -0.25
    if spread_blocks > 0:
        score += 1.5 if candidate.entry_threshold >= base.entry_threshold else -0.75
    if strong_pnl > 0:
        score += 0.75 if candidate.entry_threshold <= base.entry_threshold else 0.25

    score -= abs(candidate.entry_threshold - base.entry_threshold) * 0.15
    score -= abs(candidate.exit_threshold - base.exit_threshold) * 0.10
    score -= abs(candidate.atr_stop_multiple - base.atr_stop_multiple) * 0.05
    score -= abs(candidate.take_profit_multiple - base.take_profit_multiple) * 0.05
    return score


def _proposal_rationale(
    *,
    metrics: Mapping[str, Any],
    reject_reason_rows: Sequence[Mapping[str, Any]],
    signal_bucket_rows: Sequence[Mapping[str, Any]],
    candidate: StrategyParams,
    base: StrategyParams,
) -> str:
    pieces: list[str] = []
    trade_count = int(metrics.get("trade_count") or 0)
    if trade_count < 30 and candidate.entry_threshold < base.entry_threshold:
        pieces.append("lower entry threshold to collect more validation trades")
    block_count = _reason_count(reject_reason_rows, "block") + _reason_count(
        reject_reason_rows,
        "reject",
    )
    if block_count > 0 and candidate.entry_threshold >= base.entry_threshold:
        pieces.append("higher or equal entry threshold after execution/risk blocks")
    if _reason_count(reject_reason_rows, "spread") > 0 and candidate.entry_threshold >= base.entry_threshold:
        pieces.append("avoid weaker signals when spread blocks are common")
    if _bucket_pnl(signal_bucket_rows, "strong_long") + _bucket_pnl(signal_bucket_rows, "strong_short") > 0:
        pieces.append("test nearby threshold around profitable strong-score buckets")
    if not pieces:
        pieces.append("coarse-grid sensitivity test around frozen momo_v1 parameters")
    return "; ".join(pieces)


def _reason_count(rows: Sequence[Mapping[str, Any]], needle: str) -> int:
    total = 0
    lowered = needle.lower()
    for row in rows:
        reason = str(row.get("reason", "")).lower()
        source = str(row.get("source", "")).lower()
        if lowered in reason or lowered in source:
            total += int(row.get("count") or 0)
    return total


def _bucket_pnl(rows: Sequence[Mapping[str, Any]], bucket: str) -> float:
    for row in rows:
        if str(row.get("score_bucket")) == bucket:
            try:
                return float(row.get("pnl_usd") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _proposal_version(candidate: StrategyParams, generated_at_utc: datetime) -> str:
    timestamp = generated_at_utc.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    return (
        f"proposal_{DEFAULT_STRATEGY_VERSION}_"
        f"e{candidate.entry_threshold:g}_x{candidate.exit_threshold:g}_"
        f"s{candidate.atr_stop_multiple:g}_tp{candidate.take_profit_multiple:g}_"
        f"{timestamp}"
    ).replace(".", "p")


__all__ = [
    "CoarseGrid",
    "ParameterProposal",
    "generate_coarse_parameter_proposals",
    "manual_approval_workflow_markdown",
    "proposal_is_risk_safe",
    "store_parameter_proposals",
]
