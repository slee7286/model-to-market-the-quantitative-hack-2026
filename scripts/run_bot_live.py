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
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import ValidationError

from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, ASSET_CLASS_BY_SYMBOL
from mt5_crypto_bot.continuous_improvement import (
    ContinuousImprovementConfig,
    ContinuousImprovementError,
    run_continuous_improvement_from_store,
)
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
from mt5_crypto_bot.thresholds import recommend_thresholds_from_store


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
    parser.add_argument(
        "--improvement-after-run",
        action="store_true",
        help="Run the full offline continuous-improvement report after the live session ends.",
    )
    parser.add_argument(
        "--improvement-every-cycles",
        type=int,
        default=0,
        help=(
            "Run a lightweight offline improvement snapshot every N live cycles. "
            "Default 0 disables during-run snapshots."
        ),
    )
    parser.add_argument(
        "--improvement-output-dir",
        default="reports/continuous_improvement",
        help="Directory for optional continuous-improvement artifacts.",
    )
    parser.add_argument(
        "--order-detail-limit",
        type=int,
        default=8,
        help=(
            "Maximum individual order/suppression rows to print per cycle. "
            "Use 0 for grouped summaries only. SQLite still stores all details."
        ),
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


def _print_execution_diagnostics(execution: ExecutionResult) -> None:
    payload = execution.result or {}
    precheck = payload.get("live_precheck") if isinstance(payload, dict) else None
    if not isinstance(precheck, dict):
        return
    refresh = precheck.get("live_refresh") or {}
    liquidity = precheck.get("liquidity") or {}
    if isinstance(refresh, dict):
        print(
            "      LIVE   : "
            f"price={refresh.get('live_requested_price')} "
            f"bid={refresh.get('live_bid')} ask={refresh.get('live_ask')} "
            f"tick_age_s={refresh.get('live_tick_age_seconds')} "
            f"broker_offset_s={refresh.get('broker_time_offset_seconds')} "
            f"sl={refresh.get('refreshed_stop_loss')} tp={refresh.get('refreshed_take_profit')}",
            flush=True,
        )
    if isinstance(liquidity, dict):
        print(
            "      LIQ    : "
            f"source={liquidity.get('source')} "
            f"visible={liquidity.get('visible_volume')} "
            f"vol_before={liquidity.get('requested_volume_before')} "
            f"vol_after={liquidity.get('requested_volume_after')}",
            flush=True,
        )


def _print_order_outcomes(
    result: LiveCycleResult,
    cycle_number: int,
    *,
    detail_limit: int = 8,
) -> None:
    """Print a compact live-cycle summary plus a bounded sample of order details."""
    decisions = result.risk_result.decisions
    suppressed = getattr(result, "suppressed_order_intents", ())
    exec_by_id = {res.client_order_id: res for res in result.execution_result.results}
    finished = result.finished_at_utc.isoformat()
    execution_summary = result.execution_result.summary()
    print(
        f"\n=== cycle {cycle_number} @ {finished}: "
        f"{len(decisions)} order attempt(s), "
        f"{len(suppressed)} suppressed duplicate(s), "
        f"{len(result.risk_result.approved_orders)} risk-approved, "
        f"{execution_summary['sent_to_mt5']} sent to MT5 ===",
        flush=True,
    )
    _print_cycle_health(result)
    _print_grouped_order_outcomes(result, exec_by_id)
    if not decisions and not suppressed:
        print("  NO ORDER ATTEMPTS THIS CYCLE", flush=True)
        return
    if detail_limit <= 0:
        return

    printed = 0
    total_detail_rows = len(decisions) + len(suppressed)
    for item in suppressed:
        if printed >= detail_limit:
            break
        intent = item.order_intent
        print(
            f"  SUPPRESS {intent.symbol} {_enum_value(intent.side)} "
            f"vol={intent.requested_volume:g} @ {intent.requested_price}",
            flush=True,
        )
        print(
            "      RETRY  : skipped exact duplicate failed intent "
            f"until {item.suppressed_until_utc.isoformat()}",
            flush=True,
        )
        print(f"      REASON : previous failure -> {_compact_reason(item.reason)}", flush=True)
        printed += 1
    for decision in decisions:
        if printed >= detail_limit:
            break
        intent = decision.order_intent
        if intent is None:
            print("  ATTEMPT (could not build order intent)", flush=True)
            print(
                f"      RISK   : BLOCKED -> {_compact_reason(decision.risk_check.reason)}",
                flush=True,
            )
            print("      PLACED : no (blocked before execution)", flush=True)
            printed += 1
            continue

        print(
            f"  ATTEMPT {intent.symbol} {_enum_value(intent.side)} "
            f"vol={intent.requested_volume:g} @ {intent.requested_price}",
            flush=True,
        )
        if not decision.passed:
            print(
                f"      RISK   : BLOCKED -> {_compact_reason(decision.risk_check.reason)}",
                flush=True,
            )
            print("      PLACED : no (blocked before execution)", flush=True)
            printed += 1
            continue

        print("      RISK   : PASSED", flush=True)
        execution = exec_by_id.get(intent.client_order_id)
        if execution is None:
            print("      PLACED : no -> risk-approved but no execution result was recorded", flush=True)
            printed += 1
            continue

        _print_execution_diagnostics(execution)
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
        printed += 1
    omitted = max(0, total_detail_rows - printed)
    if omitted:
        print(
            f"  ... {omitted} additional order detail row(s) omitted "
            f"(set --order-detail-limit higher to print more).",
            flush=True,
        )


def _print_cycle_health(result: LiveCycleResult) -> None:
    collection = result.collection_result
    stale = ",".join(collection.stale_symbols) if collection.stale_symbols else "none"
    print(
        "  DATA   : "
        f"symbols={len(collection.symbols)} bars={collection.bars_written} "
        f"ticks={collection.ticks_written} metadata={collection.metadata_written} "
        f"requests={collection.request_count} stale={stale} errors={len(collection.errors)}",
        flush=True,
    )
    signal_counts = Counter(
        (
            ASSET_CLASS_BY_SYMBOL.get(signal.symbol, "unknown"),
            _enum_value(signal.decision),
        )
        for signal in result.strategy_result.signals
    )
    signal_parts = [
        f"{asset}:{decision}={count}"
        for (asset, decision), count in sorted(signal_counts.items())
    ]
    print(
        "  SIGNAL : "
        f"signals={len(result.strategy_result.signals)} "
        f"intents={len(result.strategy_result.order_intents)} "
        + (" ".join(signal_parts) if signal_parts else "none"),
        flush=True,
    )
    risk_snapshot = _last_projected_risk_snapshot(result)
    if risk_snapshot:
        print(
            "  RISK   : "
            f"gross={risk_snapshot.get('gross_leverage', 0.0):.2f}x "
            f"margin={100 * risk_snapshot.get('margin_usage', 0.0):.1f}% "
            f"single={100 * risk_snapshot.get('single_instrument_exposure', 0.0):.1f}% "
            f"net={100 * risk_snapshot.get('net_directional_exposure', 0.0):.1f}%",
            flush=True,
        )


def _print_grouped_order_outcomes(
    result: LiveCycleResult,
    exec_by_id: Mapping[str, ExecutionResult],
) -> None:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "volume": 0.0}
    )
    for item in getattr(result, "suppressed_order_intents", ()):
        intent = item.order_intent
        key = (intent.symbol, _enum_value(intent.side), "suppressed", _compact_reason(item.reason))
        groups[key]["count"] += 1
        groups[key]["volume"] += float(intent.requested_volume or 0.0)
    for decision in result.risk_result.decisions:
        intent = decision.order_intent
        symbol = decision.risk_check.symbol or (intent.symbol if intent is not None else "unknown")
        side = _enum_value(intent.side) if intent is not None else "unknown"
        volume = float(intent.requested_volume or 0.0) if intent is not None else 0.0
        if not decision.passed:
            key = (symbol, side, "blocked", _compact_reason(decision.risk_check.reason))
        else:
            execution = exec_by_id.get(intent.client_order_id) if intent is not None else None
            status = _enum_value(execution.status) if execution is not None else "missing_execution"
            reason = _compact_reason(execution.message if execution is not None else "")
            key = (symbol, side, status, reason)
        groups[key]["count"] += 1
        groups[key]["volume"] += volume
    if not groups:
        return
    print("  ORDER GROUPS:", flush=True)
    for (symbol, side, status, reason), values in sorted(
        groups.items(),
        key=lambda item: (-item[1]["count"], item[0][0], item[0][1], item[0][2]),
    ):
        reason_suffix = f" reason={reason}" if reason else ""
        print(
            f"    {symbol} {side} {status}: "
            f"count={values['count']} volume={values['volume']:.4g}{reason_suffix}",
            flush=True,
        )


