"""Compute and export deterministic feature snapshots from the local SQLite store."""

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

from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.features import (
    FeatureEngineeringError,
    compute_and_export_feature_snapshots,
    default_feature_output_path,
)
from mt5_crypto_bot.schemas import normalize_symbols


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export no-look-ahead M5 strategy feature snapshots from stored bars, ticks, "
            "and optional order-book data. This script is read-only and never contacts MT5."
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
        help="Override DATABASE_URL. Only local sqlite:/// URLs are supported for MVP export.",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated allowed canonical symbols. Defaults to TARGET_SYMBOLS/config.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path. Suffix .csv, .json, or .jsonl selects format.",
    )
    parser.add_argument(
        "--latest-only",
        action="store_true",
        help="Export only the latest feature row for each symbol.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        output = Path(args.output) if args.output else default_feature_output_path()
        database_url = args.database_url or config.database_url
        features, output_path = compute_and_export_feature_snapshots(
            database_url,
            output,
            target_symbols=symbols,
            latest_only=args.latest_only,
        )
    except ValidationError as exc:
        print("Configuration validation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Use only BAR/USD, BTC/USD, ETH/USD, SOL/USD, and XRP/USD.", file=sys.stderr)
        return 2
    except FeatureEngineeringError as exc:
        print("Feature snapshot export failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    print("Feature snapshot export completed.")
    print(f"Rows computed: {len(features)}")
    print(f"Output: {output_path}")
    if not features.empty:
        latest_time = features["feature_time_utc"].max()
        print(f"Latest feature time UTC: {latest_time}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
