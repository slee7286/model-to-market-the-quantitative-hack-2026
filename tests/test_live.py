from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.execution import LiveTradingApprovalError
from mt5_crypto_bot.live import OrderRetryGuard, run_live_cycle
from mt5_crypto_bot.risk import RiskBatchResult, RiskGateDecision
from mt5_crypto_bot.schemas import OrderIntent, OrderSide, RiskCheck


SYMBOLS = ("BAR/USD", "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD")
NOW = datetime(2026, 6, 23, 10, 17, tzinfo=timezone.utc)


class FakeMT5:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def initialize(self, *args: object, **kwargs: object) -> bool:
        self.calls.append("initialize")
        return True

    def order_check(self, request: object) -> object:
        self.calls.append("order_check")
        raise AssertionError("missing approval must block before order_check")

    def order_send(self, request: object) -> object:
        self.calls.append("order_send")
        raise AssertionError("missing approval must block before order_send")


class LiveRunnerTests(unittest.TestCase):
    def test_live_cycle_without_approval_does_not_touch_mt5(self) -> None:
        fake_mt5 = FakeMT5()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(LiveTradingApprovalError):
                run_live_cycle(
                    BotConfig(database_url=str(root / "trading.db"), target_symbols=SYMBOLS),
                    database_url=root / "trading.db",
                    target_symbols=SYMBOLS,
                    symbol_map_path=root / "missing_symbol_map.json",
                    live_approval_file=root / "LIVE_APPROVED.json",
                    minutes_limit=1,
                    mt5_module=fake_mt5,
                )

        self.assertEqual(fake_mt5.calls, [])

    def test_retry_guard_suppresses_exact_duplicate_blocked_intent(self) -> None:
        guard = OrderRetryGuard(cooldown_seconds=600)
        order_intent = _order_intent()
        first_allowed, first_suppressed = guard.filter_order_intents(
            (order_intent,),
            now_utc=NOW,
        )

        guard.observe_risk_result(
            _blocked_risk_result(order_intent, "block: stop loss is required for new risk"),
            now_utc=NOW,
        )
        second_allowed, second_suppressed = guard.filter_order_intents(
            (order_intent,),
            now_utc=NOW + timedelta(seconds=15),
        )

        self.assertEqual(first_allowed, (order_intent,))
        self.assertEqual(first_suppressed, ())
        self.assertEqual(second_allowed, ())
        self.assertEqual(len(second_suppressed), 1)
        self.assertIn("stop loss", second_suppressed[0].reason)

    def test_retry_guard_allows_changed_signal_fingerprint(self) -> None:
        guard = OrderRetryGuard(cooldown_seconds=600)
        old_intent = _order_intent(signal_id="sig-xrp-old", feature_time=NOW)
        new_intent = _order_intent(
            signal_id="sig-xrp-new",
            feature_time=NOW + timedelta(minutes=5),
            price=1.106,
        )

        guard.observe_risk_result(
            _blocked_risk_result(old_intent, "block: stop loss is required for new risk"),
            now_utc=NOW,
        )
        allowed, suppressed = guard.filter_order_intents(
            (new_intent,),
            now_utc=NOW + timedelta(seconds=15),
        )

        self.assertEqual(allowed, (new_intent,))
        self.assertEqual(suppressed, ())


def _order_intent(
    *,
    signal_id: str = "sig-xrp-old",
    feature_time: datetime = NOW,
    price: float = 1.10522,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=f"intent-{signal_id}",
        signal_id=signal_id,
        created_at_utc=NOW,
        symbol="XRP/USD",
        side=OrderSide.BUY,
        requested_volume=100.0,
        requested_price=price,
        stop_loss=None,
        take_profit=None,
        metadata={"feature_time_utc": feature_time.isoformat()},
    )


def _blocked_risk_result(order_intent: OrderIntent, reason: str) -> RiskBatchResult:
    return RiskBatchResult(
        (
            RiskGateDecision(
                order_intent=order_intent,
                risk_check=RiskCheck(
                    check_id="risk-1",
                    signal_id=order_intent.signal_id,
                    checked_at_utc=NOW,
                    passed=False,
                    symbol=order_intent.symbol,
                    reason=reason,
                    details={"reasons": [reason]},
                ),
            ),
        )
    )


if __name__ == "__main__":
    unittest.main()