def _last_projected_risk_snapshot(result: LiveCycleResult) -> dict[str, float]:
    for decision in reversed(result.risk_result.decisions):
        projected = decision.risk_check.details.get("projected_metrics")
        if isinstance(projected, dict):
            return {
                "gross_leverage": _float_from_mapping(projected, "gross_leverage"),
                "margin_usage": _float_from_mapping(projected, "margin_usage"),
                "single_instrument_exposure": _float_from_mapping(
                    projected,
                    "single_instrument_exposure",
                ),
                "net_directional_exposure": _float_from_mapping(
                    projected,
                    "net_directional_exposure",
                ),
            }
    return {}


def _float_from_mapping(payload: Mapping[str, Any], key: str) -> float:
    try:
        return float(payload.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _compact_reason(reason: object, *, max_chars: int = 120) -> str:
    text = str(reason or "").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _print_threshold_recommendation(
    *,
    database_url: str,
    symbols: Sequence[str],
    entry_threshold: float,
    exit_threshold: float,
) -> None:
    try:
        recommendation = recommend_thresholds_from_store(
            database_url,
            target_symbols=symbols,
            current_entry_threshold=entry_threshold,
            current_exit_threshold=exit_threshold,
        )
    except Exception as exc:
        print(f"Threshold recommendation unavailable: {exc}", flush=True)
        print("Continuing with configured thresholds; no parameter changes were made.", flush=True)
        return

    print("\n=== offline threshold recommendation ===", flush=True)
    print(
        f"Current: ENTRY_THRESHOLD={recommendation.current_entry_threshold:g}, "
        f"EXIT_THRESHOLD={recommendation.current_exit_threshold:g}",
        flush=True,
    )
    print(
        f"Recommended: ENTRY_THRESHOLD={recommendation.recommended_entry_threshold:g}, "
        f"EXIT_THRESHOLD={recommendation.recommended_exit_threshold:g}",
        flush=True,
    )
    print(
        f"Rows={recommendation.evaluated_rows}, pairs={recommendation.evaluated_pairs}, "
        f"available={recommendation.available}",
        flush=True,
    )
    print(f"Reason: {recommendation.reason}", flush=True)
    if recommendation.best is not None:
        print(
            "Best metrics: "
            f"return_bps={recommendation.best.total_return_bps:.4g}, "
            f"drawdown_bps={recommendation.best.max_drawdown_bps:.4g}, "
            f"sharpe={recommendation.best.sharpe:.4g}, "
            f"trades={recommendation.best.trade_count}",
            flush=True,
        )
    print("No threshold changes were applied automatically.", flush=True)


def _run_improvement_packet(
    *,
    config: object,
    database_url: str,
    symbols: Sequence[str],
    output_dir: str,
    run_id: str,
    store_proposals: bool,
    include_backtest: bool,
    start_time_utc: datetime | None = None,
    end_time_utc: datetime | None = None,
) -> None:
    try:
        report = run_continuous_improvement_from_store(
            database_url,
            target_symbols=symbols,
            base_params=config.strategy_params(),
            config=ContinuousImprovementConfig(
                output_dir=output_dir,
                run_id=run_id,
                store_analytics_proposals=store_proposals,
                store_threshold_candidate=store_proposals,
                include_shadow_backtest=include_backtest,
                write_backtest_artifacts=include_backtest,
                start_time_utc=start_time_utc,
                end_time_utc=end_time_utc,
            ),
        )
    except ContinuousImprovementError as exc:
        print(f"Continuous-improvement report failed: {exc}", flush=True)
        raise

    threshold = report.threshold_recommendation
    print(
        "\n=== continuous improvement ===\n"
        "Safety: offline only; no MT5 connection, no order_check/order_send, no auto-promotion.\n"
        f"Threshold recommendation: current=({threshold.current_entry_threshold:g}, "
        f"{threshold.current_exit_threshold:g}) recommended=({threshold.recommended_entry_threshold:g}, "
        f"{threshold.recommended_exit_threshold:g}) available={threshold.available}\n"
        f"Trade count={report.metrics.get('trade_count')} "
        f"return={report.metrics.get('return'):.6g} "
        f"window_change={report.metrics.get('window_equity_change'):.2f} "
        f"window_return={report.metrics.get('window_return'):.6g} "
        f"max_dd={report.metrics.get('max_drawdown'):.6g} "
        f"sharpe_15m={report.metrics.get('sharpe_15m'):.6g}\n"
        f"Markdown report: {report.paths['markdown']}",
        flush=True,
    )


def _print_live_startup_summary(
    *,
    symbols: Sequence[str],
    database_url: str,
    entry_threshold: float,
    exit_threshold: float,
    max_gross_leverage: float,
    max_symbol_leverage: float,
    max_margin_usage: float,
    order_detail_limit: int,
) -> None:
    class_counts = Counter(ASSET_CLASS_BY_SYMBOL.get(symbol, "unknown") for symbol in symbols)
    requested = set(symbols)
    expected = set(ALLOWED_SYMBOLS)
    missing = tuple(symbol for symbol in ALLOWED_SYMBOLS if symbol not in requested)
    extra = tuple(symbol for symbol in symbols if symbol not in expected)
    print("\n=== guarded live startup ===", flush=True)
    print(
        f"Symbols ({len(symbols)}): {', '.join(symbols)}",
        flush=True,
    )
    print(
        "Asset classes: "
        + ", ".join(f"{asset}={count}" for asset, count in sorted(class_counts.items())),
        flush=True,
    )
    if missing or extra:
        print(
            "WARNING: active symbol scope differs from the integrated 13-symbol build. "
            f"missing={list(missing)} extra={list(extra)}",
            flush=True,
        )
    print(
        "Config: "
        f"ENTRY_THRESHOLD={entry_threshold:g} EXIT_THRESHOLD={exit_threshold:g} "
        f"MAX_GROSS_LEVERAGE={max_gross_leverage:g}x "
        f"MAX_SYMBOL_LEVERAGE={max_symbol_leverage:g}x "
        f"MAX_MARGIN_USAGE={100 * max_margin_usage:.1f}%",
        flush=True,
    )
    print(
        f"Database: {database_url} | order detail limit per cycle: {order_detail_limit}",
        flush=True,
    )
    print(
        "Safety: live approval gates required; order_check is called before order_send.",
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    try:
        if args.improvement_every_cycles < 0:
            raise ValueError("--improvement-every-cycles must be >= 0")
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        database_url = args.database_url or config.database_url
        session_started_at_utc = datetime.now(timezone.utc)
        _print_live_startup_summary(
            symbols=symbols,
            database_url=database_url,
            entry_threshold=config.entry_threshold,
            exit_threshold=config.exit_threshold,
            max_gross_leverage=config.max_gross_leverage,
            max_symbol_leverage=config.max_symbol_leverage,
            max_margin_usage=config.max_margin_usage,
            order_detail_limit=args.order_detail_limit,
        )
        _print_threshold_recommendation(
            database_url=database_url,
            symbols=symbols,
            entry_threshold=config.entry_threshold,
            exit_threshold=config.exit_threshold,
        )
        collector_settings = CollectorSettings(
            bar_count=args.bar_count,
            tick_backfill_minutes=args.tick_backfill_minutes,
            include_depth=args.include_depth,
        )
        def on_cycle(result: LiveCycleResult, cycle_number: int) -> None:
            _print_order_outcomes(
                result,
                cycle_number,
                detail_limit=args.order_detail_limit,
            )
            if args.improvement_every_cycles > 0 and cycle_number % args.improvement_every_cycles == 0:
                try:
                    _run_improvement_packet(
                        config=config,
                        database_url=database_url,
                        symbols=symbols,
                        output_dir=args.improvement_output_dir,
                        run_id=f"live_cycle_{cycle_number}",
                        store_proposals=False,
                        include_backtest=False,
                        start_time_utc=session_started_at_utc,
                        end_time_utc=result.finished_at_utc,
                    )
                except ContinuousImprovementError:
                    print("Continuing live session; improvement snapshot is advisory only.", flush=True)

        results = run_live_session(
            config,
            minutes=args.minutes,
            poll_seconds=args.poll_seconds,
            on_cycle=on_cycle,
            database_url=database_url,
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
    if args.improvement_after_run:
        try:
            _run_improvement_packet(
                config=config,
                database_url=database_url,
                symbols=symbols,
                output_dir=args.improvement_output_dir,
                run_id="after_live_run",
                store_proposals=True,
                include_backtest=True,
                start_time_utc=results[0].started_at_utc if results else session_started_at_utc,
                end_time_utc=results[-1].finished_at_utc if results else datetime.now(timezone.utc),
            )
        except ContinuousImprovementError:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
