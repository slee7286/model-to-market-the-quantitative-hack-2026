from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.constants import ALLOWED_SYMBOLS
from mt5_crypto_bot.risk import (
    AccountRiskState,
    MarketRiskState,
    PositionRiskState,
    RiskContext,
    RiskEngine,
    RiskLimits,
)
from mt5_crypto_bot.schemas import OrderIntent, OrderSide, PositionSide, SymbolConfig
from mt5_crypto_bot.storage import SQLiteStore


NOW = datetime(2026, 6, 22, 12, 5, tzinfo=timezone.utc)
FEATURE_TIME = NOW - timedelta(seconds=30)
EQUITY = 1_000_000.0


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


def account(
    *,
    margin: float = 0.0,
    max_drawdown: float = 0.0,
    gross_leverage: float = 0.0,
) -> AccountRiskState:
    return AccountRiskState(
        observed_at_utc=NOW,
        balance=EQUITY,
        equity=EQUITY,
        margin=margin,
        margin_free=EQUITY - margin,
        margin_level=None,
        gross_leverage=gross_leverage,
        max_drawdown=max_drawdown,
        leverage=30.0,
    )


def market(
    symbol: str = "BTC/USD",
    *,
    bid: float = 100.00,
    ask: float = 100.04,
    age_seconds: float = 30.0,
) -> MarketRiskState:
    return MarketRiskState(
        symbol=symbol,
        observed_at_utc=NOW - timedelta(seconds=age_seconds),
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2.0,
    )


def all_metadata() -> dict[str, SymbolConfig]:
    return {symbol: metadata(symbol) for symbol in ALLOWED_SYMBOLS}


def high_capacity_metadata(symbol: str = "BTC/USD") -> SymbolConfig:
    config = metadata(symbol)
    return config.model_copy(update={"volume_max": 1_000_000.0})


def context(
    *,
    acct: AccountRiskState | None = None,
    positions: tuple[PositionRiskState, ...] = tuple(),
    markets: dict[str, MarketRiskState] | None = None,
    kill_switch: bool = False,
    single_breach_seconds: float = 0.0,
    net_breach_seconds: float = 0.0,
) -> RiskContext:
    return RiskContext(
        account=acct or account(),
        symbol_metadata=all_metadata(),
        positions=positions,
        market=markets or {"BTC/USD": market("BTC/USD")},
        now_utc=NOW,
        kill_switch_active=kill_switch,
        single_instrument_breach_seconds=single_breach_seconds,
        net_directional_breach_seconds=net_breach_seconds,
    )


def intent(
    *,
    symbol: str = "BTC/USD",
    side: OrderSide = OrderSide.BUY,
    volume: float = 10.0,
    price: float = 100.04,
    stop_loss: float | None = 98.0,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=f"order-{symbol.replace('/', '')}-{side.value}",
        signal_id="sig-1",
        created_at_utc=NOW,
        symbol=symbol,
        side=side,
        requested_volume=volume,
        requested_price=price,
        stop_loss=stop_loss,
        take_profit=104.0 if side == OrderSide.BUY else 96.0,
        metadata={"feature_time_utc": FEATURE_TIME.isoformat()},
    )


def position(symbol: str, side: PositionSide, leverage: float) -> PositionRiskState:
    return PositionRiskState(
        symbol=symbol,
        side=side,
        volume=leverage * EQUITY / 100.0,
        price_open=100.0,
        price_current=100.0,
        observed_at_utc=NOW,
    )


