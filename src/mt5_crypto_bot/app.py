"""Command-line entry point for the scaffold package."""

from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_TRADE_MODE


def main() -> int:
    """Print a minimal safety-oriented status message."""
    symbols = ", ".join(ALLOWED_SYMBOLS)
    print(f"mt5-crypto-bot scaffold ready; trade_mode={DEFAULT_TRADE_MODE}; symbols={symbols}")
    return 0
