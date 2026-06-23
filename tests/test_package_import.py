import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

EXPECTED_SYMBOLS = (
    "AUD/USD",
    "EUR/CHF",
    "EUR/GBP",
    "EUR/USD",
    "GBP/USD",
    "USD/CAD",
    "USD/CHF",
    "USD/JPY",
    "BAR/USD",
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
)


class PackageImportTests(unittest.TestCase):
    def test_package_imports_without_mt5(self) -> None:
        package = importlib.import_module("mt5_crypto_bot")

        self.assertEqual(package.DEFAULT_TRADE_MODE, "dry_run")
        self.assertEqual(package.ALLOWED_SYMBOLS, EXPECTED_SYMBOLS)

    def test_scaffold_modules_import_without_credentials(self) -> None:
        modules = [
            "analytics",
            "app",
            "config",
            "continuous_improvement",
            "constants",
            "data_collector",
            "execution",
            "features",
            "logging_setup",
            "mt5_client",
            "reporting",
            "retune",
            "risk",
            "schemas",
            "storage",
            "strategy",
            "symbols",
        ]

        for module_name in modules:
            importlib.import_module(f"mt5_crypto_bot.{module_name}")

    def test_default_config_snapshot_is_dry_run_only(self) -> None:
        from mt5_crypto_bot.config import default_config_snapshot

        snapshot = default_config_snapshot()

        self.assertEqual(snapshot["trade_mode"], "dry_run")
        self.assertEqual(snapshot["target_symbols"], EXPECTED_SYMBOLS)


if __name__ == "__main__":
    unittest.main()
