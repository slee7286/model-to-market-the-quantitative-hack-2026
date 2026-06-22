from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.dry_run import run_dry_run_cycle
from mt5_crypto_bot.storage import SQLiteStore


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
SYMBOLS = ("BAR/USD", "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD")


class EndToEndDryRunTests(unittest.TestCase):
    def test_fixture_cycle_records_signals_risk_checks_and_dry_run_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trading.db"
            result = run_dry_run_cycle(
                BotConfig(database_url=str(db_path), target_symbols=SYMBOLS),
                database_url=db_path,
                target_symbols=SYMBOLS,
                force_fixture=True,
                now_utc=NOW,
            )

            with SQLiteStore(db_path) as store:
                signals = store.count_rows("signals")
                risk_checks = store.count_rows("risk_checks")
                dry_run_orders = store.fetch_all(
                    "SELECT status, result_json FROM orders ORDER BY client_order_id"
                )

        self.assertEqual(result.data_mode, "synthetic_fixture_forced")
        self.assertGreaterEqual(signals, 5)
        self.assertGreaterEqual(risk_checks, 1)
        self.assertGreaterEqual(len(dry_run_orders), 1)
        self.assertGreaterEqual(result.execution_result.dry_run_count, 1)
        for row in dry_run_orders:
            payload = json.loads(row["result_json"])
            self.assertEqual(row["status"], "dry_run")
            self.assertFalse(payload["result"]["sent_to_mt5"])
            self.assertFalse(payload["result"]["order_check_called"])
            self.assertFalse(payload["result"]["order_send_called"])

    def test_missing_symbol_map_uses_non_live_fixture_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "trading.db"
            missing_map = root / "config" / "symbol_map.json"
            result = run_dry_run_cycle(
                BotConfig(database_url=str(db_path), target_symbols=SYMBOLS),
                database_url=db_path,
                target_symbols=SYMBOLS,
                symbol_map_path=missing_map,
                now_utc=NOW,
            )

        self.assertEqual(result.data_mode, "synthetic_fixture_fallback")
        self.assertIn("SymbolMapError", result.fallback_reason or "")
        self.assertGreaterEqual(result.risk_result.summary()["risk_checks"], 1)
        self.assertGreaterEqual(result.execution_result.dry_run_count, 1)


if __name__ == "__main__":
    unittest.main()
