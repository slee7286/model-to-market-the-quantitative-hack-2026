"""Project-wide constants that are safe to import anywhere."""

FOREX_SYMBOLS: tuple[str, ...] = (
    "AUD/USD",
    "EUR/CHF",
    "EUR/GBP",
    "EUR/USD",
    "GBP/USD",
    "USD/CAD",
    "USD/CHF",
    "USD/JPY",
)

CRYPTO_SYMBOLS: tuple[str, ...] = (
    "BAR/USD",
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
)

# The user-requested integrated session trades forex plus crypto only. Metals
# are allowed by rules.md but intentionally excluded from this build.
ALLOWED_SYMBOLS: tuple[str, ...] = FOREX_SYMBOLS + CRYPTO_SYMBOLS

ASSET_CLASS_BY_SYMBOL: dict[str, str] = {
    **{symbol: "forex" for symbol in FOREX_SYMBOLS},
    **{symbol: "crypto" for symbol in CRYPTO_SYMBOLS},
}

# Integrated PnL sprint profile: collect, audit, and permit fresh entries across
# the 8 FX pairs plus 5 crypto symbols, with risk gates enforcing the 28x cap.
PNL_SPRINT_ENTRY_SYMBOLS: tuple[str, ...] = ALLOWED_SYMBOLS

# Keep one-instrument conviction trades below the rules.md concentration line
# without increasing total intended gross leverage. A 0.89 / 0.11 split leaves a
# small opposite leg while targeting <= 90% single-instrument share.
DISCIPLINE_BALLAST_MAIN_SHARE = 0.89
DISCIPLINE_BALLAST_MIN_TRIGGER_LEVERAGE = 1.0
DISCIPLINE_BALLAST_MAX_TARGET_LEVERAGE = 3.25
# Broker ``volume_max`` is an order-size limit, not a position-size limit. Keep
# each cycle to a bounded burst of chunks so recovery-sprint positions can
# build quickly without flooding MT5, the terminal, or the audit log.
MAX_ORDER_INTENT_CHUNKS_PER_SIGNAL = 16

# FX moves are lower-volatility and normally produce smaller absolute momentum
# scores than crypto in this shared signal stack. Scale only the FX trend score
# so the global entry threshold can remain unchanged for the rest of the bot.
FX_MOMENTUM_SCORE_MULTIPLIER = 1.60

BROKER_STOP_DISTANCE_BUFFER_POINTS = 3.0

BAR_USD_DESCRIPTION = "BAR/USD is treated as HBAR/Hedera per information.md."
DEFAULT_TRADE_MODE = "dry_run"
DEFAULT_DATABASE_URL = "sqlite:///data/trading.db"
DEFAULT_STRATEGY_VERSION = "momo_v1"
