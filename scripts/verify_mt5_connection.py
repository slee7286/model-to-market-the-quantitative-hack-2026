"""Verify read-only MT5 connectivity and print sanitized account status."""

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
from mt5_crypto_bot.mt5_client import (
    MT5ConfigurationError,
    MT5ConnectionError,
    MT5DependencyError,
    MT5Error,
    verify_mt5_connection,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify MT5 initialize/login/account access using read-only calls only. "
            "TRADE_MODE must remain dry_run."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to dotenv file with local MT5 credentials. Defaults to .env.",
    )
    return parser.parse_args(argv)


def print_section(title: str, payload: dict[str, object]) -> None:
    print(f"\n{title}")
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(env_file=args.env_file)
        snapshot = verify_mt5_connection(config)
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
        print("MT5 connection verification failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        if exc.last_error:
            print("MT5 last_error:", file=sys.stderr)
            print(json.dumps(exc.last_error, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    except MT5Error as exc:
        print("MT5 verification failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    print("MT5 connection verification succeeded.")
    print("Mode: read-only verification; TRADE_MODE=dry_run.")
    print_section("Terminal info", snapshot.terminal_info)
    print_section("Account info", snapshot.account_info)
    print_section("MT5 last_error", snapshot.last_error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
