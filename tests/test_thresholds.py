from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.schemas import Direction, Signal, SignalDecision
from mt5_crypto_bot.storage import SQLiteStore
from mt5_crypto_bot.thresholds import recommend_thresholds_from_store


class ThresholdRecommendationTests(unittest.TestCase):
    def test_recommender_reads_signals_and_bars_without_changing_params(self) -> None:
        start = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Path(tmpdir) / "trading.db"
            with SQLiteStore(database) as store:
                bars = []
                for index in range(40):
                    now = start + timedelta(minutes=5 * index)
                    close = 100.0 + index
                    bars.append(
                        {
                            "symbol": "BTC/USD",
                            "timeframe": "M5",
                            "time_utc": now,
                            "open": close - 0.5,
                            "high": close + 0.5,
                            "low": close - 1.0,
                            "close": close,
                            "spread": 1,
                        }
                    )
                store.upsert_bars(bars)
                for index in range(30):
                    now = start + timedelta(minutes=5 * index)
                    store.upsert_signal(
                        Signal(
                            signal_id=f"sig-{index}",
                            created_at_utc=now,
                            strategy_version="momo_v1",
                            symbol="BTC/USD",
                            timeframe="M5",
                            direction=Direction.LONG,
                            score=0.9,
                            target_leverage=1.0,
                            features={
                                "feature_time_utc": now.isoformat(),
                                "spread_bps": 1.0,
                            },
                            decision=SignalDecision.ENTER,
                            reason="test",
                        )
                    )

            recommendation = recommend_thresholds_from_store(
                database,
                target_symbols=("BTC/USD",),
                current_entry_threshold=0.15,
                current_exit_threshold=0.05,
            )

        self.assertTrue(recommendation.available)
        self.assertGreaterEqual(recommendation.evaluated_rows, 20)
        self.assertGreater(recommendation.evaluated_pairs, 0)
        self.assertLess(recommendation.recommended_exit_threshold, recommendation.recommended_entry_threshold)
        self.assertEqual(recommendation.current_entry_threshold, 0.15)
        self.assertEqual(recommendation.current_exit_threshold, 0.05)


if __name__ == "__main__":
    unittest.main()
