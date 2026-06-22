from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.execution import (
    DRY_RUN_RESULT_MESSAGE,
    ExecutionEngine,
    LiveTradingApprovalError,
    UnapprovedOrderError,
    build_mt5_order_request,
    read_deals,
    read_positions,
)
from mt5_crypto_bot.risk import RiskApprovedOrder
from mt5_crypto_bot.schemas import OrderIntent, OrderSide, RiskCheck
from mt5_crypto_bot.storage import SQLiteStore


NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


def order_intent() -> OrderIntent:
    return OrderIntent(
        client_order_id="order-1",
        signal_id="sig-1",
        created_at_utc=NOW,
        symbol="BTC/USD",
        side=OrderSide.BUY,
        requested_volume=1.25,
        requested_price=100.5,
        stop_loss=98.0,
        take_profit=104.0,
        comment="momo_v1:sig-1",
        metadata={"broker_symbol": "BTCUSD", "feature_time_utc": NOW.isoformat()},
    )


def risk_check(*, passed: bool = True) -> RiskCheck:
    return RiskCheck(
        check_id="risk-1",
        signal_id="sig-1",
        checked_at_utc=NOW,
        passed=passed,
        symbol="BTC/USD",
        reason="passed: risk checks approved order intent" if passed else "block: test",
        details={"approval_id": "approval-1"} if passed else {},
    )


def approved_order() -> RiskApprovedOrder:
    return RiskApprovedOrder(
        order_intent=order_intent(),
        risk_check=risk_check(),
        approval_id="approval-1",
        approved_at_utc=NOW,
    )


class FakeMT5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TIME_GTC = 0
    ORDER_TIME_DAY = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1

    def __init__(self) -> None:
        self.calls: list[str] = []

    def order_check(self, request: object) -> object:
        self.calls.append("order_check")
        raise AssertionError("dry-run tests must not call order_check")

    def order_send(self, request: object) -> object:
        self.calls.append("order_send")
        raise AssertionError("dry-run tests must not call order_send")

    def positions_get(self) -> tuple[object, ...]:
        self.calls.append("positions_get")
        return (
            SimpleObject(
                ticket=101,
                symbol="BTCUSD",
                type=self.POSITION_TYPE_BUY,
                volume=1.5,
                price_open=100.0,
                price_current=101.0,
                sl=98.0,
                tp=104.0,
                profit=150.0,
                time_update_msc=int(NOW.timestamp() * 1000),
            ),
        )

    def history_deals_get(self, start: datetime, end: datetime) -> tuple[object, ...]:
        self.calls.append("history_deals_get")
        return (
            SimpleObject(
                ticket=201,
                order=301,
                position_id=101,
                symbol="BTCUSD",
                type=self.DEAL_TYPE_BUY,
                volume=1.5,
                price=100.0,
                profit=25.0,
                commission=0.0,
                swap=0.0,
                time_msc=int(NOW.timestamp() * 1000),
            ),
        )


@dataclass
class SimpleObject:
    ticket: int | None = None
    order: int | None = None
    position_id: int | None = None
    symbol: str = "BTCUSD"
    type: int = 0
    volume: float = 0.0
    price: float | None = None
    price_open: float | None = None
    price_current: float | None = None
    sl: float | None = None
    tp: float | None = None
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    time_msc: int | None = None
    time_update_msc: int | None = None


class ExecutionEngineTests(unittest.TestCase):
    def test_dry_run_records_order_and_result_without_mt5_calls(self) -> None:
        fake_mt5 = FakeMT5()
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                result = ExecutionEngine().execute_approved_order(
                    approved_order(),
                    store=store,
                    mt5_module=fake_mt5,
                )
                row = store.fetch_one(
                    "SELECT status, request_json, result_json FROM orders WHERE client_order_id = ?",
                    ("order-1",),
                )

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.filled_volume, 0.0)
        self.assertEqual(result.message, DRY_RUN_RESULT_MESSAGE)
        self.assertEqual(fake_mt5.calls, [])
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "dry_run")
        self.assertIn("order_send_called", row["result_json"])

    def test_execute_order_intent_requires_passing_risk_check(self) -> None:
        with self.assertRaises(UnapprovedOrderError):
            ExecutionEngine().execute_order_intent(
                order_intent(),
                risk_check=risk_check(passed=False),
            )

    def test_build_mt5_order_request_uses_broker_symbol_and_constants(self) -> None:
        request = build_mt5_order_request(order_intent(), mt5_module=FakeMT5())

        self.assertEqual(request["symbol"], "BTCUSD")
        self.assertEqual(request["type"], FakeMT5.ORDER_TYPE_BUY)
        self.assertEqual(request["action"], FakeMT5.TRADE_ACTION_DEAL)
        self.assertEqual(request["volume"], 1.25)
        self.assertEqual(request["price"], 100.5)
        self.assertEqual(request["sl"], 98.0)
        self.assertEqual(request["tp"], 104.0)

    def test_live_mode_without_approval_does_not_call_order_check_or_order_send(self) -> None:
        fake_mt5 = FakeMT5()
        old_value = os.environ.pop("LIVE_APPROVED", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                with self.assertRaises(LiveTradingApprovalError):
                    ExecutionEngine(
                        trade_mode="live",
                        live_approval_file=approval_file,
                    ).execute_approved_order(approved_order(), mt5_module=fake_mt5)
        finally:
            if old_value is not None:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertEqual(fake_mt5.calls, [])

    def test_live_approval_requires_valid_file_payload(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"approved": False}), encoding="utf-8")

                with self.assertRaises(LiveTradingApprovalError):
                    ExecutionEngine(
                        trade_mode="live",
                        live_approval_file=approval_file,
                    ).require_live_approval()
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

    def test_reconciliation_helpers_are_read_only_and_store_snapshots(self) -> None:
        fake_mt5 = FakeMT5()
        mapping = {"BTCUSD": "BTC/USD"}
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                positions = read_positions(fake_mt5, broker_to_canonical=mapping, store=store)
                fills = read_deals(fake_mt5, broker_to_canonical=mapping, store=store)

                self.assertEqual(store.count_rows("positions_snapshots"), 1)
                self.assertEqual(store.count_rows("fills"), 1)

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "BTC/USD")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].symbol, "BTC/USD")
        self.assertIn("positions_get", fake_mt5.calls)
        self.assertIn("history_deals_get", fake_mt5.calls)
        self.assertNotIn("order_check", fake_mt5.calls)
        self.assertNotIn("order_send", fake_mt5.calls)


if __name__ == "__main__":
    unittest.main()