class RiskEngineTests(unittest.TestCase):
    def test_safe_order_passes_and_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                decision = RiskEngine().check_order_intent(
                    intent(),
                    context(),
                    store=store,
                )
                row = store.fetch_one("SELECT passed, reason FROM risk_checks")

        self.assertTrue(decision.passed)
        self.assertIsNotNone(decision.approved_order)
        self.assertIsNotNone(row)
        self.assertEqual(row["passed"], 1)
        self.assertIn("approved", row["reason"])

    def test_invalid_symbol_mapping_is_blocked_and_stored(self) -> None:
        raw_intent = {
            "client_order_id": "bad-symbol",
            "signal_id": "sig-bad",
            "created_at_utc": NOW,
            "symbol": "DOGE/USD",
            "side": "buy",
            "requested_volume": 1.0,
            "metadata": {"feature_time_utc": FEATURE_TIME.isoformat()},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                decision = RiskEngine().check_order_intent(raw_intent, context(), store=store)

                self.assertEqual(store.count_rows("risk_checks"), 1)

        self.assertFalse(decision.passed)
        self.assertIn("invalid order intent", decision.risk_check.reason or "")

    def test_stale_tick_blocks_trade(self) -> None:
        decision = RiskEngine().check_order_intent(
            intent(),
            context(markets={"BTC/USD": market("BTC/USD", age_seconds=180)}),
        )

        self.assertFalse(decision.passed)
        self.assertIn("latest tick is stale", decision.risk_check.reason or "")

    def test_wide_spread_blocks_trade(self) -> None:
        decision = RiskEngine().check_order_intent(
            intent(price=101.0),
            context(markets={"BTC/USD": market("BTC/USD", bid=100.0, ask=101.0)}),
        )

        self.assertFalse(decision.passed)
        self.assertIn("spread", decision.risk_check.reason or "")

    def test_gross_leverage_cap_blocks_trade(self) -> None:
        decision = RiskEngine(RiskLimits(max_gross_leverage=0.0005)).check_order_intent(
            intent(),
            context(),
        )

        self.assertFalse(decision.passed)
        self.assertIn("gross leverage", decision.risk_check.reason or "")

    def test_margin_usage_cap_blocks_trade(self) -> None:
        decision = RiskEngine().check_order_intent(
            intent(),
            context(acct=account(margin=910_000.0)),
        )

        self.assertFalse(decision.passed)
        self.assertIn("margin usage", decision.risk_check.reason or "")

    def test_aggressive_profile_allows_just_under_28x_but_blocks_above_28x_gross(self) -> None:
        base_context = context()
        live_metadata = dict(base_context.symbol_metadata)
        live_metadata["EUR/USD"] = high_capacity_metadata("EUR/USD")
        aggressive_context = RiskContext(
            account=base_context.account,
            symbol_metadata=live_metadata,
            positions=base_context.positions,
            market={"EUR/USD": market("EUR/USD", bid=100.0, ask=100.01)},
            now_utc=base_context.now_utc,
        )
        gross_cap_engine = RiskEngine(RiskLimits(max_margin_usage=1.0))

        allowed = gross_cap_engine.check_order_intent(
            intent(symbol="EUR/USD", volume=278_000.0, price=100.0),
            aggressive_context,
        )
        blocked = gross_cap_engine.check_order_intent(
            intent(symbol="EUR/USD", volume=282_000.0, price=100.0),
            aggressive_context,
        )

        self.assertTrue(allowed.passed, allowed.risk_check.reason)
        self.assertFalse(blocked.passed)
        self.assertIn("gross leverage", blocked.risk_check.reason or "")

    def test_single_instrument_concentration_blocks_after_soft_limit(self) -> None:
        # Lone dominant BTC position keeps single-instrument exposure > 90%; once
        # the breach is older than the soft limit, new exposure is blocked.
        btc_volume = 1.00 * EQUITY / 100.04
        decision = RiskEngine().check_order_intent(
            intent(volume=round(btc_volume, 2)),
            context(
                positions=(position("ETH/USD", PositionSide.SHORT, 0.05),),
                single_breach_seconds=16 * 60,
            ),
        )

        self.assertFalse(decision.passed)
        self.assertIn("volume above broker maximum", decision.risk_check.reason or "")

    def test_net_directional_exposure_blocks_after_soft_limit(self) -> None:
        btc_volume = 0.70 * EQUITY / 100.04
        decision = RiskEngine().check_order_intent(
            intent(volume=round(btc_volume, 2)),
            context(
                positions=(position("ETH/USD", PositionSide.LONG, 0.40),),
                net_breach_seconds=16 * 60,
            ),
        )

        self.assertFalse(decision.passed)
        self.assertIn("volume above broker maximum", decision.risk_check.reason or "")

    def test_volume_cap_blocks_before_soft_concentration_window(self) -> None:
        # With broker max volume capped at 100, this formerly allowed
        # over-concentrated order is now rejected before soft-window logic.
        btc_volume = 1.00 * EQUITY / 100.04
        decision = RiskEngine().check_order_intent(
            intent(volume=round(btc_volume, 2)),
            context(
                positions=(position("ETH/USD", PositionSide.SHORT, 0.05),),
                single_breach_seconds=60.0,
            ),
        )

        self.assertFalse(decision.passed)
        self.assertIn("volume above broker maximum", decision.risk_check.reason or "")

    def test_ballast_batch_allows_concentrated_main_after_soft_limit(self) -> None:
        metadata_map = all_metadata()
        metadata_map["BTC/USD"] = high_capacity_metadata("BTC/USD")
        metadata_map["ETH/USD"] = high_capacity_metadata("ETH/USD")
        risk_context = RiskContext(
            account=account(),
            symbol_metadata=metadata_map,
            positions=tuple(),
            market={
                "BTC/USD": market("BTC/USD"),
                "ETH/USD": market("ETH/USD"),
            },
            now_utc=NOW,
            single_instrument_breach_seconds=16 * 60,
            net_directional_breach_seconds=16 * 60,
        )
        main = intent(symbol="BTC/USD", side=OrderSide.BUY, volume=240_000.0, price=100.0)
        ballast = intent(
            symbol="ETH/USD",
            side=OrderSide.SELL,
            volume=30_000.0,
            price=100.0,
        ).model_copy(
            update={
                "metadata": {
                    "feature_time_utc": FEATURE_TIME.isoformat(),
                    "discipline_ballast": True,
                    "ballast_for_symbol": "BTC/USD",
                    "target_leverage": 3.0,
                }
            }
        )

        main_alone = RiskEngine().check_order_intent(main, risk_context)
        batch = RiskEngine().check_order_intents((ballast, main), risk_context)

        self.assertFalse(main_alone.passed)
        self.assertIn("single-instrument", main_alone.risk_check.reason or "")
        self.assertEqual(batch.summary()["passed"], 2)
        self.assertLessEqual(
            batch.decisions[-1].risk_check.single_instrument_exposure or 1.0,
            0.90,
        )
        self.assertLessEqual(
            batch.decisions[-1].risk_check.net_directional_exposure or 1.0,
            0.95,
        )

    def test_sprint_mode_does_not_block_new_risk_on_drawdown_alone(self) -> None:
        decision = RiskEngine().check_order_intent(
            intent(),
            context(acct=account(max_drawdown=0.081)),
        )

        self.assertTrue(decision.passed, decision.risk_check.reason)

    def test_kill_switch_blocks_new_risk(self) -> None:
        decision = RiskEngine().check_order_intent(
            intent(),
            context(kill_switch=True),
        )

        self.assertFalse(decision.passed)
        self.assertIn("kill switch", decision.risk_check.reason or "")

    def test_kill_switch_allows_risk_reducing_close(self) -> None:
        close_intent = intent(
            side=OrderSide.SELL,
            volume=10.0,
            price=100.0,
            stop_loss=None,
        )
        decision = RiskEngine().check_order_intent(
            close_intent,
            context(positions=(position("BTC/USD", PositionSide.LONG, 0.20),), kill_switch=True),
        )

        self.assertTrue(decision.passed)
        self.assertIsNotNone(decision.approved_order)

    def test_stopless_exact_position_close_with_price_drift_is_risk_reducing(self) -> None:
        close_intent = intent(
            side=OrderSide.SELL,
            volume=100.0,
            price=102.0,
            stop_loss=None,
        )
        close_context = RiskContext(
            account=AccountRiskState(
                observed_at_utc=NOW,
                balance=100_000.0,
                equity=100_000.0,
                margin=0.0,
                margin_free=100_000.0,
                gross_leverage=0.0,
                max_drawdown=0.0,
                leverage=30.0,
            ),
            symbol_metadata=all_metadata(),
            positions=(
                PositionRiskState(
                    symbol="BTC/USD",
                    side=PositionSide.LONG,
                    volume=100.0,
                    price_open=100.0,
                    price_current=100.0,
                    observed_at_utc=NOW,
                ),
            ),
            market={"BTC/USD": market("BTC/USD", bid=102.0, ask=102.04)},
            now_utc=NOW,
        )

        decision = RiskEngine().check_order_intent(close_intent, close_context)

        self.assertTrue(decision.passed, decision.risk_check.reason)
        self.assertTrue(decision.risk_check.details.get("position_close_reducing"))
        self.assertNotIn("stop loss", decision.risk_check.reason or "")

    def test_tiny_broker_step_reversal_counts_as_risk_reducing_close(self) -> None:
        close_intent = intent(
            symbol="XRP/USD",
            side=OrderSide.BUY,
            volume=100.0,
            price=100.04,
            stop_loss=None,
        )
        decision = RiskEngine().check_order_intent(
            close_intent,
            context(
                positions=(
                    PositionRiskState(
                        symbol="XRP/USD",
                        side=PositionSide.SHORT,
                        volume=99.99,
                        price_open=100.0,
                        price_current=100.0,
                        observed_at_utc=NOW,
                    ),
                ),
                markets={"XRP/USD": market("XRP/USD")},
            ),
        )

        self.assertTrue(decision.passed, decision.risk_check.reason)
        self.assertIsNotNone(decision.approved_order)

    def test_minimum_stop_distance_blocks_trade(self) -> None:
        decision = RiskEngine().check_order_intent(
            intent(stop_loss=100.03),
            context(),
        )

        self.assertFalse(decision.passed)
        self.assertIn("stop distance", decision.risk_check.reason or "")

    def test_volume_step_blocks_trade(self) -> None:
        decision = RiskEngine().check_order_intent(
            intent(volume=0.015),
            context(),
        )

        self.assertFalse(decision.passed)
        self.assertIn("volume step", decision.risk_check.reason or "")


if __name__ == "__main__":
    unittest.main()
