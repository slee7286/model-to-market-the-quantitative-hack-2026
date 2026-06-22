"""Bootstrap MT5 crypto symbol mapping and metadata with read-only calls."""

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
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS
from mt5_crypto_bot.mt5_client import (
    MT5ConfigurationError,
    MT5ConnectionError,
    MT5DependencyError,
    MT5Error,
)
from mt5_crypto_bot.symbols import (
    DEFAULT_SYMBOL_MAP_PATH,
    DEFAULT_SYMBOL_METADATA_PATH,
    bootstrap_symbols,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover broker symbols for BAR/USD, BTC/USD, ETH/USD, SOL/USD, and "
            "XRP/USD using read-only MT5 calls. Ambiguous mappings are written as "
            "candidates and are not auto-selected."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to dotenv file with local MT5 credentials. Defaults to .env.",
    )
    parser.add_argument(
        "--symbol-map",
        default=str(DEFAULT_SYMBOL_MAP_PATH),
        help="Output mapping JSON path. Defaults to config/symbol_map.json.",
    )
    parser.add_argument(
        "--metadata",
        default=str(DEFAULT_SYMBOL_METADATA_PATH),
        help="Output metadata JSON path. Defaults to data/symbol_metadata.json.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(env_file=args.env_file)
        result = bootstrap_symbols(
            config,
            symbol_map_path=args.symbol_map,
            metadata_path=args.metadata,
        )
    except ValidationError as exc:
        print("Configuration validation failed before MT5 was contacted.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Keep TRADE_MODE=dry_run and use only allowed crypto symbols.", file=sys.stderr)
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
        print("MT5 symbol bootstrap failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        if exc.last_error:
            print("MT5 last_error:", file=sys.stderr)
            print(json.dumps(exc.last_error, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    except MT5Error as exc:
        print("MT5 symbol bootstrap failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    print("MT5 symbol bootstrap completed using read-only calls.")
    print(f"Allowed canonical symbols: {', '.join(ALLOWED_SYMBOLS)}")
    print(f"Wrote symbol map: {result.symbol_map_path}")
    print(f"Wrote symbol metadata: {result.metadata_path}")

    print("\nMapping status")
    for canonical in ALLOWED_SYMBOLS:
        entry = result.symbol_map["symbols"][canonical]
        broker_symbol = entry["broker_symbol"] or "<manual required>"
        print(f"- {canonical}: {entry['status']} -> {broker_symbol}")
        if entry["status"] != "confirmed":
            candidates = entry.get("candidates", [])
            print(f"  reason: {entry['reason']}")
            print(f"  candidates written: {len(candidates)}")

    if result.unresolved_symbols:
        unresolved = ", ".join(result.unresolved_symbols)
        print(
            "\nManual confirmation required before these symbols can be used: "
            f"{unresolved}",
            file=sys.stderr,
        )
        print(
            "Inspect MT5 Market Watch, then edit config/symbol_map.json "
            "canonical_to_broker entries and rerun this script.",
            file=sys.stderr,
        )
        return 1

    print("\nAll allowed crypto symbols have confirmed broker mappings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
