from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.backtest import (
    BacktestConfig,
    BacktestDataError,
    run_backtest_comparison,
    run_backtest_from_store,
    run_synthetic_fixture_backtest,
    write_backtest_reports,
)
from mt5_crypto_bot.features import compute_feature_snapshots
from mt5_crypto_bot.storage import SQLiteStore


class BacktestTests(unittest.TestCase):
    def test_synthetic_fixture_compares_mvp_and_challengers(self) -> None:
        comparison = run_synthetic_fixture_backtest(target_symbols=("BTC/USD", "ETH/USD"))

        strategy_names = {result.strategy_name for result in comparison.results}
        self.assertIn("momo_v1", strategy_names)
        self.assertIn("volatility_managed_momentum", strategy_names)
        self.assertIn("donchian_trend_ensemble", strategy_names)
        self.assertIn("intraday_reversal", strategy_names)
        self.assertEqual(comparison.selected_strategy, "momo_v1")
        self.assertTrue(comparison.is_fixture)

        for result in comparison.results:
            for metric in (
                "return",
                "max_drawdown",
                "sharpe_15m",
                "trade_count",
                "average_gross_exposure",
                "turnover",
                "estimated_cost_usd",
                "risk_discipline_estimate",
            ):
                self.assertIn(metric, result.metrics)
            self.assertGreaterEqual(result.metrics["max_drawdown"], 0.0)
            self.assertLessEqual(result.metrics["max_gross_exposure"], 27.0 + 1e-9)

    def test_reports_are_written_with_selection_and_artifacts(self) -> None:
        comparison = run_synthetic_fixture_backtest(target_symbols=("BTC/USD", "ETH/USD"))
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = write_backtest_reports(comparison, tmpdir, run_id="unit")

            markdown = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("Selected live MVP strategy remains `momo_v1`", markdown)
            self.assertIn("Fixture data: `true`", markdown)
            self.assertTrue(paths["summary_csv"].exists())
            self.assertTrue(paths["equity_csv"].exists())
            self.assertTrue(paths["ledger_csv"].exists())
            self.assertTrue(paths["metrics_json"].exists())

    def test_backtest_from_store_uses_collected_bars_and_ticks(self) -> None:
        from mt5_crypto_bot.backtest import make_synthetic_fixture_market_data

        bars, ticks = make_synthetic_fixture_market_data(symbols=("BTC/USD", "ETH/USD"))
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trading.db"
            with SQLiteStore(db_path) as store:
                store.upsert_bars(bars)
                store.upsert_ticks(ticks)

            comparison = run_backtest_from_store(
                db_path,
                target_symbols=("BTC/USD", "ETH/USD"),
            )

        self.assertFalse(comparison.is_fixture)
        self.assertEqual(len(comparison.results), 4)
        self.assertEqual(comparison.selected_strategy, "momo_v1")

    def test_missing_store_data_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(BacktestDataError):
                run_backtest_from_store(
                    Path(tmpdir) / "missing.db",
                    target_symbols=("BTC/USD",),
                )

    def test_backtest_uses_next_open_position_timing(self) -> None:
        from mt5_crypto_bot.backtest import make_synthetic_fixture_market_data

        bars, ticks = make_synthetic_fixture_market_data(symbols=("BTC/USD",), count=180)
        features = compute_feature_snapshots(
            bars,
            ticks=ticks,
            target_symbols=("BTC/USD",),
        )
        comparison = run_backtest_comparison(features, config=BacktestConfig())
        result = next(item for item in comparison.results if item.strategy_name == "momo_v1")
        ledger = result.ledger.sort_values("feature_time_utc").reset_index(drop=True)

        self.assertEqual(float(ledger.loc[0, "position_leverage"]), 0.0)
        self.assertTrue(
            (
                ledger["position_leverage"].iloc[1:].reset_index(drop=True)
                == ledger["target_leverage"].shift(1).iloc[1:].reset_index(drop=True)
            ).all()
        )


if __name__ == "__main__":
    unittest.main()
