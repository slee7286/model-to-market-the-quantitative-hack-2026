"""MT5 crypto bot package.

The scaffold is intentionally safe to import without MT5 installed or configured.
Live trading is not enabled by any module in this package.
"""

from mt5_crypto_bot.constants import ALLOWED_SYMBOLS, DEFAULT_TRADE_MODE

__all__ = ["ALLOWED_SYMBOLS", "DEFAULT_TRADE_MODE", "__version__"]

__version__ = "0.1.0"
