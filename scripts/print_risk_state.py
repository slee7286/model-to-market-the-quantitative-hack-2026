"""Print current MT5 account and portfolio risk state with read-only calls."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


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
    build_mt5_credentials,
    initialize_mt5,
    load_mt5_module,
    login_mt5,
    read_last_error,
    shutdown_mt5,
)
from mt5_crypto_bot.risk import (
    AccountRiskState,
    MarketRiskState,
    PositionRiskState,
    RiskContext,
    RiskEngine,
    RiskLimits,
    read_kill_switch,
)
from mt5_crypto_bot.schemas import PositionSide, SymbolConfig, normalize_symbols
from mt5_crypto_bot.symbols import DEFAULT_SYMBOL_MAP_PATH


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print current MT5 account and risk state using read-only calls only. "
            "This script never calls order_check or order_send."
        )
    )
    parser.add_argument("--env-file", default=".env", help="Path to local dotenv file.")
    parser.add_argument(
        "--symbol-map",
        default=str(DEFAULT_SYMBOL_MAP_PATH),
        help="Confirmed canonical-to-broker symbol map path.",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated canonical symbols. Defaults to TARGET_SYMBOLS/config.",
    )
    parser.add_argument(
        "--kill-switch-file",
        default="config/KILL_SWITCH",
        help="Optional local kill-switch file path.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    initialized = False
    try:
        config = load_config(env_file=args.env_file)
        symbols = normalize_symbols(args.symbols) if args.symbols else config.target_symbols
        broker_map = load_broker_symbol_map(args.symbol_map, symbols)
        credentials = build_mt5_credentials(config)
        mt5 = load_mt5_module()

        initialize_mt5(credentials, mt5)
        initialized = True
        login_mt5(credentials, mt5)

        context = build_risk_context_from_mt5(
            mt5,
            symbols=symbols,
            broker_map=broker_map,
            kill_switch_file=args.kill_switch_file,
        )
        summary = RiskEngine(RiskLimits.from_config(config)).summarize_context(context)
        summary["symbol_map_source"] = str(args.symbol_map)
        summary["broker_symbols"] = broker_map
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except ValidationError as exc:
        print("Risk-state configuration validation failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("Keep TRADE_MODE=dry_run and use only allowed FX/crypto symbols.", file=sys.stderr)
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
        print("MT5 risk-state read failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        if exc.last_error:
            print("MT5 last_error:", file=sys.stderr)
            print(json.dumps(exc.last_error, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    except MT5Error as exc:
        print("MT5 risk-state read failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if initialized:
            shutdown_mt5(locals().get("mt5"))


def build_risk_context_from_mt5(
    mt5_module: Any,
    *,
    symbols: tuple[str, ...],
    broker_map: dict[str, str],
    kill_switch_file: str | Path | None,
) -> RiskContext:
    now = datetime.now(timezone.utc)
    account_raw = _object_to_mapping(mt5_module.account_info())
    if not account_raw:
        raise MT5ConnectionError(
            "MT5 account_info returned no data.",
            last_error=read_last_error(mt5_module),
        )
    account_raw.pop("login", None)
    account = AccountRiskState(
        observed_at_utc=now,
        balance=_float(account_raw.get("balance")),
        equity=_float(account_raw.get("equity")),
        margin=_float(account_raw.get("margin")),
        margin_free=_optional_float(account_raw.get("margin_free")),
        margin_level=_optional_float(account_raw.get("margin_level")),
        gross_leverage=0.0,
        max_drawdown=0.0,
        leverage=_optional_float(account_raw.get("leverage")) or 30.0,
    )

    metadata: dict[str, SymbolConfig] = {}
    market: dict[str, MarketRiskState] = {}
    broker_to_canonical = {broker: canonical for canonical, broker in broker_map.items()}
    for canonical in symbols:
        broker_symbol = broker_map[canonical]
        info = _object_to_mapping(mt5_module.symbol_info(broker_symbol))
        if info:
            metadata[canonical] = _symbol_config_from_info(canonical, broker_symbol, info)
        tick = _object_to_mapping(mt5_module.symbol_info_tick(broker_symbol))
        if tick:
            tick_time = tick.get("time_msc") or tick.get("time") or now
            market[canonical] = MarketRiskState(
                symbol=canonical,
                observed_at_utc=_datetime_from_tick_time(tick_time),
                bid=_optional_float(tick.get("bid")),
                ask=_optional_float(tick.get("ask")),
                last=_optional_float(tick.get("last")),
            )

    positions = read_position_states(mt5_module, broker_to_canonical, now)
    return RiskContext(
        account=account,
        symbol_metadata=metadata,
        positions=positions,
        market=market,
        now_utc=now,
        kill_switch_active=read_kill_switch(kill_switch_file),
    )


def read_position_states(
    mt5_module: Any,
    broker_to_canonical: dict[str, str],
    observed_at_utc: datetime,
) -> tuple[PositionRiskState, ...]:
    raw_positions = mt5_module.positions_get()
    if raw_positions is None:
        return tuple()
    buy_type = getattr(mt5_module, "POSITION_TYPE_BUY", 0)
    sell_type = getattr(mt5_module, "POSITION_TYPE_SELL", 1)
    positions: list[PositionRiskState] = []
    for raw_position in raw_positions:
        raw = _object_to_mapping(raw_position)
        broker_symbol = str(raw.get("symbol", "")).strip()
        canonical = broker_to_canonical.get(broker_symbol)
        if canonical is None:
            continue
        position_type = raw.get("type")
        if position_type == buy_type:
            side = PositionSide.LONG
        elif position_type == sell_type:
            side = PositionSide.SHORT
        else:
            side = PositionSide.FLAT
        positions.append(
            PositionRiskState(
                symbol=canonical,
                side=side,
                volume=_float(raw.get("volume")),
                price_current=_optional_float(raw.get("price_current")),
                price_open=_optional_float(raw.get("price_open")),
                observed_at_utc=observed_at_utc,
            )
        )
    return tuple(positions)


def load_broker_symbol_map(path: str | Path, symbols: tuple[str, ...]) -> dict[str, str]:
    map_path = Path(path)
    if not map_path.exists():
        return {symbol: symbol for symbol in symbols}
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {symbol: symbol for symbol in symbols}
    raw_map = payload.get("canonical_to_broker", {})
    result: dict[str, str] = {}
    for symbol in symbols:
        broker_symbol = raw_map.get(symbol) if isinstance(raw_map, dict) else None
        result[symbol] = broker_symbol if isinstance(broker_symbol, str) and broker_symbol else symbol
    return result


def _symbol_config_from_info(
    canonical: str,
    broker_symbol: str,
    info: dict[str, Any],
) -> SymbolConfig:
    return SymbolConfig(
        symbol=canonical,
        broker_symbol=broker_symbol,
        digits=info.get("digits"),
        point=info.get("point"),
        trade_tick_size=info.get("trade_tick_size"),
        trade_tick_value=info.get("trade_tick_value"),
        trade_contract_size=info.get("trade_contract_size"),
        volume_min=info.get("volume_min"),
        volume_max=info.get("volume_max"),
        volume_step=info.get("volume_step"),
        spread=info.get("spread"),
        filling_mode=info.get("filling_mode"),
        trade_mode=info.get("trade_mode"),
        raw={key: _json_safe(value) for key, value in info.items() if key != "login"},
    )


def _object_to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        return dict(asdict())
    try:
        return dict(vars(value))
    except TypeError:
        pass
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if not callable(item):
            result[name] = item
    return result


def _datetime_from_tick_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _float(value: Any) -> float:
    number = _optional_float(value)
    return 0.0 if number is None else number


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
