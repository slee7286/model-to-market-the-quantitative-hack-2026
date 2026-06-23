"""Read-only MT5 symbol discovery and conservative broker mapping."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, BAR_USD_DESCRIPTION
from mt5_crypto_bot.mt5_client import (
    MT5ConnectionError,
    build_mt5_credentials,
    initialize_mt5,
    load_mt5_module,
    login_mt5,
    read_last_error,
    shutdown_mt5,
)


DEFAULT_SYMBOL_MAP_PATH = Path("config/symbol_map.json")
DEFAULT_SYMBOL_METADATA_PATH = Path("data/symbol_metadata.json")
SYMBOL_MAP_SCHEMA_VERSION = 1
AUTO_CONFIRM_SCORE = 90

SUMMARY_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "path",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "digits",
    "point",
    "trade_contract_size",
    "trade_tick_size",
    "trade_tick_value",
    "volume_min",
    "volume_max",
    "volume_step",
    "spread",
    "trade_mode",
    "filling_mode",
)

METADATA_FIELDS: tuple[str, ...] = (
    "digits",
    "point",
    "trade_contract_size",
    "trade_tick_size",
    "trade_tick_value",
    "trade_tick_value_profit",
    "trade_tick_value_loss",
    "volume_min",
    "volume_max",
    "volume_step",
    "volume_limit",
    "spread",
    "spread_float",
    "trade_mode",
    "filling_mode",
    "order_mode",
    "trade_exemode",
    "trade_stops_level",
    "trade_freeze_level",
    "ticks_bookdepth",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "description",
    "path",
)

MARGIN_FIELDS: tuple[str, ...] = (
    "margin_initial",
    "margin_maintenance",
    "margin_hedged",
    "margin_hedged_use_leg",
    "trade_calc_mode",
    "trade_liquidity_rate",
)

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True)
class SymbolBootstrapResult:
    """Result of one read-only symbol bootstrap run."""

    generated_at_utc: datetime
    symbol_map_path: Path
    metadata_path: Path
    symbol_map: dict[str, Any]
    metadata: dict[str, Any]

    @property
    def confirmed_symbols(self) -> tuple[str, ...]:
        canonical_to_broker = self.symbol_map.get("canonical_to_broker", {})
        return tuple(
            symbol for symbol in ALLOWED_SYMBOLS if canonical_to_broker.get(symbol) is not None
        )

    @property
    def unresolved_symbols(self) -> tuple[str, ...]:
        return tuple(
            symbol
            for symbol in ALLOWED_SYMBOLS
            if self.symbol_map["symbols"][symbol]["status"] != "confirmed"
        )


def bootstrap_symbols(
    config: BotConfig,
    *,
    symbol_map_path: str | Path = DEFAULT_SYMBOL_MAP_PATH,
    metadata_path: str | Path = DEFAULT_SYMBOL_METADATA_PATH,
    mt5_module: Any | None = None,
    shutdown: bool = True,
) -> SymbolBootstrapResult:
    """Discover broker symbols, write mapping files, and return the run summary.

    The MT5 calls used here are read-only: initialize, login, symbols_get,
    symbol_info, last_error, and shutdown.
    """
    credentials = build_mt5_credentials(config)
    mt5 = mt5_module or load_mt5_module()
    initialized = False
    try:
        initialize_mt5(credentials, mt5)
        initialized = True
        login_mt5(credentials, mt5)

        raw_symbols = read_symbols_get(mt5)
        existing_manual = load_existing_manual_mappings(symbol_map_path)
        generated_at = datetime.now(timezone.utc)
        mapping = build_symbol_map(raw_symbols, existing_manual, generated_at)
        metadata = build_symbol_metadata(mt5, mapping, raw_symbols, generated_at)

        write_json(symbol_map_path, mapping)
        write_json(metadata_path, metadata)

        return SymbolBootstrapResult(
            generated_at_utc=generated_at,
            symbol_map_path=Path(symbol_map_path),
            metadata_path=Path(metadata_path),
            symbol_map=mapping,
            metadata=metadata,
        )
    finally:
        if shutdown and initialized:
            shutdown_mt5(mt5)


def read_symbols_get(mt5_module: Any) -> list[dict[str, Any]]:
    """Return all MT5 symbols as JSON-safe dictionaries."""
    symbols = mt5_module.symbols_get()
    if symbols is None:
        raise MT5ConnectionError(
            "MT5 symbols_get returned no data. Check that the terminal is logged in and "
            "the broker account can see market symbols.",
            last_error=read_last_error(mt5_module),
        )
    return [_object_to_mapping(symbol) for symbol in symbols]


def build_symbol_map(
    raw_symbols: list[dict[str, Any]],
    manual_mappings: dict[str, str],
    generated_at_utc: datetime,
) -> dict[str, Any]:
    """Build a canonical-to-broker map with candidates for unresolved symbols."""
    available_by_name = {
        str(symbol.get("name", "")).strip(): symbol
        for symbol in raw_symbols
        if str(symbol.get("name", "")).strip()
    }
    canonical_to_broker: dict[str, str | None] = {}
    symbols_payload: dict[str, dict[str, Any]] = {}

    for canonical in ALLOWED_SYMBOLS:
        candidates = find_symbol_candidates(canonical, raw_symbols)
        manual_broker_symbol = manual_mappings.get(canonical)
        candidate_names = {candidate["broker_symbol"] for candidate in candidates}

        if manual_broker_symbol:
            if manual_broker_symbol in candidate_names:
                status = "confirmed"
                source = "manual"
                broker_symbol: str | None = manual_broker_symbol
                reason = "Manual mapping from existing config/symbol_map.json was confirmed."
            elif manual_broker_symbol not in available_by_name:
                status = "invalid_manual"
                source = "manual"
                broker_symbol = None
                reason = (
                    "Manual broker symbol is not present in MT5 symbols_get output; "
                    "metadata was not captured."
                )
            else:
                status = "invalid_manual"
                source = "manual"
                broker_symbol = None
                reason = (
                    "Manual broker symbol is available but does not conservatively match "
                    f"{canonical}; metadata was not captured."
                )
        elif len(candidates) == 1 and candidates[0]["score"] >= AUTO_CONFIRM_SCORE:
            status = "confirmed"
            source = "auto"
            broker_symbol = candidates[0]["broker_symbol"]
            reason = "Single high-confidence broker symbol candidate."
        elif not candidates:
            status = "missing"
            source = "auto"
            broker_symbol = None
            reason = "No conservative broker symbol candidates were found."
        else:
            status = "ambiguous"
            source = "auto"
            broker_symbol = None
            reason = "Multiple or low-confidence candidates require manual confirmation."

        canonical_to_broker[canonical] = broker_symbol
        symbols_payload[canonical] = {
            "canonical_symbol": canonical,
            "broker_symbol": broker_symbol,
            "status": status,
            "source": source,
            "reason": reason,
            "candidates": candidates,
        }
        if manual_broker_symbol and status != "confirmed":
            symbols_payload[canonical]["manual_broker_symbol"] = manual_broker_symbol

    return {
        "schema_version": SYMBOL_MAP_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc.isoformat(),
        "source": "MT5 symbols_get",
        "allowed_canonical_symbols": list(ALLOWED_SYMBOLS),
        "notes": [
            "Canonical symbols must remain within the rules.md FX+crypto allow-list.",
            "Metals are allowed by rules.md but intentionally excluded from this build.",
            BAR_USD_DESCRIPTION,
            "Only entries with status=confirmed are eligible for metadata capture.",
            "Ambiguous or missing entries require manual review in MT5 Market Watch.",
        ],
        "canonical_to_broker": canonical_to_broker,
        "symbols": symbols_payload,
    }


def build_symbol_metadata(
    mt5_module: Any,
    symbol_map: dict[str, Any],
    raw_symbols: list[dict[str, Any]],
    generated_at_utc: datetime,
) -> dict[str, Any]:
    """Capture metadata for confirmed symbol mappings only."""
    raw_by_name = {
        str(symbol.get("name", "")).strip(): symbol
        for symbol in raw_symbols
        if str(symbol.get("name", "")).strip()
    }
    metadata: dict[str, Any] = {}
    for canonical, broker_symbol in symbol_map["canonical_to_broker"].items():
        if broker_symbol is None:
            continue
        info = mt5_module.symbol_info(broker_symbol)
        raw_info = _object_to_mapping(info) if info is not None else raw_by_name.get(broker_symbol, {})
        metadata[canonical] = extract_symbol_metadata(
            canonical,
            broker_symbol,
            raw_info,
            generated_at_utc,
            info_source="symbol_info" if info is not None else "symbols_get",
        )

    return {
        "schema_version": SYMBOL_MAP_SCHEMA_VERSION,
        "generated_at_utc": generated_at_utc.isoformat(),
        "metadata_source": "MT5 symbol_info for confirmed mappings",
        "metadata": metadata,
    }


def find_symbol_candidates(canonical_symbol: str, raw_symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find conservative broker candidates for one canonical allowed symbol."""
    candidates: list[dict[str, Any]] = []
    for raw_symbol in raw_symbols:
        score, reasons = score_symbol_candidate(canonical_symbol, raw_symbol)
        if score <= 0:
            continue
        candidates.append(
            {
                "broker_symbol": str(raw_symbol.get("name", "")).strip(),
                "score": score,
                "reasons": reasons,
                "summary": summarize_symbol(raw_symbol),
            }
        )
    return sorted(candidates, key=lambda item: (-item["score"], item["broker_symbol"]))


