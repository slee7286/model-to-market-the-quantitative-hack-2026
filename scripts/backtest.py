"""Run offline strategy backtests and write comparison reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import ValidationError

from mt5_crypto_bot.backtest import (
    BacktestDataError,
    run_backtest_from_csv,
    run_backtest_from_store,
    run_synthetic_fixture_backtest,
    write_backtest_reports,
)
from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.schemas import normalize_symbols


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run offline M5/M15 strategy backtests. This script is read-only, "
            "never connects to MT5, and never places orders."
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
        "--bars-csv",
        action="append",
        default=[],
        help="Organizer/local historical bar CSV path. May be passed more than once.",
    )
    parser.add_argument(
        "--ticks-csv",
        action="append",
        default=[],
        help="Optional historical tick CSV path for spread estimates. May be repeated.",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated allowed canonical symbols. Defaults to TARGET_SYMBOLS/config.",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/backtests",
        help="Directory for Markdown/CSV/JSON reports.",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Use deterministic synthetic fixture data for smoke validation.",
    )
    parser.add_argument(
        "--allow-fixture-fallback",
        action="store_true",
        help="If local DB/CSV data is unavailable, write a clearly marked fixture report.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional report filename suffix, for unattended automation run IDs.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        database_url = args.database_url or config.database_url

        if args.fixture:
            comparison = run_synthetic_fixture_backtest(target_symbols=symbols)
        elif args.bars_csv:
            comparison = run_backtest_from_csv(
                args.bars_csv,
                ticks_csv_paths=args.ticks_csv,
                target_symbols=symbols,
                data_label="csv_history",
            )
        else:
            comparison = run_backtest_from_store(
                database_url,
                target_symbols=symbols,
                data_label=f"sqlite:{database_url}",
            )
    except (ValidationError, ValueError) as exc:
        print("Backtest configuration validation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Use only the active FX/crypto symbols from rules.md and constants.py.", file=sys.stderr)
        return 2
    except BacktestDataError as exc:
        if not args.allow_fixture_fallback:
            print("Backtest data load failed.", file=sys.stderr)
            print(str(exc), file=sys.stderr)
            print(
                "Run the collector first, pass --bars-csv, or use --fixture for smoke validation.",
                file=sys.stderr,
            )
            return 2
        print("Backtest data unavailable; using synthetic fixture fallback.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        comparison = run_synthetic_fixture_backtest(
            target_symbols=normalize_symbols(args.symbols) if args.symbols else load_config(
                env_file=args.env_file
            ).target_symbols
        )

    paths = write_backtest_reports(comparison, args.output_dir, run_id=args.run_id)
    print("Backtest comparison completed.")
    print(f"Data label: {comparison.data_label}")
    print(f"Fixture data: {comparison.is_fixture}")
    print(f"Selected live MVP strategy: {comparison.selected_strategy}")
    print(f"Best metric strategy: {comparison.best_metric_strategy}")
    print(f"Markdown report: {paths['markdown']}")
    print(f"Summary CSV: {paths['summary_csv']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
