from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.data_collector import (
    CollectorSettings,
    MarketDataCollector,
    SymbolMapError,
    load_confirmed_symbol_map,
    run_bounded_collection,
)
from mt5_crypto_bot.storage import SQLiteStore


def make_config(terminal_path: Path, database_url: str) -> BotConfig:
    return BotConfig(
        mt5_path=terminal_path,
        mt5_login="123456789",
        mt5_password="dummy-password",
        mt5_server="Demo-Server",
        trade_mode="dry_run",
        target_symbols=("BTC/USD", "ETH/USD"),
        database_url=database_url,
    )


def write_symbol_map(path: Path, *, eth_status: str = "confirmed") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "canonical_to_broker": {
                    "BTC/USD": "BTCUSD",
                    "ETH/USD": "ETHUSD",
                },
                "symbols": {
                    "BTC/USD": {"status": "confirmed", "broker_symbol": "BTCUSD"},
                    "ETH/USD": {"status": eth_status, "broker_symbol": "ETHUSD"},
                },
            }
        ),
        encoding="utf-8",
    )


class FakeMT5MarketData:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    COPY_TICKS_ALL = 0
    BOOK_TYPE_SELL = 1
    BOOK_TYPE_BUY = 2

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.shutdown_called = False
        self.now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)

    def initialize(self, path: str, *, timeout: int) -> bool:
        self.calls.append("initialize")
        return True

    def login(self, login: int, *, password: str, server: str, timeout: int) -> bool:
        self.calls.append("login")
        return True

    def shutdown(self) -> bool:
        self.calls.append("shutdown")
        self.shutdown_called = True
        return True

    def last_error(self) -> tuple[int, str]:
        self.calls.append("last_error")
        return (0, "OK")

    def symbol_info(self, symbol: str) -> SimpleNamespace:
        self.calls.append(f"symbol_info:{symbol}")
        return SimpleNamespace(
            name=symbol,
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
            margin_initial=0.0,
        )

    def copy_rates_from(
        self,
        symbol: str,
        timeframe: int,
        date_from: datetime,
        count: int,
    ) -> list[dict[str, object]]:
        self.calls.append(f"copy_rates_from:{symbol}:{timeframe}")
        base = int((self.now - timedelta(minutes=5)).timestamp())
        return [
            {
                "time": base,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "tick_volume": 10,
                "spread": 2,
                "real_volume": 1,
            },
            {
                "time": base + 60,
                "open": 100.5,
                "high": 102.0,
                "low": 100.0,
                "close": 101.5,
                "tick_volume": 12,
                "spread": 2,
                "real_volume": 1,
            },
        ][:count]

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace:
        self.calls.append(f"symbol_info_tick:{symbol}")
        time_msc = int(self.now.timestamp() * 1000)
        return SimpleNamespace(
            time=int(self.now.timestamp()),
            time_msc=time_msc,
            bid=100.0,
            ask=100.2,
            last=100.1,
            volume=1.0,
            flags=0,
            volume_real=1.0,
        )

    def copy_ticks_from(
        self,
        symbol: str,
        date_from: datetime,
        count: int,
        flags: int,
    ) -> list[dict[str, object]]:
        self.calls.append(f"copy_ticks_from:{symbol}")
        time_msc = int((self.now - timedelta(seconds=5)).timestamp() * 1000)
        return [
            {
                "time": int((self.now - timedelta(seconds=5)).timestamp()),
                "time_msc": time_msc,
                "bid": 99.9,
                "ask": 100.1,
                "last": 100.0,
                "volume": 1.0,
                "flags": 0,
                "volume_real": 1.0,
            }
        ]

    def market_book_add(self, symbol: str) -> bool:
        self.calls.append(f"market_book_add:{symbol}")
        return True

    def market_book_get(self, symbol: str) -> list[SimpleNamespace]:
        self.calls.append(f"market_book_get:{symbol}")
        return [
            SimpleNamespace(type=self.BOOK_TYPE_BUY, price=100.0, volume=2.0, volume_dbl=2.0),
            SimpleNamespace(type=self.BOOK_TYPE_SELL, price=100.2, volume=3.0, volume_dbl=3.0),
        ]

    def market_book_release(self, symbol: str) -> bool:
        self.calls.append(f"market_book_release:{symbol}")
        return True

    def order_send(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("collector must never call order_send")


class MarketDataCollectorTests(unittest.TestCase):
    def test_confirmed_symbol_map_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            map_path = Path(tmpdir) / "config" / "symbol_map.json"
            write_symbol_map(map_path, eth_status="ambiguous")

            with self.assertRaises(SymbolMapError):
                load_confirmed_symbol_map(
                    map_path,
                    target_symbols=("BTC/USD", "ETH/USD"),
                )

            with self.assertRaises(SymbolMapError):
                load_confirmed_symbol_map(
                    Path(tmpdir) / "missing.json",
                    target_symbols=("BTC/USD",),
                )

    def test_collect_once_stores_bars_ticks_metadata_and_depth(self) -> None:
        fake_mt5 = FakeMT5MarketData()
        settings = CollectorSettings(
            bar_count=2,
            tick_backfill_minutes=1,
            tick_backfill_count=10,
            include_depth=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trading.db"
            with SQLiteStore(db_path) as store:
                collector = MarketDataCollector(
                    fake_mt5,
                    store,
                    {"BTC/USD": "BTCUSD", "ETH/USD": "ETHUSD"},
                    settings=settings,
                )

                result = collector.collect_once()

                self.assertEqual(result.metadata_written, 2)
                self.assertEqual(result.bars_written, 8)
                self.assertEqual(result.ticks_written, 4)
                self.assertEqual(result.order_book_rows_written, 4)
                self.assertEqual(store.count_rows("symbol_metadata"), 2)
                self.assertEqual(store.count_rows("bars"), 8)
                self.assertEqual(store.count_rows("ticks"), 4)
                self.assertEqual(store.count_rows("order_book_snapshots"), 4)

        self.assertNotIn("order_send", fake_mt5.calls)
        self.assertIn("market_book_release:BTCUSD", fake_mt5.calls)

    def test_run_bounded_collection_initializes_and_shuts_down(self) -> None:
        fake_mt5 = FakeMT5MarketData()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            terminal = root / "terminal64.exe"
            terminal.write_text("", encoding="utf-8")
            map_path = root / "config" / "symbol_map.json"
            db_path = root / "trading.db"
            write_symbol_map(map_path)

            results = run_bounded_collection(
                make_config(terminal, str(db_path)),
                symbol_map_path=map_path,
                settings=CollectorSettings(bar_count=1),
                once=True,
                mt5_module=fake_mt5,
            )

            with SQLiteStore(db_path) as store:
                bar_count = store.count_rows("bars")
                tick_count = store.count_rows("ticks")

        self.assertTrue(fake_mt5.shutdown_called)
        self.assertEqual(len(results), 1)
        self.assertGreater(bar_count, 0)
        self.assertGreater(tick_count, 0)
        self.assertNotIn("order_send", fake_mt5.calls)


if __name__ == "__main__":
    unittest.main()
