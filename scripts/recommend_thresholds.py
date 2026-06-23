"""Recommend ENTRY_THRESHOLD and EXIT_THRESHOLD from local trading.db data.

This script reads SQLite only. It does not connect to MT5 and does not change
live strategy parameters.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import ValidationError

from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.schemas import normalize_symbols
from mt5_crypto_bot.thresholds import recommend_thresholds_from_store


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Offline threshold recommendation from stored signals and M5 bars. "
            "No MT5 connection, no live orders, and no automatic parameter changes."
        )
    )
    parser.add_argument("--env-file", default=".env", help="Path to dotenv file.")
    parser.add_argument("--database-url", default=None, help="SQLite database URL/path.")
    parser.add_argument("--symbols", default=None, help="Comma-separated allowed symbols.")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        recommendation = recommend_thresholds_from_store(
            args.database_url or config.database_url,
            target_symbols=symbols,
            current_entry_threshold=config.entry_threshold,
            current_exit_threshold=config.exit_threshold,
        )
    except (ValidationError, ValueError) as exc:
        print("Threshold recommendation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Use only the active FX/crypto symbols from rules.md and constants.py.", file=sys.stderr)
        return 2

    payload = recommendation.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("Offline threshold recommendation completed.")
    print(f"Available: {recommendation.available}")
    print(f"Reason: {recommendation.reason}")
    print(
        "Current thresholds: "
        f"ENTRY_THRESHOLD={recommendation.current_entry_threshold:g}, "
        f"EXIT_THRESHOLD={recommendation.current_exit_threshold:g}"
    )
    print(
        "Recommended thresholds: "
        f"ENTRY_THRESHOLD={recommendation.recommended_entry_threshold:g}, "
        f"EXIT_THRESHOLD={recommendation.recommended_exit_threshold:g}"
    )
    print(f"Rows evaluated: {recommendation.evaluated_rows}")
    print(f"Pairs evaluated: {recommendation.evaluated_pairs}")
    if recommendation.best is not None:
        print(
            "Best metrics: "
            f"return_bps={recommendation.best.total_return_bps:.4g}, "
            f"drawdown_bps={recommendation.best.max_drawdown_bps:.4g}, "
            f"sharpe={recommendation.best.sharpe:.4g}, "
            f"trades={recommendation.best.trade_count}"
        )
    print("No parameter changes were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
