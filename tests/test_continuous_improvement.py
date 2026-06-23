from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.continuous_improvement import (
    ContinuousImprovementConfig,
    run_continuous_improvement_from_store,
)
from mt5_crypto_bot.schemas import (
    AccountSnapshot,
    Direction,
    Fill,
    FillSide,
    OrderIntent,
    OrderSide,
    Signal,
    SignalDecision,
    StrategyParams,
)
from mt5_crypto_bot.storage import SQLiteStore


NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


class ContinuousImprovementTests(unittest.TestCase):
    def test_loop_writes_reports_and_inactive_candidates_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Path(tmpdir) / "trading.db"
            output_dir = Path(tmpdir) / "reports"
            with SQLiteStore(database) as store:
                self._seed_store(store)

            report = run_continuous_improvement_from_store(
                database,
                target_symbols=("BTC/USD",),
                base_params=StrategyParams(
                    entry_threshold=1.5,
                    exit_threshold=0.6,
                ),
                config=ContinuousImprovementConfig(
                    output_dir=output_dir,
                    run_id="unit",
                    store_analytics_proposals=True,
                    store_threshold_candidate=True,
                    include_shadow_backtest=False,
                    write_backtest_artifacts=False,
                ),
                now_utc=NOW + timedelta(hours=1),
            )

            self.assertTrue(report.paths["markdown"].exists())
            self.assertTrue(report.paths["summary_json"].exists())
            self.assertTrue(report.paths["candidate_env"].exists())
            self.assertTrue(report.safety["offline_only"])
            self.assertFalse(report.safety["mt5_connection"])
            self.assertFalse(report.safety["order_send_called"])
            self.assertFalse(report.safety["live_config_modified"])
            self.assertFalse(report.safety["strategy_auto_promoted"])
            markdown = report.paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("Manual Promotion Checklist", markdown)
            self.assertIn("No `.env`, approval file, or live runner setting was modified", markdown)

            with SQLiteStore(database) as store:
                rows = store.fetch_all(
                    """
                    SELECT strategy_version, active, approved_by, approved_at_utc, params_json
                    FROM strategy_versions
                    """
                )

            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertEqual(int(row["active"]), 0)
                self.assertIsNone(row["approved_by"])
                self.assertIsNone(row["approved_at_utc"])
                params = json.loads(row["params_json"])
                self.assertNotEqual(params.get("proposal_status"), "active")

    def _seed_store(self, store: SQLiteStore) -> None:
        for index, equity in enumerate((1_000_000.0, 1_004_000.0, 1_001_000.0, 1_008_000.0)):
            store.upsert_account_snapshot(
                AccountSnapshot(
                    observed_at_utc=NOW + timedelta(minutes=15 * index),
                    balance=1_000_000.0,
                    equity=equity,
                    profit=equity - 1_000_000.0,
                    gross_leverage=1.0,
                    max_drawdown=0.01,
                )
            )
        bars = []
        for index in range(40):
            now = NOW + timedelta(minutes=5 * index)
            close = 100.0 + index * 0.2
            bars.append(
                {
                    "symbol": "BTC/USD",
                    "timeframe": "M5",
                    "time_utc": now,
                    "open": close - 0.1,
                    "high": close + 0.3,
                    "low": close - 0.3,
                    "close": close,
                    "spread": 1,
                    "tick_volume": 100 + index,
                }
            )
        store.upsert_bars(bars)
        for index in range(30):
            now = NOW + timedelta(minutes=5 * index)
            signal = Signal(
                signal_id=f"sig-btc-{index}",
                created_at_utc=now,
                strategy_version="momo_v1",
                symbol="BTC/USD",
                timeframe="M5",
                direction=Direction.LONG,
                score=1.0 if index % 2 == 0 else -0.4,
                target_leverage=1.0,
                target_volume=0.1,
                target_price=100.0,
                features={"spread_bps": 1.0, "feature_time_utc": now.isoformat()},
                decision=SignalDecision.ENTER if index % 2 == 0 else SignalDecision.HOLD,
                reason="unit",
            )
            store.upsert_signal(signal)
        order = OrderIntent(
            client_order_id="filled-order",
            signal_id="sig-btc-0",
            created_at_utc=NOW,
            symbol="BTC/USD",
            side=OrderSide.BUY,
            requested_volume=0.1,
            requested_price=100.0,
            strategy_version="momo_v1",
        )
        store.upsert_order_intent(order, status="filled")
        store.connection.execute(
            "UPDATE orders SET mt5_order_ticket = ?, mt5_deal_ticket = ? WHERE client_order_id = ?",
            (1001, 2001, "filled-order"),
        )
        store.connection.commit()
        store.insert_fill(
            Fill(
                deal_ticket=2001,
                order_ticket=1001,
                symbol="BTC/USD",
                filled_at_utc=NOW + timedelta(minutes=1),
                side=FillSide.BUY,
                volume=0.1,
                price=100.2,
                profit=500.0,
            )
        )


if __name__ == "__main__":
    unittest.main()