def score_symbol_candidate(canonical_symbol: str, raw_symbol: dict[str, Any]) -> tuple[int, list[str]]:
    """Score a broker symbol candidate without considering unsupported instruments."""
    name = str(raw_symbol.get("name", "")).strip()
    if not name:
        return 0, []

    base, quote = canonical_symbol.split("/")
    aliases = canonical_aliases(canonical_symbol)
    quote_code = compact(quote)
    compact_name = compact(name)
    upper_name = name.upper()
    currency_base = compact(raw_symbol.get("currency_base"))
    currency_profit = compact(raw_symbol.get("currency_profit"))
    currency_margin = compact(raw_symbol.get("currency_margin"))
    compact_text = compact(
        " ".join(
            str(raw_symbol.get(field, ""))
            for field in ("name", "description", "path", "currency_base", "currency_profit")
        )
    )

    score = 0
    reasons: list[str] = []
    expected_compacts = [f"{alias}{quote_code}" for alias in aliases]

    if upper_name == canonical_symbol or compact_name in expected_compacts:
        score = max(score, 100)
        reasons.append("exact symbol/name match to canonical base and quote")

    for expected in expected_compacts:
        if compact_name.startswith(expected) and compact_name != expected:
            score = max(score, 85)
            reasons.append("broker symbol starts with canonical base and quote")

    if currency_base in aliases and currency_profit == quote_code:
        score = max(score, 95)
        reasons.append("currency_base and currency_profit match canonical pair")
    elif currency_base in aliases and currency_margin == quote_code:
        score = max(score, 80)
        reasons.append("currency_base and currency_margin match canonical pair")

    if any(alias in compact_text for alias in aliases) and quote_code in compact_text:
        score = max(score, 60)
        reasons.append("symbol text contains canonical base alias and quote")

    if canonical_symbol == "BAR/USD":
        if "HBAR" in compact_text or "HEDERA" in compact_text:
            score = min(100, max(score + 10, 90))
            reasons.append("BAR/USD is treated as HBAR/Hedera per information.md")
        elif compact_name.startswith("BARUSD") or upper_name == "BAR/USD":
            reasons.append("verify manually that BAR broker symbol represents HBAR/Hedera")

    return score, sorted(set(reasons))


