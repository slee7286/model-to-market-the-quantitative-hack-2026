"""Project-wide constants that are safe to import anywhere."""

ALLOWED_SYMBOLS: tuple[str, ...] = (
    "BAR/USD",
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
)

BAR_USD_DESCRIPTION = "BAR/USD is treated as HBAR/Hedera per information.md."
DEFAULT_TRADE_MODE = "dry_run"
DEFAULT_DATABASE_URL = "sqlite:///data/trading.db"
DEFAULT_STRATEGY_VERSION = "momo_v1"
