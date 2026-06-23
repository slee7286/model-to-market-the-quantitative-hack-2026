from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.analytics import (
    AnalyticsConfig,
    bucket_signal_score,
    compute_performance_metrics,
    generate_analytics_report_from_store,
    write_analytics_reports,
)
from mt5_crypto_bot.schemas import (
    AccountSnapshot,
    Direction,
    Fill,
    FillSide,
    OrderIntent,
    OrderSide,
    RiskCheck,
    Signal,
    SignalDecision,
)
from mt5_crypto_bot.storage import SQLiteStore


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


class AnalyticsTests(unittest.TestCase):
    def test_metric_calculations_use_return_drawdown_and_15m_sharpe(self) -> None:
        equity_curve = pd.DataFrame(
            {
                "time_utc": [
                    NOW,
                    NOW + timedelta(minutes=15),
                    NOW + timedelta(minutes=30),
                    NOW + timedelta(minutes=45),
                ],
                "equity": [1_000_000.0, 1_010_000.0, 990_000.0, 1_020_000.0],
            }
        )

        metrics = compute_performance_metrics(
            equity_curve,
            pd.DataFrame(),
            pd.DataFrame(),
            config=AnalyticsConfig(),
        )

        self.assertAlmostEqual(metrics["return"], 0.02)
        self.assertAlmostEqual(metrics["max_drawdown"], (1_010_000.0 - 990_000.0) / 1_010_000.0)
        self.assertEqual(metrics["sharpe_15m_observations"], 3)
        self.assertFalse(math.isnan(metrics["sharpe_15m"]))

    def test_report_covers_dry_run_real_fills_and_inactive_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trading.db"
            output_dir = Path(tmpdir) / "reports"
            with SQLiteStore(db_path) as store:
                self._seed_analytics_store(store)

            report = generate_analytics_report_from_store(
                db_path,
                target_symbols=("BTC/USD", "ETH/USD"),
                include_shadow_evaluation=False,
                now_utc=NOW + timedelta(hours=1),
            )
            paths = write_analytics_reports(report, output_dir, run_id="unit")

            self.assertEqual(report.metrics["real_fill_count"], 1)
            self.assertEqual(report.metrics["dry_run_order_count"], 1)
            self.assertGreater(len(report.parameter_proposals), 0)
            self.assertTrue(paths["markdown"].exists())
            markdown = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("Manual Approval Workflow", markdown)
            self.assertIn("Proposed parameters are stored as inactive", markdown)

            bucket = report.signal_bucket_performance.set_index("score_bucket")
            self.assertEqual(int(bucket.loc["strong_long", "fill_count"]), 1)
            self.assertEqual(float(bucket.loc["strong_long", "pnl_usd"]), 1_000.0)
            reasons = set(report.reject_block_reasons["source"])
            self.assertIn("signal_block", reasons)
            self.assertIn("risk_reject", reasons)

            with SQLiteStore(db_path) as store:
                rows = store.fetch_all(
                    """
                    SELECT strategy_version, active, approved_by, approved_at_utc, params_json
                    FROM strategy_versions
                    WHERE strategy_version LIKE 'proposal_%'
                    """
                )

            self.assertEqual(len(rows), len(report.parameter_proposals))
            for row in rows:
                self.assertEqual(int(row["active"]), 0)
                self.assertIsNone(row["approved_by"])
                self.assertIsNone(row["approved_at_utc"])
                params = json.loads(row["params_json"])
                self.assertTrue(params["requires_manual_approval"])
                self.assertLessEqual(params["max_gross_leverage"], 27.0)
                self.assertLessEqual(params["max_symbol_leverage"], 27.0)
                self.assertLessEqual(params["max_margin_usage"], 0.90)

    def test_signal_score_buckets_are_stable(self) -> None:
        self.assertEqual(bucket_signal_score(-2.0), "strong_short")
        self.assertEqual(bucket_signal_score(-0.5), "weak_short")
        self.assertEqual(bucket_signal_score(0.0), "neutral")
        self.assertEqual(bucket_signal_score(0.8), "weak_long")
        self.assertEqual(bucket_signal_score(2.0), "strong_long")

    def _seed_analytics_store(self, store: SQLiteStore) -> None:
        for index, equity in enumerate((1_000_000.0, 1_006_000.0, 1_002_000.0, 1_012_000.0)):
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

        strong_signal = Signal(
            signal_id="sig-btc-strong",
            created_at_utc=NOW + timedelta(minutes=1),
            strategy_version="momo_v1",
            symbol="BTC/USD",
            timeframe="M5",
            direction=Direction.LONG,
            score=1.75,
            target_leverage=1.0,
            target_volume=0.1,
            target_price=70_000.0,
            features={"spread_bps": 4.0, "feature_time_utc": NOW.isoformat()},
            decision=SignalDecision.ENTER,
            reason="long entry/resize",
        )
        blocked_signal = Signal(
            signal_id="sig-eth-block",
            created_at_utc=NOW + timedelta(minutes=2),
            strategy_version="momo_v1",
            symbol="ETH/USD",
            timeframe="M5",
            direction=Direction.FLAT,
            score=-1.8,
            target_leverage=0.0,
            features={"spread_bps": 20.0, "feature_time_utc": NOW.isoformat()},
            decision=SignalDecision.BLOCK,
            reason="block: spread cap",
        )
        store.upsert_signal(strong_signal)
        store.upsert_signal(blocked_signal)

        dry_order = OrderIntent(
            client_order_id="dry-order",
            signal_id="sig-eth-block",
            created_at_utc=NOW + timedelta(minutes=3),
            symbol="ETH/USD",
            side=OrderSide.SELL,
            requested_volume=0.2,
            requested_price=3_500.0,
            strategy_version="momo_v1",
        )
        filled_order = OrderIntent(
            client_order_id="filled-order",
            signal_id="sig-btc-strong",
            created_at_utc=NOW + timedelta(minutes=4),
            symbol="BTC/USD",
            side=OrderSide.BUY,
            requested_volume=0.1,
            requested_price=70_000.0,
            strategy_version="momo_v1",
        )
        store.upsert_order_intent(dry_order, status="dry_run")
        store.upsert_order_intent(filled_order, status="filled")
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
                filled_at_utc=NOW + timedelta(minutes=5),
                side=FillSide.BUY,
                volume=0.1,
                price=70_010.0,
                profit=1_000.0,
                slippage_bps=1.4,
            )
        )
        store.insert_risk_check(
            RiskCheck(
                check_id="risk-eth-block",
                signal_id="sig-eth-block",
                checked_at_utc=NOW + timedelta(minutes=3),
                passed=False,
                symbol="ETH/USD",
                equity=1_006_000.0,
                margin_usage=0.1,
                gross_leverage=1.0,
                reason="block: spread cap",
            )
        )


if __name__ == "__main__":
    unittest.main()
