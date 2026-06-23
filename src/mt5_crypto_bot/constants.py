"""Project-wide constants that are safe to import anywhere."""

ALLOWED_SYMBOLS: tuple[str, ...] = (
    "BAR/USD",
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
)

# Qualification PnL sprint profile based on the latest live/replay reports:
# collect and audit all allowed symbols, but open/add exposure only where the
# collected data showed positive return contribution.
PNL_SPRINT_ENTRY_SYMBOLS: tuple[str, ...] = (
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
)

BROKER_STOP_DISTANCE_BUFFER_POINTS = 3.0

BAR_USD_DESCRIPTION = "BAR/USD is treated as HBAR/Hedera per information.md."
DEFAULT_TRADE_MODE = "dry_run"
DEFAULT_DATABASE_URL = "sqlite:///data/trading.db"
DEFAULT_STRATEGY_VERSION = "momo_v1"
