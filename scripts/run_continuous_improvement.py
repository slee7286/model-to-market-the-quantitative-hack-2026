"""Run the offline continuous-improvement loop.

This script reads the local SQLite audit database only. It never connects to
MT5, never sends orders, never edits `.env`, and never activates strategy
versions. It writes reports plus review-only inactive candidates.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import ValidationError

from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.continuous_improvement import (
    ContinuousImprovementConfig,
    ContinuousImprovementError,
    run_continuous_improvement_from_store,
)
from mt5_crypto_bot.schemas import normalize_symbols


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a safe continuous-improvement packet from local SQLite data. "
            "No MT5 connection, no live orders, and no automatic promotion."
        )
    )
    parser.add_argument("--env-file", default=".env", help="Path to dotenv file.")
    parser.add_argument("--database-url", default=None, help="SQLite database URL/path.")
    parser.add_argument("--symbols", default=None, help="Comma-separated allowed canonical symbols.")
    parser.add_argument(
        "--output-dir",
        default="reports/continuous_improvement",
        help="Directory for improvement reports and candidate snippets.",
    )
    parser.add_argument("--run-id", default=None, help="Optional stable report suffix.")
    parser.add_argument(
        "--no-store-proposals",
        action="store_true",
        help="Do not write inactive analytics or threshold proposals to strategy_versions.",
    )
    parser.add_argument(
        "--skip-backtest",
        action="store_true",
        help="Skip full champion/challenger backtest artifacts for faster during-run snapshots.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the compact report summary as JSON.",
    )
    parser.add_argument(
        "--start-utc",
        default=None,
        help="Optional inclusive UTC start time for session-window improvement.",
    )
    parser.add_argument(
        "--end-utc",
        default=None,
        help="Optional inclusive UTC end time for session-window improvement.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        report = run_continuous_improvement_from_store(
            args.database_url or config.database_url,
            target_symbols=symbols,
            base_params=config.strategy_params(),
            config=ContinuousImprovementConfig(
                output_dir=args.output_dir,
                run_id=args.run_id,
                store_analytics_proposals=not args.no_store_proposals,
                store_threshold_candidate=not args.no_store_proposals,
                include_shadow_backtest=not args.skip_backtest,
                write_backtest_artifacts=not args.skip_backtest,
                start_time_utc=_parse_datetime_arg(args.start_utc),
                end_time_utc=_parse_datetime_arg(args.end_utc),
            ),
        )
    except (ValidationError, ValueError) as exc:
        print("Continuous-improvement configuration failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Use only the active FX/crypto symbols from rules.md and constants.py.", file=sys.stderr)
        return 2
    except ContinuousImprovementError as exc:
        print("Continuous-improvement loop failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.summary(), indent=2, sort_keys=True))
        return 0

    threshold = report.threshold_recommendation
    print("Continuous-improvement report completed.")
    print("Safety: offline only; no MT5 connection, no order_check/order_send, no auto-promotion.")
    print(f"Data label: {report.data_label}")
    print(f"Return: {report.metrics.get('return'):.6g}")
    print(f"Window equity change: {report.metrics.get('window_equity_change'):.2f}")
    print(f"Window return: {report.metrics.get('window_return'):.6g}")
    print(f"Max drawdown: {report.metrics.get('max_drawdown'):.6g}")
    print(f"15-minute Sharpe: {report.metrics.get('sharpe_15m'):.6g}")
    print(f"Trade count: {report.metrics.get('trade_count')}")
    print(
        "Thresholds: "
        f"current=({threshold.current_entry_threshold:g}, {threshold.current_exit_threshold:g}) "
        f"recommended=({threshold.recommended_entry_threshold:g}, {threshold.recommended_exit_threshold:g}) "
        f"available={threshold.available}"
    )
    if report.inactive_threshold_candidate:
        print(f"Inactive threshold candidate: {report.inactive_threshold_candidate['strategy_version']}")
    else:
        print("Inactive threshold candidate: none")
    print(f"Markdown report: {report.paths['markdown']}")
    print(f"Candidate env snippet: {report.paths['candidate_env']}")
    print(f"Summary JSON: {report.paths['summary_json']}")
    return 0


def _parse_datetime_arg(value: str | None) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
