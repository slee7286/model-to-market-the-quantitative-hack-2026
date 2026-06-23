from __future__ import annotations

import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.features import (
    compute_and_export_feature_snapshots,
    compute_feature_snapshots,
    compute_feature_snapshots_from_store,
    latest_feature_snapshots,
)
from mt5_crypto_bot.storage import SQLiteStore


START = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)


def make_m5_bars(symbol: str, *, count: int = 140, base: float = 100.0) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    previous_close = base
    for index in range(count):
        drift = index * base * 0.0008
        wave = base * 0.01 * math.sin(index / 7)
        close = base + drift + wave
        open_price = previous_close
        high = max(open_price, close) + base * 0.002
        low = min(open_price, close) - base * 0.002
        rows.append(
            {
                "symbol": symbol,
                "timeframe": "M5",
                "time_utc": START + timedelta(minutes=5 * index),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "tick_volume": 100 + (index % 17),
                "spread": 2,
                "real_volume": 1,
            }
        )
        previous_close = close
    return rows


def make_ticks(
    symbol: str,
    bars: list[dict[str, object]],
    *,
    spread: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in bars:
        close = float(row["close"])
        feature_time = row["time_utc"] + timedelta(minutes=5)
        rows.append(
            {
                "symbol": symbol,
                "time_utc": feature_time,
                "bid": close - spread / 2,
                "ask": close + spread / 2,
                "last": close,
                "volume": 1.0,
                "volume_real": 1.0,
            }
        )
    return rows


class FeatureEngineeringTests(unittest.TestCase):
    def test_feature_generation_includes_strategy_freeze_columns(self) -> None:
        btc_bars = make_m5_bars("BTC/USD", base=10_000)
        eth_bars = make_m5_bars("ETH/USD", base=1_000)
        ticks = make_ticks("BTC/USD", btc_bars, spread=5.0) + make_ticks(
            "ETH/USD",
            eth_bars,
            spread=1.0,
        )
        order_book = [
            {
                "symbol": "BTC/USD",
                "observed_at_utc": START + timedelta(minutes=500),
                "side": "bid",
                "level": 1,
                "price": 10_100,
                "volume": 3.0,
            },
            {
                "symbol": "BTC/USD",
                "observed_at_utc": START + timedelta(minutes=500),
                "side": "ask",
                "level": 1,
                "price": 10_101,
                "volume": 1.0,
            },
        ]

        features = compute_feature_snapshots(
            btc_bars + eth_bars,
            ticks=ticks,
            order_book_snapshots=order_book,
            target_symbols=("BTC/USD", "ETH/USD"),
        )
        latest = latest_feature_snapshots(features)

        self.assertEqual(set(latest["symbol"]), {"BTC/USD", "ETH/USD"})
        for column in (
            "ret_3_m5",
            "ema20_minus_ema80_over_atr",
            "donchian_ensemble",
            "atr_14",
            "rv_48_m5",
            "spread_bps",
            "volume_zscore",
            "btc_regime",
            "beta_to_btc",
            "relative_score",
            "final_score_raw",
            "shadow_final_score",
        ):
            self.assertIn(column, features.columns)
        self.assertTrue(bool(latest["feature_warmup_complete"].all()))
        self.assertTrue(math.isfinite(float(latest["spread_bps"].dropna().iloc[0])))

    def test_future_bar_changes_do_not_change_prior_features(self) -> None:
        bars = make_m5_bars("BTC/USD", base=10_000)
        baseline = compute_feature_snapshots(bars, target_symbols=("BTC/USD",))

        mutated = [dict(row) for row in bars]
        mutated[125]["close"] = 50_000.0
        mutated[125]["high"] = 55_000.0
        mutated[125]["low"] = 9_000.0
        mutated[125]["tick_volume"] = 10_000
        changed = compute_feature_snapshots(mutated, target_symbols=("BTC/USD",))

        baseline_row = baseline[
            (baseline["symbol"] == "BTC/USD") & (baseline["bar_index"] == 100)
        ].iloc[0]
        changed_row = changed[
            (changed["symbol"] == "BTC/USD") & (changed["bar_index"] == 100)
        ].iloc[0]
        for column in (
            "ret_1_m5",
            "ret_3_m5",
            "ret_12_m5",
            "z_ret_12_m5",
            "ema_20",
            "ema_80",
            "donchian_48",
            "atr_14",
            "rv_48_m5",
            "trend_score",
            "final_score_raw",
        ):
            left = baseline_row[column]
            right = changed_row[column]
            if math.isnan(float(left)) and math.isnan(float(right)):
                continue
            self.assertAlmostEqual(float(left), float(right), places=12, msg=column)

    def test_donchian_breakout_uses_prior_channel_not_current_high(self) -> None:
        bars: list[dict[str, object]] = []
        for index in range(60):
            close = 95.0
            high = 100.0
            low = 90.0
            if index == 50:
                close = 101.0
                high = 101.0
                low = 94.0
            bars.append(
                {
                    "symbol": "BTC/USD",
                    "timeframe": "M5",
                    "time_utc": START + timedelta(minutes=5 * index),
                    "open": 95.0,
                    "high": high,
                    "low": low,
                    "close": close,
                    "tick_volume": 100,
                }
            )

        features = compute_feature_snapshots(bars, target_symbols=("BTC/USD",))
        breakout = features[features["bar_index"] == 50].iloc[0]

        self.assertEqual(float(breakout["rolling_high_12"]), 100.0)
        self.assertEqual(float(breakout["donchian_12"]), 1.0)

    def test_current_tick_after_latest_completed_bar_is_attached_for_live_context(self) -> None:
        bars = make_m5_bars("EUR/USD", base=1.08)
        now = START + timedelta(minutes=5 * len(bars), seconds=30)
        latest_close = float(bars[-1]["close"])
        ticks = [
            {
                "symbol": "EUR/USD",
                "time_utc": now - timedelta(seconds=10),
                "bid": latest_close - 0.00005,
                "ask": latest_close + 0.00005,
                "last": latest_close,
            }
        ]

        features = compute_feature_snapshots(
            bars,
            ticks=ticks,
            target_symbols=("EUR/USD",),
            now_utc=now,
        )
        latest = latest_feature_snapshots(features).iloc[0]

        self.assertTrue(math.isfinite(float(latest["spread_bps"])))
        self.assertAlmostEqual(float(latest["tick_age_seconds"]), 10.0, delta=1.0)

    def test_store_load_and_csv_export(self) -> None:
        btc_bars = make_m5_bars("BTC/USD", base=10_000)
        eth_bars = make_m5_bars("ETH/USD", base=1_000)
        ticks = make_ticks("BTC/USD", btc_bars, spread=5.0) + make_ticks(
            "ETH/USD",
            eth_bars,
            spread=1.0,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "trading.db"
            output_path = root / "features.csv"
            with SQLiteStore(db_path) as store:
                store.upsert_bars(btc_bars + eth_bars)
                store.upsert_ticks(ticks)

            features = compute_feature_snapshots_from_store(
                db_path,
                target_symbols=("BTC/USD", "ETH/USD"),
            )
            _, exported = compute_and_export_feature_snapshots(
                db_path,
                output_path,
                target_symbols=("BTC/USD", "ETH/USD"),
                latest_only=True,
            )

            self.assertFalse(features.empty)
            self.assertTrue(exported.exists())
            self.assertIn("final_score_raw", exported.read_text(encoding="utf-8"))

    def test_store_load_accepts_mixed_iso_timestamp_precision(self) -> None:
        btc_bars = make_m5_bars("BTC/USD", base=10_000)
        for index, row in enumerate(btc_bars):
            timestamp = row["time_utc"]
            assert isinstance(timestamp, datetime)
            row["time_utc"] = (
                timestamp.isoformat()
                if index % 2
                else timestamp.replace(microsecond=123456).isoformat()
            )
        ticks = make_ticks("BTC/USD", make_m5_bars("BTC/USD", base=10_000), spread=5.0)
        for index, row in enumerate(ticks):
            timestamp = row["time_utc"]
            assert isinstance(timestamp, datetime)
            row["time_utc"] = (
                timestamp.isoformat()
                if index % 2
                else timestamp.replace(microsecond=654321).isoformat()
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trading.db"
            with SQLiteStore(db_path) as store:
                store.upsert_bars(btc_bars)
                store.upsert_ticks(ticks)

            features = compute_feature_snapshots_from_store(
                db_path,
                target_symbols=("BTC/USD",),
            )

        self.assertFalse(features.empty)
        self.assertIn("feature_time_utc", features.columns)


if __name__ == "__main__":
    unittest.main()