def canonical_aliases(canonical_symbol: str) -> tuple[str, ...]:
    """Return broker-name aliases for a canonical allowed symbol."""
    aliases: dict[str, tuple[str, ...]] = {
        "AUD/USD": ("AUD",),
        "BAR/USD": ("HBAR", "BAR"),
        "BTC/USD": ("BTC", "XBT"),
        "EUR/CHF": ("EUR",),
        "EUR/GBP": ("EUR",),
        "EUR/USD": ("EUR",),
        "ETH/USD": ("ETH",),
        "GBP/USD": ("GBP",),
        "SOL/USD": ("SOL",),
        "USD/CAD": ("USD",),
        "USD/CHF": ("USD",),
        "USD/JPY": ("USD",),
        "XRP/USD": ("XRP",),
    }
    return aliases[canonical_symbol]


def summarize_symbol(raw_symbol: dict[str, Any]) -> dict[str, Any]:
    """Return a concise candidate summary safe for manual review."""
    return {
        field: _json_safe(raw_symbol[field])
        for field in SUMMARY_FIELDS
        if field in raw_symbol and raw_symbol[field] is not None
    }


def extract_symbol_metadata(
    canonical_symbol: str,
    broker_symbol: str,
    raw_info: dict[str, Any],
    generated_at_utc: datetime,
    *,
    info_source: str,
) -> dict[str, Any]:
    """Extract the metadata fields required by Prompt 05."""
    metadata = {
        "canonical_symbol": canonical_symbol,
        "broker_symbol": broker_symbol,
        "observed_at_utc": generated_at_utc.isoformat(),
        "info_source": info_source,
    }
    for field in METADATA_FIELDS:
        metadata[field] = _json_safe(raw_info.get(field))

    metadata["contract_size"] = metadata["trade_contract_size"]
    metadata["tick_size"] = metadata["trade_tick_size"]
    metadata["tick_value"] = metadata["trade_tick_value"]
    metadata["margin"] = {
        field: _json_safe(raw_info[field])
        for field in MARGIN_FIELDS
        if field in raw_info and raw_info[field] is not None
    }
    metadata["raw"] = _json_safe(raw_info)
    return metadata


def load_existing_manual_mappings(path: str | Path) -> dict[str, str]:
    """Load existing user-confirmed canonical-to-broker mappings if present."""
    map_path = Path(path)
    if not map_path.exists():
        return {}
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    mappings: dict[str, str] = {}
    raw_mapping = payload.get("canonical_to_broker", {})
    if isinstance(raw_mapping, dict):
        for canonical in ALLOWED_SYMBOLS:
            broker_symbol = raw_mapping.get(canonical)
            if isinstance(broker_symbol, str) and broker_symbol.strip():
                mappings[canonical] = broker_symbol.strip()

    raw_symbols = payload.get("symbols", {})
    if isinstance(raw_symbols, dict):
        for canonical in ALLOWED_SYMBOLS:
            entry = raw_symbols.get(canonical)
            if not isinstance(entry, dict):
                continue
            broker_symbol = entry.get("broker_symbol")
            status = entry.get("status")
            if isinstance(broker_symbol, str) and broker_symbol.strip() and status == "confirmed":
                mappings[canonical] = broker_symbol.strip()
    return mappings


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Write a JSON file, creating its parent directory if needed."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def compact(value: Any) -> str:
    """Uppercase a value and remove separators for conservative comparisons."""
    return _NON_ALNUM.sub("", str(value or "").upper())


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


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)
