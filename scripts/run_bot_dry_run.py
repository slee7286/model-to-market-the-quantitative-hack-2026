"""Run the end-to-end bot in dry-run mode only."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import ValidationError

from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.data_collector import DEFAULT_BAR_COUNT, CollectorSettings
from mt5_crypto_bot.dry_run import (
    DEFAULT_DRY_RUN_POLL_SECONDS,
    DEFAULT_FIXTURE_BAR_COUNT,
    MIN_DRY_RUN_POLL_SECONDS,
    DryRunOrchestrationError,
    run_dry_run_session,
)
from mt5_crypto_bot.schemas import normalize_symbols
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one or more dry-run bot cycles: read-only collection, features, "
            "strategy, risk checks, and dry-run execution recording. This script "
            "never calls MT5 order_check or order_send."
        )
    )
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one dry-run cycle. Default when --minutes is omitted.",
    )
    run_group.add_argument(
        "--minutes",
        type=float,
        help="Run a bounded dry-run session for this many minutes.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_DRY_RUN_POLL_SECONDS,
        help=f"Seconds between cycles for --minutes runs. Minimum {MIN_DRY_RUN_POLL_SECONDS:g}.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to local dotenv file.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite database URL/path. Defaults to DATABASE_URL from config.",
    )
    parser.add_argument(
        "--symbol-map",
        default=str(DEFAULT_SYMBOL_MAP_PATH),
        help="Path to confirmed canonical-to-broker symbol map JSON.",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated allowed canonical symbols. Defaults to TARGET_SYMBOLS/config.",
    )
    parser.add_argument(
        "--bar-count",
        type=int,
        default=DEFAULT_BAR_COUNT,
        help="Recent M1/M5 bars to request per symbol when MT5 collection is available.",
    )
    parser.add_argument(
        "--tick-backfill-minutes",
        type=float,
        default=None,
        help="Optionally backfill recent ticks when MT5 collection is available.",
    )
    parser.add_argument(
        "--include-depth",
        action="store_true",
        help="Optionally collect market depth with read-only MT5 depth calls.",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Force deterministic synthetic fixture data for offline smoke validation.",
    )
    parser.add_argument(
        "--fixture-count",
        type=int,
        default=DEFAULT_FIXTURE_BAR_COUNT,
        help="Number of synthetic M5 bars per symbol when fixture fallback is used.",
    )
    parser.add_argument(
        "--no-fixture-fallback",
        action="store_true",
        help="Fail instead of seeding synthetic fixture data when MT5 setup is unavailable.",
    )
    parser.add_argument(
        "--no-freshness-check",
        action="store_true",
        help="Disable stale bar/tick gates for offline inspection only.",
    )
    parser.add_argument(
        "--kill-switch-file",
        default="config/KILL_SWITCH",
        help="Optional local kill-switch file. When active, risk blocks new exposure.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Python logging level.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_once = bool(args.once or args.minutes is None)

    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        collector_settings = CollectorSettings(
            bar_count=args.bar_count,
            tick_backfill_minutes=args.tick_backfill_minutes,
            include_depth=args.include_depth,
        )
        results = run_dry_run_session(
            config,
            once=run_once,
            minutes=args.minutes,
            poll_seconds=args.poll_seconds,
            database_url=args.database_url,
            target_symbols=symbols,
            symbol_map_path=args.symbol_map,
            collector_settings=collector_settings,
            fixture_fallback=not args.no_fixture_fallback,
            force_fixture=args.fixture,
            fixture_bar_count=args.fixture_count,
            enforce_freshness=not args.no_freshness_check,
            kill_switch_file=args.kill_switch_file,
        )
    except ValidationError as exc:
        print("Dry-run configuration validation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Keep TRADE_MODE=dry_run or paper and use only allowed crypto symbols.", file=sys.stderr)
        return 2
    except (ValueError, DryRunOrchestrationError) as exc:
        print("End-to-end dry-run failed safely.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("No live orders were sent.", file=sys.stderr)
        return 2

    print("End-to-end dry-run completed.")
    print(f"Cycles: {len(results)}")
    total_signals = sum(len(result.strategy_result.signals) for result in results)
    total_intents = sum(len(result.strategy_result.order_intents) for result in results)
    total_risk_checks = sum(len(result.risk_result.risk_checks) for result in results)
    total_approved = sum(len(result.risk_result.approved_orders) for result in results)
    total_dry_run = sum(result.execution_result.dry_run_count for result in results)
    print(f"Signals stored: {total_signals}")
    print(f"Order intents generated: {total_intents}")
    print(f"Risk checks stored: {total_risk_checks}")
    print(f"Risk-approved dry-run orders: {total_approved}")
    print(f"Dry-run execution records: {total_dry_run}")
    print("No MT5 order_check or order_send calls are made by this script.")

    if results:
        print("Last cycle summary:")
        print(json.dumps(results[-1].summary(), indent=2, sort_keys=True))
        _print_signal_lines(results[-1])
        _print_risk_lines(results[-1])
        _print_execution_lines(results[-1])
    return 0


def _print_signal_lines(result: object) -> None:
    signals = getattr(result, "strategy_result").signals
    if not signals:
        print("No signals generated.")
        return
    print("Signals:")
    for signal in signals:
        print(
            json.dumps(
                {
                    "signal_id": signal.signal_id,
                    "symbol": signal.symbol,
                    "decision": signal.decision,
                    "direction": signal.direction,
                    "score": round(signal.score, 6),
                    "target_leverage": round(signal.target_leverage, 6),
                    "target_volume": signal.target_volume,
                    "reason": signal.reason,
                },
                sort_keys=True,
            )
        )


def _print_risk_lines(result: object) -> None:
    decisions = getattr(result, "risk_result").decisions
    if not decisions:
        print("No order intents reached risk checks.")
        return
    print("Risk decisions:")
    for decision in decisions:
        check = decision.risk_check
        print(
            json.dumps(
                {
                    "check_id": check.check_id,
                    "signal_id": check.signal_id,
                    "symbol": check.symbol,
                    "passed": check.passed,
                    "reason": check.reason,
                },
                sort_keys=True,
            )
        )


def _print_execution_lines(result: object) -> None:
    executions = getattr(result, "execution_result").results
    if not executions:
        print("No risk-approved orders were recorded as dry-run executions.")
        return
    print("Dry-run executions:")
    for execution in executions:
        print(
            json.dumps(
                {
                    "client_order_id": execution.client_order_id,
                    "symbol": execution.symbol,
                    "status": execution.status,
                    "requested_volume": execution.requested_volume,
                    "requested_price": execution.requested_price,
                    "message": execution.message,
                    "sent_to_mt5": execution.result.get("sent_to_mt5"),
                    "order_send_called": execution.result.get("order_send_called"),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
