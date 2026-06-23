from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.backtest import make_synthetic_fixture_market_data
from mt5_crypto_bot.constants import DISCIPLINE_BALLAST_MAIN_SHARE
from mt5_crypto_bot.schemas import OrderSide, SignalDecision, StrategyParams, SymbolConfig
from mt5_crypto_bot.storage import SQLiteStore
from mt5_crypto_bot.strategy import (
    DryRunStrategyEngine,
    StrategyContext,
    run_strategy_once_from_store,
)


FEATURE_TIME = datetime(2026, 6, 22, 12, 5, tzinfo=timezone.utc)
NOW = FEATURE_TIME + timedelta(seconds=30)


def metadata(symbol: str = "BTC/USD") -> SymbolConfig:
    return SymbolConfig(
        symbol=symbol,
        broker_symbol=symbol.replace("/", ""),
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        trade_contract_size=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        spread=4.0,
        filling_mode=1,
        trade_mode=4,
        raw={"name": symbol.replace("/", "")},
    )


def high_capacity_metadata(symbol: str = "BTC/USD") -> SymbolConfig:
    return metadata(symbol).model_copy(update={"volume_max": 1_000_000.0})


def feature_row(
    *,
    symbol: str = "BTC/USD",
    score_side: str = "flat",
    spread_bps: float = 4.0,
    shock: bool = False,
) -> dict[str, object]:
    if score_side == "long":
        z_value = 2.0
        donchian = 1.0
        close = 101.0
        ema80 = 100.0
        slope = 1.0
    elif score_side == "short":
        z_value = -2.0
        donchian = -1.0
        close = 99.0
        ema80 = 100.0
        slope = -1.0
    else:
        z_value = 0.0
        donchian = 0.0
        close = 100.5
        ema80 = 100.0
        slope = 0.2

    spread = 0.04
    return {
        "symbol": symbol,
        "timeframe": "M5",
        "feature_time_utc": FEATURE_TIME,
        "bar_time_utc": FEATURE_TIME - timedelta(minutes=5),
        "open": 100.0,
        "high": 102.0,
        "low": 98.0,
        "close": close,
        "ema_20": 100.2,
        "ema_80": ema80,
        "ema20_slope_6_over_atr": slope,
        "z_ret_3_m5": z_value,
        "z_ret_12_m5": z_value,
        "z_ema20_minus_ema80_over_atr": z_value,
        "donchian_ensemble": donchian,
        "z_ema20_slope_6_over_atr": z_value,
        "volume_zscore": 1.0,
        "relative_score": 0.0,
        "final_score_raw": z_value,
        "rv_1h_equiv": 0.006,
        "atr_14": 2.0,
        "feature_ready": True,
        "shock_flag": shock,
        "bid": close - spread / 2,
        "ask": close + spread / 2,
        "last": close,
        "spread": spread,
        "spread_bps": spread_bps,
        "tick_time_utc": FEATURE_TIME,
        "tick_age_seconds": 0.0,
        "btc_regime": "neutral",
        "btc_trend_score": 0.0,
    }


class StrategyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = DryRunStrategyEngine()

    def context(self) -> StrategyContext:
        return StrategyContext(
            symbol_metadata={"BTC/USD": high_capacity_metadata()},
            now_utc=NOW,
        )

    def test_long_signal_generates_buy_order_intent(self) -> None:
        result = self.engine.generate_signals(
            [feature_row(score_side="long")],
            context=self.context(),
        )

        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].decision, SignalDecision.ENTER)
        self.assertEqual(result.signals[0].direction, "long")
        self.assertGreater(result.signals[0].target_leverage, 0.0)
        self.assertEqual(len(result.order_intents), 1)
        self.assertEqual(result.order_intents[0].side, OrderSide.BUY)
        self.assertGreater(result.order_intents[0].requested_volume, 0.0)
        self.assertIsNotNone(result.order_intents[0].stop_loss)
        self.assertIsNotNone(result.order_intents[0].take_profit)

    def test_short_signal_generates_sell_order_intent(self) -> None:
        result = self.engine.generate_signals(
            [feature_row(score_side="short")],
            context=self.context(),
        )

        self.assertEqual(result.signals[0].decision, SignalDecision.ENTER)
        self.assertEqual(result.signals[0].direction, "short")
        self.assertEqual(len(result.order_intents), 1)
        self.assertEqual(result.order_intents[0].side, OrderSide.SELL)
        self.assertGreater(result.order_intents[0].requested_volume, 0.0)

    def test_target_position_can_exceed_broker_max_order_volume(self) -> None:
        result = self.engine.generate_signals(
            [feature_row(score_side="long")],
            context=StrategyContext(
                symbol_metadata={"BTC/USD": metadata("BTC/USD")},
                equity=1_000.0,
                now_utc=NOW,
            ),
        )

        self.assertGreater(result.signals[0].target_volume or 0.0, 100.0)
        self.assertGreater(len(result.order_intents), 1)
        self.assertTrue(all(intent.requested_volume <= 100.0 for intent in result.order_intents))
        self.assertTrue(all(intent.metadata.get("chunked_order") for intent in result.order_intents))
        submitted = sum(intent.requested_volume for intent in result.order_intents)
        self.assertLessEqual(submitted, result.signals[0].target_volume or 0.0)

    def test_lone_entry_adds_opposite_ballast_leg(self) -> None:
        result = self.engine.generate_signals(
            [
                feature_row(symbol="BTC/USD", score_side="long"),
                feature_row(symbol="ETH/USD", score_side="flat", spread_bps=3.0),
            ],
            context=StrategyContext(
                symbol_metadata={
                    "BTC/USD": high_capacity_metadata("BTC/USD"),
                    "ETH/USD": high_capacity_metadata("ETH/USD"),
                },
                now_utc=NOW,
            ),
        )

        self.assertEqual(len(result.order_intents), 2)
        self.assertEqual(result.order_intents[0].symbol, "ETH/USD")
        self.assertEqual(result.order_intents[0].side, OrderSide.SELL)
        self.assertTrue(result.order_intents[0].metadata.get("discipline_ballast"))
        self.assertEqual(result.order_intents[1].symbol, "BTC/USD")
        main_signal = next(signal for signal in result.signals if signal.symbol == "BTC/USD")
        ballast_signal = next(
            signal for signal in result.signals if signal.features.get("discipline_ballast")
        )
        gross_target = main_signal.target_leverage + ballast_signal.target_leverage
        self.assertAlmostEqual(
            main_signal.target_leverage / gross_target,
            DISCIPLINE_BALLAST_MAIN_SHARE,
            places=2,
        )

    def test_xrp_entries_are_enabled_in_integrated_sprint(self) -> None:
        result = self.engine.generate_signals(
            [feature_row(symbol="XRP/USD", score_side="short")],
            context=StrategyContext(
                symbol_metadata={"XRP/USD": high_capacity_metadata("XRP/USD")},
                now_utc=NOW,
            ),
        )

        self.assertEqual(result.signals[0].decision, SignalDecision.ENTER)
        self.assertEqual(result.signals[0].direction, "short")
        self.assertEqual(len(result.order_intents), 1)

    def test_forex_entry_uses_own_trend_without_btc_regime(self) -> None:
        row = feature_row(symbol="EUR/USD", score_side="long", spread_bps=1.0)
        row["btc_regime"] = "unknown"
        row["btc_trend_score"] = None
        result = self.engine.generate_signals(
            [row],
            context=StrategyContext(
                symbol_metadata={"EUR/USD": high_capacity_metadata("EUR/USD")},
                now_utc=NOW,
            ),
        )

        self.assertEqual(result.signals[0].decision, SignalDecision.ENTER)
        self.assertEqual(result.signals[0].direction, "long")
        self.assertEqual(result.order_intents[0].symbol, "EUR/USD")

    def test_strategy_params_control_stop_and_take_profit_distance(self) -> None:
        engine = DryRunStrategyEngine(
            StrategyParams(atr_stop_multiple=2.0, take_profit_multiple=3.0)
        )
        result = engine.generate_signals(
            [feature_row(score_side="long")],
            context=self.context(),
        )

        intent = result.order_intents[0]
        self.assertAlmostEqual(intent.stop_loss or 0.0, (intent.requested_price or 0.0) - 4.0)
        self.assertAlmostEqual(intent.take_profit or 0.0, (intent.requested_price or 0.0) + 6.0)

    def test_flat_signal_holds_without_order_intent(self) -> None:
        result = self.engine.generate_signals(
            [feature_row(score_side="flat")],
            context=self.context(),
        )

        self.assertEqual(result.signals[0].decision, SignalDecision.HOLD)
        self.assertEqual(result.signals[0].direction, "flat")
        self.assertEqual(result.signals[0].target_leverage, 0.0)
        self.assertEqual(result.order_intents, ())

    def test_blocked_signal_has_no_order_intent(self) -> None:
        result = self.engine.generate_signals(
            [feature_row(score_side="long", spread_bps=20.0)],
            context=self.context(),
        )

        self.assertEqual(result.signals[0].decision, SignalDecision.BLOCK)
        self.assertIn("spread", result.signals[0].reason or "")
        self.assertEqual(result.order_intents, ())

    def test_every_generated_signal_is_persisted_with_feature_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                result = self.engine.generate_signals(
                    [feature_row(score_side="long")],
                    context=self.context(),
                    store=store,
                )
                row = store.fetch_one(
                    "SELECT features_json, strategy_version FROM signals WHERE signal_id = ?",
                    (result.signals[0].signal_id,),
                )

        self.assertEqual(result.persisted_signals, 1)
        self.assertIsNotNone(row)
        self.assertEqual(row["strategy_version"], "momo_v1")
        features = json.loads(row["features_json"])
        self.assertIn("strategy_score", features)
        self.assertIn("feature_time_utc", features)

    def test_strategy_once_from_store_produces_signals_from_collected_data(self) -> None:
        bars, ticks = make_synthetic_fixture_market_data(symbols=("BTC/USD", "ETH/USD"))
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trading.db"
            with SQLiteStore(db_path) as store:
                store.upsert_symbol_metadata(metadata("BTC/USD"))
                store.upsert_symbol_metadata(metadata("ETH/USD"))
                store.upsert_bars(bars)
                store.upsert_ticks(ticks)

            result = run_strategy_once_from_store(
                db_path,
                target_symbols=("BTC/USD", "ETH/USD"),
                enforce_freshness=False,
            )

            with SQLiteStore(db_path) as store:
                stored_signals = store.count_rows("signals")

        self.assertEqual(len(result.signals), 2)
        self.assertEqual(result.persisted_signals, 2)
        self.assertEqual(stored_signals, 2)


if __name__ == "__main__":
    unittest.main()
