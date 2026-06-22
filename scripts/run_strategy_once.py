"""Run one dry-run strategy cycle from local SQLite market data."""

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
from mt5_crypto_bot.schemas import normalize_symbols
from mt5_crypto_bot.strategy import StrategyEngineError, run_strategy_once_from_store


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate dry-run momo_v1 strategy signals from stored feature data. "
            "This script never contacts MT5 and never places orders."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to dotenv file for DATABASE_URL, TARGET_SYMBOLS, and strategy config.",
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
        "--all-snapshots",
        action="store_true",
        help="Generate signals for every stored feature row instead of latest per symbol.",
    )
    parser.add_argument(
        "--now-utc",
        default=None,
        help="Override current UTC time for freshness checks, ISO-8601 format.",
    )
    parser.add_argument(
        "--no-freshness-check",
        action="store_true",
        help="Disable stale bar/tick checks for offline fixture inspection only.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        database_url = args.database_url or config.database_url
        now_utc = _parse_now(args.now_utc)
        result = run_strategy_once_from_store(
            database_url,
            target_symbols=symbols,
            params=config.strategy_params(),
            now_utc=now_utc,
            latest_only=not args.all_snapshots,
            enforce_freshness=not args.no_freshness_check,
        )
    except ValidationError as exc:
        print("Strategy configuration validation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Use dry_run mode and only allowed crypto symbols.", file=sys.stderr)
        return 2
    except (ValueError, StrategyEngineError) as exc:
        print("Strategy dry-run cycle failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print(
            "Run symbol bootstrap and the data collector first, then rerun this script.",
            file=sys.stderr,
        )
        return 2

    print("Dry-run strategy cycle completed.")
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    for signal in result.signals:
        print(
            json.dumps(
                {
                    "signal_id": signal.signal_id,
                    "symbol": signal.symbol,
                    "decision": signal.decision,
                    "direction": signal.direction,
                    "score": signal.score,
                    "target_leverage": signal.target_leverage,
                    "target_volume": signal.target_volume,
                    "reason": signal.reason,
                },
                sort_keys=True,
            )
        )
    if result.order_intents:
        print("Generated dry-run order intents:")
        for intent in result.order_intents:
            print(
                json.dumps(
                    {
                        "client_order_id": intent.client_order_id,
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "requested_volume": intent.requested_volume,
                        "requested_price": intent.requested_price,
                        "stop_loss": intent.stop_loss,
                        "take_profit": intent.take_profit,
                    },
                    sort_keys=True,
                )
            )
    return 0


def _parse_now(value: str | None) -> datetime | None:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
