from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.execution import LiveTradingApprovalError
from mt5_crypto_bot.live import run_live_cycle


SYMBOLS = ("BAR/USD", "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD")


class FakeMT5:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def initialize(self, *args: object, **kwargs: object) -> bool:
        self.calls.append("initialize")
        return True

    def order_check(self, request: object) -> object:
        self.calls.append("order_check")
        raise AssertionError("missing approval must block before order_check")

    def order_send(self, request: object) -> object:
        self.calls.append("order_send")
        raise AssertionError("missing approval must block before order_send")


class LiveRunnerTests(unittest.TestCase):
    def test_live_cycle_without_approval_does_not_touch_mt5(self) -> None:
        fake_mt5 = FakeMT5()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(LiveTradingApprovalError):
                run_live_cycle(
                    BotConfig(database_url=str(root / "trading.db"), target_symbols=SYMBOLS),
                    database_url=root / "trading.db",
                    target_symbols=SYMBOLS,
                    symbol_map_path=root / "missing_symbol_map.json",
                    live_approval_file=root / "LIVE_APPROVED.json",
                    minutes_limit=1,
                    mt5_module=fake_mt5,
                )

        self.assertEqual(fake_mt5.calls, [])


if __name__ == "__main__":
    unittest.main()
