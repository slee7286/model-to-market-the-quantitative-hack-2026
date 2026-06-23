"""Run the read-only MT5 market data collector with bounded runtime options."""

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
from mt5_crypto_bot.data_collector import (
    DEFAULT_BAR_COUNT,
    DEFAULT_FRESHNESS_SECONDS,
    DEFAULT_POLL_SECONDS,
    MIN_POLL_SECONDS,
    CollectorSettings,
    MarketDataCollectorError,
    SymbolMapError,
    run_bounded_collection,
)
from mt5_crypto_bot.mt5_client import (
    MT5ConfigurationError,
    MT5ConnectionError,
    MT5DependencyError,
    MT5Error,
)
from mt5_crypto_bot.storage import ParquetArchiveWriter
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect read-only MT5 market data for confirmed allowed FX/crypto symbols. "
            "No orders are placed and order_send is never called."
        )
    )
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one collection cycle. This is the default when --minutes is omitted.",
    )
    run_group.add_argument(
        "--minutes",
        type=float,
        help="Run for a bounded number of minutes.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help=f"Seconds between cycles for --minutes runs. Minimum {MIN_POLL_SECONDS:g}.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to dotenv file with local MT5 credentials. Defaults to .env.",
    )
    parser.add_argument(
        "--symbol-map",
        default=str(DEFAULT_SYMBOL_MAP_PATH),
        help="Path to confirmed canonical-to-broker mapping JSON.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL for the local SQLite store.",
    )
    parser.add_argument(
        "--bar-count",
        type=int,
        default=DEFAULT_BAR_COUNT,
        help="Number of recent M1/M5 bars to request per symbol and timeframe.",
    )
    parser.add_argument(
        "--tick-backfill-minutes",
        type=float,
        default=None,
        help="Optionally backfill ticks from the last N minutes using copy_ticks_from.",
    )
    parser.add_argument(
        "--tick-backfill-count",
        type=int,
        default=1_000,
        help="Maximum tick count per symbol for optional backfill.",
    )
    parser.add_argument(
        "--include-depth",
        action="store_true",
        help="Optionally collect market depth with market_book_add/get/release if available.",
    )
    parser.add_argument(
        "--max-depth-levels",
        type=int,
        default=5,
        help="Maximum market-depth levels per side to store when --include-depth is used.",
    )
    parser.add_argument(
        "--freshness-seconds",
        type=float,
        default=DEFAULT_FRESHNESS_SECONDS,
        help="Mark a symbol stale if the latest tick is older than this many seconds.",
    )
    parser.add_argument(
        "--parquet",
        action="store_true",
        help="Also archive bars/ticks to Parquet when pyarrow is installed.",
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
        settings = CollectorSettings(
            bar_count=args.bar_count,
            tick_backfill_minutes=args.tick_backfill_minutes,
            tick_backfill_count=args.tick_backfill_count,
            include_depth=args.include_depth,
            max_depth_levels=args.max_depth_levels,
            freshness_seconds=args.freshness_seconds,
        )
        parquet_writer = ParquetArchiveWriter(config.parquet_dir) if args.parquet else None
        results = run_bounded_collection(
            config,
            symbol_map_path=args.symbol_map,
            database_url=args.database_url,
            settings=settings,
            once=run_once,
            minutes=args.minutes,
            poll_seconds=args.poll_seconds,
            parquet_writer=parquet_writer,
        )
    except ValidationError as exc:
        print("Configuration validation failed before MT5 was contacted.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Keep TRADE_MODE=dry_run and use only allowed FX/crypto symbols.", file=sys.stderr)
        return 2
    except SymbolMapError as exc:
        print("Symbol map check failed before MT5 was contacted.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    except MT5DependencyError as exc:
        print("MT5 dependency check failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    except MT5ConfigurationError as exc:
        print("MT5 configuration check failed before connection.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    except MT5ConnectionError as exc:
        print("MT5 data collector connection failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        if exc.last_error:
            print("MT5 last_error:", file=sys.stderr)
            print(json.dumps(exc.last_error, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    except (MT5Error, MarketDataCollectorError) as exc:
        print("Market data collector failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    total_bars = sum(result.bars_written for result in results)
    total_ticks = sum(result.ticks_written for result in results)
    total_metadata = sum(result.metadata_written for result in results)
    total_depth = sum(result.order_book_rows_written for result in results)
    total_requests = sum(result.request_count for result in results)
    print("Read-only market data collection completed.")
    print(f"Cycles: {len(results)}")
    print(f"Bars written: {total_bars}")
    print(f"Ticks written: {total_ticks}")
    print(f"Metadata rows upserted: {total_metadata}")
    print(f"Order-book rows written: {total_depth}")
    print(f"Approximate MT5 requests: {total_requests}")
    if results:
        print("Last cycle:")
        print(json.dumps(results[-1].as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
