from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.schemas import (
    AccountSnapshot,
    Direction,
    ExecutionResult,
    ExecutionStatus,
    Fill,
    FillSide,
    OrderIntent,
    OrderSide,
    PositionSide,
    PositionSnapshot,
    RiskCheck,
    Signal,
    SignalDecision,
    StrategyParams,
    SymbolConfig,
)
from mt5_crypto_bot.storage import (
    REQUIRED_TABLES,
    SQLITE_SCHEMA_VERSION,
    ParquetArchiveWriter,
    SQLiteStore,
)


class SQLiteStorageTests(unittest.TestCase):
    def test_schema_creation_is_idempotent_from_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "data" / "trading.db"
            store = SQLiteStore(db_path)

            store.initialize_schema()
            store.initialize_schema()

            tables = store.list_tables()
            user_version = store.fetch_one("PRAGMA user_version")
            store.close()

        self.assertTrue(set(REQUIRED_TABLES).issubset(tables))
        self.assertEqual(user_version[0], SQLITE_SCHEMA_VERSION)

    def test_duplicate_bar_and_tick_writes_are_idempotent(self) -> None:
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                bar = {
                    "symbol": "BTC/USD",
                    "timeframe": "M5",
                    "time_utc": now,
                    "open": 100.0,
                    "high": 105.0,
                    "low": 99.0,
                    "close": 101.0,
                    "tick_volume": 10,
                    "spread": 2,
                    "real_volume": 0,
                }
                store.upsert_bars([bar])
                store.upsert_bars([bar])

                updated_bar = dict(bar)
                updated_bar["close"] = 102.0
                store.upsert_bars([updated_bar])

                tick = {
                    "symbol": "BTC/USD",
                    "time_msc": 1_782_128_800_000,
                    "time_utc": now,
                    "bid": 100.0,
                    "ask": 100.5,
                    "last": 100.25,
                    "volume": 1.0,
                    "flags": 0,
                }
                store.upsert_ticks([tick])
                store.upsert_ticks([tick])

                updated_tick = dict(tick)
                updated_tick["ask"] = 100.75
                store.upsert_ticks([updated_tick])

                bar_row = store.fetch_one("SELECT close FROM bars WHERE symbol = ?", ("BTC/USD",))
                tick_row = store.fetch_one("SELECT ask FROM ticks WHERE symbol = ?", ("BTC/USD",))

                self.assertEqual(store.count_rows("bars"), 1)
                self.assertEqual(store.count_rows("ticks"), 1)
                self.assertEqual(bar_row["close"], 102.0)
                self.assertEqual(tick_row["ask"], 100.75)

    def test_audit_records_insert_and_upsert(self) -> None:
        now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                store.upsert_symbol_metadata(
                    SymbolConfig(
                        symbol="BTC/USD",
                        broker_symbol="BTCUSD",
                        digits=2,
                        point=0.01,
                        trade_tick_size=0.01,
                        trade_tick_value=1.0,
                        trade_contract_size=1.0,
                        volume_min=0.01,
                        volume_max=100.0,
                        volume_step=0.01,
                        spread=20,
                        filling_mode=1,
                        trade_mode=4,
                        raw={"name": "BTCUSD"},
                    )
                )
                store.upsert_strategy_version(StrategyParams(), active=True, approved_by="test")
                store.upsert_signal(
                    Signal(
                        signal_id="sig-1",
                        created_at_utc=now,
                        strategy_version="momo_v1",
                        symbol="BTC/USD",
                        timeframe="M5",
                        direction=Direction.LONG,
                        score=1.5,
                        target_leverage=0.5,
                        target_volume=1.0,
                        features={"return_1h": 0.01},
                        decision=SignalDecision.ENTER,
                        reason="test signal",
                    )
                )
                store.insert_risk_check(
                    RiskCheck(
                        check_id="risk-1",
                        signal_id="sig-1",
                        checked_at_utc=now,
                        passed=True,
                        symbol="BTC/USD",
                        equity=1_000_000,
                        balance=1_000_000,
                        margin_usage=0.1,
                        gross_leverage=0.5,
                        reason="within caps",
                    )
                )
                store.upsert_order_intent(
                    OrderIntent(
                        client_order_id="order-1",
                        signal_id="sig-1",
                        created_at_utc=now,
                        symbol="BTC/USD",
                        side=OrderSide.BUY,
                        requested_volume=1.0,
                        requested_price=100.0,
                        stop_loss=98.0,
                        take_profit=104.0,
                    )
                )
                store.upsert_execution_result(
                    ExecutionResult(
                        client_order_id="order-1",
                        executed_at_utc=now,
                        status=ExecutionStatus.DRY_RUN,
                        symbol="BTC/USD",
                        requested_volume=1.0,
                        requested_price=100.0,
                        request={"dry_run": True},
                        result={"status": "not sent"},
                    )
                )
                store.insert_fill(
                    Fill(
                        deal_ticket=123,
                        order_ticket=456,
                        position_id=789,
                        symbol="BTC/USD",
                        filled_at_utc=now,
                        side=FillSide.BUY,
                        volume=1.0,
                        price=100.0,
                    )
                )
                store.insert_position_snapshot(
                    PositionSnapshot(
                        observed_at_utc=now,
                        symbol="BTC/USD",
                        ticket=789,
                        side=PositionSide.LONG,
                        volume=1.0,
                        price_open=100.0,
                        price_current=101.0,
                        profit=1.0,
                    )
                )
                store.upsert_account_snapshot(
                    AccountSnapshot(
                        observed_at_utc=now,
                        balance=1_000_000,
                        equity=1_000_100,
                        profit=100.0,
                        margin=1_000.0,
                        margin_free=999_100.0,
                        margin_level=100_010.0,
                        gross_leverage=0.5,
                        max_drawdown=0.0,
                    )
                )
                store.insert_order_book_snapshots(
                    [
                        {
                            "symbol": "BTC/USD",
                            "observed_at_utc": now,
                            "side": "bid",
                            "level": 1,
                            "price": 100.0,
                            "volume": 2.0,
                        }
                    ]
                )

                order = store.fetch_one(
                    "SELECT status, result_json FROM orders WHERE client_order_id = ?",
                    ("order-1",),
                )

                self.assertEqual(store.count_rows("symbol_metadata"), 1)
                self.assertEqual(store.count_rows("strategy_versions"), 1)
                self.assertEqual(store.count_rows("signals"), 1)
                self.assertEqual(store.count_rows("risk_checks"), 1)
                self.assertEqual(store.count_rows("orders"), 1)
                self.assertEqual(store.count_rows("fills"), 1)
                self.assertEqual(store.count_rows("positions_snapshots"), 1)
                self.assertEqual(store.count_rows("account_snapshots"), 1)
                self.assertEqual(store.count_rows("order_book_snapshots"), 1)
                self.assertEqual(order["status"], "dry_run")
                self.assertIn("not sent", order["result_json"])

    def test_invalid_symbols_are_rejected_by_storage_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                with self.assertRaises(ValueError):
                    store.upsert_bars(
                        [
                            {
                                "symbol": "DOGE/USD",
                                "timeframe": "M5",
                                "time_utc": datetime(2026, 6, 22, tzinfo=timezone.utc),
                                "open": 1.0,
                                "high": 1.0,
                                "low": 1.0,
                                "close": 1.0,
                            }
                        ]
                    )


class ParquetArchiveWriterTests(unittest.TestCase):
    def test_writer_skips_cleanly_without_pyarrow_or_writes_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = ParquetArchiveWriter(Path(tmpdir) / "parquet")
            path = writer.write_bars(
                [
                    {
                        "symbol": "BTC/USD",
                        "timeframe": "M5",
                        "time_utc": datetime(2026, 6, 22, tzinfo=timezone.utc),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                    }
                ]
            )

            if writer.available:
                self.assertIsNotNone(path)
                self.assertTrue(path.exists())
                self.assertEqual(path.suffix, ".parquet")
            else:
                self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main()
