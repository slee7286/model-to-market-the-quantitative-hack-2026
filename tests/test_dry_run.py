from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.dry_run import run_dry_run_cycle, run_dry_run_settlement_once
from mt5_crypto_bot.schemas import PositionSide, PositionSnapshot
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

    def test_settlement_records_dry_run_close_for_latest_open_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trading.db"
            config = BotConfig(database_url=str(db_path), target_symbols=SYMBOLS)
            run_dry_run_cycle(
                config,
                database_url=db_path,
                target_symbols=SYMBOLS,
                force_fixture=True,
                now_utc=NOW,
            )
            with SQLiteStore(db_path) as store:
                store.insert_position_snapshot(
                    PositionSnapshot(
                        observed_at_utc=NOW + timedelta(seconds=1),
                        symbol="BTC/USD",
                        ticket=123,
                        side=PositionSide.LONG,
                        volume=0.5,
                        price_open=70_000.0,
                        price_current=70_250.0,
                    )
                )

            settlement = run_dry_run_settlement_once(
                db_path,
                config=config,
                target_symbols=SYMBOLS,
                now_utc=NOW + timedelta(seconds=2),
            )

            with SQLiteStore(db_path) as store:
                rows = store.fetch_all(
                    """
                    SELECT client_order_id, symbol, side, status, result_json
                    FROM orders
                    WHERE client_order_id LIKE 'settle-%'
                    """
                )

        self.assertEqual(len(settlement.order_intents), 1)
        self.assertEqual(settlement.order_intents[0].symbol, "BTC/USD")
        self.assertEqual(settlement.order_intents[0].side, "sell")
        self.assertEqual(settlement.execution_result.dry_run_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "dry_run")
        payload = json.loads(rows[0]["result_json"])
        self.assertFalse(payload["result"]["sent_to_mt5"])
        self.assertFalse(payload["result"]["order_check_called"])
        self.assertFalse(payload["result"]["order_send_called"])


if __name__ == "__main__":
    unittest.main()
