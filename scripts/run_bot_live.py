"""Run the guarded MT5 live bot.

This script is intentionally separate from ``run_bot_dry_run.py``. It can place
live orders only after ``LIVE_APPROVED=true`` and ``config/LIVE_APPROVED.json``
both pass validation. It never uses fixture fallback.
"""

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
from mt5_crypto_bot.dry_run import MIN_DRY_RUN_POLL_SECONDS
from mt5_crypto_bot.execution import DEFAULT_LIVE_APPROVAL_FILE, LiveTradingApprovalError
from mt5_crypto_bot.live import (
    DEFAULT_LIVE_POLL_SECONDS,
    LiveCycleResult,
    LiveRunError,
    run_live_session,
)
from mt5_crypto_bot.schemas import ExecutionResult, ExecutionStatus, normalize_symbols
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded guarded live MT5 session. Requires LIVE_APPROVED=true "
            "and config/LIVE_APPROVED.json. Calls order_check before order_send."
        )
    )
    parser.add_argument(
        "--minutes",
        type=float,
        required=True,
        help="Positive bounded live runtime in minutes.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_LIVE_POLL_SECONDS,
        help=f"Seconds between live cycles. Minimum {MIN_DRY_RUN_POLL_SECONDS:g}.",
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
        help="Recent M1/M5 bars to request per symbol from MT5.",
    )
    parser.add_argument(
        "--tick-backfill-minutes",
        type=float,
        default=None,
        help="Optionally backfill recent ticks from MT5.",
    )
    parser.add_argument(
        "--include-depth",
        action="store_true",
        help="Optionally collect market depth with read-only MT5 depth calls.",
    )
    parser.add_argument(
        "--kill-switch-file",
        default="config/KILL_SWITCH",
        help="Optional local kill-switch file. When active, risk blocks new exposure.",
    )
    parser.add_argument(
        "--approval-file",
        default=str(DEFAULT_LIVE_APPROVAL_FILE),
        help="Local JSON approval artifact required for guarded live trading.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Python logging level.",
    )
    return parser.parse_args(argv)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _not_placed_reason(result: ExecutionResult) -> str:
    """Build a human-readable reason an approved order was not placed."""
    parts: list[str] = []
    if result.message:
        parts.append(result.message)
    if result.retcode is not None:
        parts.append(f"retcode={result.retcode}")
    payload = result.result or {}
    order_check = payload.get("order_check") or {}
    if isinstance(order_check, dict):
        if order_check.get("comment") or order_check.get("retcode") is not None:
            parts.append(
                f"order_check[retcode={order_check.get('retcode')}, "
                f"comment={order_check.get('comment')!r}]"
            )
        elif payload.get("order_check_called") and not order_check:
            parts.append("order_check returned an empty/None result (broker rejected the request outright)")
    order_send = payload.get("order_send") or {}
    if isinstance(order_send, dict) and order_send.get("comment"):
        parts.append(f"order_send[comment={order_send.get('comment')!r}]")
    last_error = payload.get("last_error")
    if last_error:
        parts.append(f"last_error={last_error}")
    return "; ".join(parts) or "no reason recorded"


def _print_order_outcomes(result: LiveCycleResult, cycle_number: int) -> None:
    """Print every order attempt this cycle: risk verdict and placement result."""
    decisions = result.risk_result.decisions
    if not decisions:
        return
    exec_by_id = {res.client_order_id: res for res in result.execution_result.results}
    finished = result.finished_at_utc.isoformat()
    print(f"\n=== cycle {cycle_number} @ {finished}: {len(decisions)} order attempt(s) ===", flush=True)
    for decision in decisions:
        intent = decision.order_intent
        if intent is None:
            print("  ATTEMPT (could not build order intent)", flush=True)
            print(f"      RISK   : BLOCKED -> {decision.risk_check.reason}", flush=True)
            print("      PLACED : no (blocked before execution)", flush=True)
            continue

        print(
            f"  ATTEMPT {intent.symbol} {_enum_value(intent.side)} "
            f"vol={intent.requested_volume:g} @ {intent.requested_price}",
            flush=True,
        )
        if not decision.passed:
            print(f"      RISK   : BLOCKED -> {decision.risk_check.reason}", flush=True)
            print("      PLACED : no (blocked before execution)", flush=True)
            continue

        print("      RISK   : PASSED", flush=True)
        execution = exec_by_id.get(intent.client_order_id)
        if execution is None:
            print("      PLACED : no -> risk-approved but no execution result was recorded", flush=True)
            continue

        status = _enum_value(execution.status)
        if status in {ExecutionStatus.FILLED.value, ExecutionStatus.PARTIAL.value}:
            note = "partial fill" if status == ExecutionStatus.PARTIAL.value else "filled"
            print(
                f"      PLACED : YES ({note}) ticket={execution.mt5_order_ticket} "
                f"fill_price={execution.average_fill_price} retcode={execution.retcode}",
                flush=True,
            )
        elif status == ExecutionStatus.DRY_RUN.value:
            print("      PLACED : no (dry-run mode: order recorded, not sent to MT5)", flush=True)
        else:
            print(f"      PLACED : no -> {_not_placed_reason(execution)}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        collector_settings = CollectorSettings(
            bar_count=args.bar_count,
            tick_backfill_minutes=args.tick_backfill_minutes,
            include_depth=args.include_depth,
        )
        results = run_live_session(
            config,
            minutes=args.minutes,
            poll_seconds=args.poll_seconds,
            on_cycle=_print_order_outcomes,
            database_url=args.database_url,
            target_symbols=symbols,
            symbol_map_path=args.symbol_map,
            collector_settings=collector_settings,
            kill_switch_file=args.kill_switch_file,
            live_approval_file=args.approval_file,
        )
    except ValidationError as exc:
        print("Live-run configuration validation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Shared config must remain dry_run/paper; live is enabled only by this runner.", file=sys.stderr)
        return 2
    except LiveTradingApprovalError as exc:
        print("Guarded live trading approval failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("No order_check or order_send calls were made.", file=sys.stderr)
        return 2
    except (ValueError, LiveRunError) as exc:
        print("Guarded live run failed safely.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    print("Guarded live run completed.")
    print(f"Cycles: {len(results)}")
    total_signals = sum(len(result.strategy_result.signals) for result in results)
    total_intents = sum(len(result.strategy_result.order_intents) for result in results)
    total_risk_checks = sum(len(result.risk_result.risk_checks) for result in results)
    total_approved = sum(len(result.risk_result.approved_orders) for result in results)
    total_sent = sum(result.execution_result.summary()["sent_to_mt5"] for result in results)
    print(f"Signals stored: {total_signals}")
    print(f"Order intents generated: {total_intents}")
    print(f"Risk checks stored: {total_risk_checks}")
    print(f"Risk-approved live orders: {total_approved}")
    print(f"MT5 order_send results recorded: {total_sent}")
    if results:
        print("Last cycle summary:")
        print(json.dumps(results[-1].summary(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
