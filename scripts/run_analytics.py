"""Generate offline analytics and inactive retuning proposals."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import ValidationError

from mt5_crypto_bot.analytics import (
    AnalyticsConfig,
    AnalyticsError,
    generate_analytics_report_from_store,
    write_analytics_reports,
)
from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.schemas import normalize_symbols


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate offline trading analytics. This script reads local storage only, "
            "never connects to MT5, never places orders, and stores retuning proposals "
            "as inactive strategy versions."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to dotenv file for DATABASE_URL and TARGET_SYMBOLS. Defaults to .env.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite database URL/path. Defaults to DATABASE_URL from config.",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated allowed canonical symbols. Defaults to TARGET_SYMBOLS/config.",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/analytics",
        help="Directory for Markdown/CSV/JSON analytics reports.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional report filename suffix, for unattended automation run IDs.",
    )
    parser.add_argument(
        "--no-store-proposals",
        action="store_true",
        help="Generate the report without writing inactive strategy-version proposals.",
    )
    parser.add_argument(
        "--skip-shadow-evaluation",
        action="store_true",
        help="Skip champion/challenger shadow backtest evaluation.",
    )
    parser.add_argument(
        "--start-utc",
        default=None,
        help="Optional inclusive UTC start time for session-window analytics.",
    )
    parser.add_argument(
        "--end-utc",
        default=None,
        help="Optional inclusive UTC end time for session-window analytics.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        database_url = args.database_url or config.database_url
        report = generate_analytics_report_from_store(
            database_url,
            target_symbols=symbols,
            config=AnalyticsConfig(
                output_dir=args.output_dir,
                start_time_utc=_parse_datetime_arg(args.start_utc),
                end_time_utc=_parse_datetime_arg(args.end_utc),
            ),
            store_proposals=not args.no_store_proposals,
            include_shadow_evaluation=not args.skip_shadow_evaluation,
        )
    except (ValidationError, ValueError) as exc:
        print("Analytics configuration validation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Use only the active FX/crypto symbols from rules.md and constants.py.", file=sys.stderr)
        return 2
    except AnalyticsError as exc:
        print("Analytics generation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    paths = write_analytics_reports(report, args.output_dir, run_id=args.run_id)
    print("Offline analytics report completed.")
    print(f"Data label: {report.data_label}")
    print(f"Return: {report.metrics['return']:.6g}")
    print(f"Window equity change: {report.metrics['window_equity_change']:.2f}")
    print(f"Window return: {report.metrics['window_return']:.6g}")
    print(f"Max drawdown: {report.metrics['max_drawdown']:.6g}")
    print(f"15-minute Sharpe: {report.metrics['sharpe_15m']:.6g}")
    print(f"Trade count: {report.metrics['trade_count']}")
    print(f"Inactive proposals: {len(report.parameter_proposals)}")
    print(f"Markdown report: {paths['markdown']}")
    print(f"Metrics JSON: {paths['metrics_json']}")
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
