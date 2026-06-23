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
from mt5_crypto_bot.schemas import ExecutionResult, ExecutionStatus, OrderIntent, OrderSide, RiskCheck
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
    BOOK_TYPE_SELL = 1
    BOOK_TYPE_BUY = 2
    BOOK_TYPE_SELL_MARKET = 3
    BOOK_TYPE_BUY_MARKET = 4
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE_PARTIAL = 10010

    def __init__(self, *, last_error: tuple[object, ...] = (-1, "order_check unavailable")) -> None:
        self.calls: list[str] = []
        self._last_error = last_error

    def order_check(self, request: object) -> object:
        self.calls.append("order_check")
        raise AssertionError("dry-run tests must not call order_check")

    def order_send(self, request: object) -> object:
        self.calls.append("order_send")
        raise AssertionError("dry-run tests must not call order_send")

    def last_error(self) -> tuple[object, ...]:
        self.calls.append("last_error")
        return self._last_error

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
    deal: int | None = None
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
    retcode: int | None = None
    comment: str | None = None
    time_msc: int | None = None
    time_update_msc: int | None = None


class FakeLiveMT5(FakeMT5):
    def __init__(
        self,
        *,
        check_retcode: int = FakeMT5.TRADE_RETCODE_DONE,
        bid: float = 100.4,
        ask: float = 100.6,
        book: tuple[object, ...] = (),
        send_retcode: int = FakeMT5.TRADE_RETCODE_DONE,
        send_volume: float | None = None,
        send_price: float | None = None,
        last_error: tuple[object, ...] = (-2, "order_check returned None"),
    ) -> None:
        super().__init__(last_error=last_error)
        self.check_retcode = check_retcode
        self.bid = bid
        self.ask = ask
        self.book = book
        self.send_retcode = send_retcode
        self.send_volume = send_volume
        self.send_price = send_price

    def symbol_info_tick(self, symbol: str) -> object:
        self.calls.append("symbol_info_tick")
        return {
            "symbol": symbol,
            "bid": self.bid,
            "ask": self.ask,
            "time_msc": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

    def symbol_info(self, symbol: str) -> object:
        self.calls.append("symbol_info")
        return {
            "symbol": symbol,
            "digits": 2,
            "point": 0.01,
            "trade_stops_level": 0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }

    def market_book_get(self, symbol: str) -> tuple[object, ...]:
        self.calls.append("market_book_get")
        return self.book

    def order_check(self, request: object) -> object:
        self.calls.append("order_check")
        self.check_request = request
        return SimpleObject(retcode=self.check_retcode, comment="check result")

    def order_send(self, request: object) -> object:
        self.calls.append("order_send")
        self.send_request = request
        request_data = request if isinstance(request, dict) else {}
        return SimpleObject(
            retcode=self.send_retcode,
            order=555,
            deal=777,
            volume=self.send_volume if self.send_volume is not None else float(request_data.get("volume", 1.25)),
            price=self.send_price if self.send_price is not None else float(request_data.get("price", 100.5)),
            comment="send result",
        )


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

    def test_live_order_check_rejection_does_not_call_order_send(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"live_approved": True}), encoding="utf-8")
                fake_mt5 = FakeLiveMT5(check_retcode=10013)

                result = ExecutionEngine(
                    trade_mode="live",
                    live_approval_file=approval_file,
                ).execute_approved_order(approved_order(), mt5_module=fake_mt5)
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertIn("order_check", fake_mt5.calls)
        self.assertNotIn("order_send", fake_mt5.calls)
        self.assertEqual(result.trade_mode, "live")
        self.assertEqual(result.status, "rejected")
        self.assertTrue(result.result["order_check_called"])
        self.assertFalse(result.result["order_send_called"])

    def test_live_order_check_none_records_last_error(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"live_approved": True}), encoding="utf-8")

                class NoneCheckMT5(FakeLiveMT5):
                    def order_check(self, request: object) -> object:
                        self.calls.append("order_check")
                        return None

                fake_mt5 = NoneCheckMT5(last_error=(-10004, "request rejected by terminal"))
                result = ExecutionEngine(
                    trade_mode="live",
                    live_approval_file=approval_file,
                ).execute_approved_order(approved_order(), mt5_module=fake_mt5)
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertIn("order_check", fake_mt5.calls)
        self.assertIn("last_error", fake_mt5.calls)
        self.assertNotIn("order_send", fake_mt5.calls)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.result["last_error"]["code"], -10004)
        self.assertEqual(result.result["last_error"]["message"], "request rejected by terminal")

    def test_live_order_check_then_order_send_records_live_result(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"live_approved": True}), encoding="utf-8")
                fake_mt5 = FakeLiveMT5()
                with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                    result = ExecutionEngine(
                        trade_mode="live",
                        live_approval_file=approval_file,
                    ).execute_approved_order(
                        approved_order(),
                        store=store,
                        mt5_module=fake_mt5,
                    )
                    row = store.fetch_one(
                        "SELECT status, result_json FROM orders WHERE client_order_id = ?",
                        ("order-1",),
                    )
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertIn("order_check", fake_mt5.calls)
        self.assertIn("order_send", fake_mt5.calls)
        self.assertEqual(result.trade_mode, "live")
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.mt5_order_ticket, 555)
        self.assertEqual(result.mt5_deal_ticket, 777)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "filled")
        payload = json.loads(row["result_json"])
        self.assertTrue(payload["result"]["order_check_called"])
        self.assertTrue(payload["result"]["order_send_called"])

    def test_live_order_uses_fresh_tick_and_preserves_stop_offsets(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"live_approved": True}), encoding="utf-8")
                fake_mt5 = FakeLiveMT5(bid=100.9, ask=101.0)
                with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                    result = ExecutionEngine(
                        trade_mode="live",
                        live_approval_file=approval_file,
                    ).execute_approved_order(
                        approved_order(),
                        store=store,
                        mt5_module=fake_mt5,
                    )
                    row = store.fetch_one(
                        "SELECT requested_price, sl, tp, request_json, result_json FROM orders WHERE client_order_id = ?",
                        ("order-1",),
                    )
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertEqual(result.status, "filled")
        self.assertEqual(fake_mt5.check_request["price"], 101.0)
        self.assertEqual(fake_mt5.check_request["sl"], 98.5)
        self.assertEqual(fake_mt5.check_request["tp"], 104.5)
        self.assertIsNotNone(row)
        self.assertEqual(row["requested_price"], 101.0)
        self.assertIn('"price":101.0', row["request_json"])
        payload = json.loads(row["result_json"])
        refresh = payload["result"]["live_precheck"]["live_refresh"]
        self.assertEqual(refresh["original_requested_price"], 100.5)
        self.assertEqual(refresh["live_requested_price"], 101.0)

    def test_live_liquidity_caps_volume_before_order_check(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"live_approved": True}), encoding="utf-8")
                fake_mt5 = FakeLiveMT5(
                    bid=100.4,
                    ask=100.6,
                    book=(
                        {"type": FakeMT5.BOOK_TYPE_SELL, "price": 100.6, "volume_dbl": 0.50},
                        {"type": FakeMT5.BOOK_TYPE_BUY, "price": 100.4, "volume_dbl": 2.00},
                    ),
                )
                result = ExecutionEngine(
                    trade_mode="live",
                    live_approval_file=approval_file,
                ).execute_approved_order(approved_order(), mt5_module=fake_mt5)
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertEqual(result.status, "filled")
        self.assertEqual(fake_mt5.check_request["volume"], 0.4)
        liquidity = result.result["live_precheck"]["liquidity"]
        self.assertEqual(liquidity["visible_volume"], 0.5)
        self.assertEqual(liquidity["requested_volume_after"], 0.4)

    def test_live_liquidity_rejects_before_order_check_when_below_minimum(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"live_approved": True}), encoding="utf-8")
                fake_mt5 = FakeLiveMT5(
                    book=(
                        {"type": FakeMT5.BOOK_TYPE_SELL, "price": 100.6, "volume_dbl": 0.005},
                    ),
                )
                result = ExecutionEngine(
                    trade_mode="live",
                    live_approval_file=approval_file,
                ).execute_approved_order(approved_order(), mt5_module=fake_mt5)
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.result["order_check_called"])
        self.assertFalse(result.result["order_send_called"])
        self.assertNotIn("order_check", fake_mt5.calls)
        self.assertNotIn("order_send", fake_mt5.calls)

    def test_live_smaller_done_volume_is_recorded_as_partial(self) -> None:
        old_value = os.environ.get("LIVE_APPROVED")
        os.environ["LIVE_APPROVED"] = "true"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                approval_file = Path(tmpdir) / "LIVE_APPROVED.json"
                approval_file.write_text(json.dumps({"live_approved": True}), encoding="utf-8")
                fake_mt5 = FakeLiveMT5(send_volume=0.75)
                result = ExecutionEngine(
                    trade_mode="live",
                    live_approval_file=approval_file,
                ).execute_approved_order(approved_order(), mt5_module=fake_mt5)
        finally:
            if old_value is None:
                os.environ.pop("LIVE_APPROVED", None)
            else:
                os.environ["LIVE_APPROVED"] = old_value

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.filled_volume, 0.75)

    def test_reconciliation_helpers_are_read_only_and_store_snapshots(self) -> None:
        fake_mt5 = FakeMT5()
        mapping = {"BTCUSD": "BTC/USD"}
        with tempfile.TemporaryDirectory() as tmpdir:
            with SQLiteStore(Path(tmpdir) / "trading.db") as store:
                store.upsert_execution_result(
                    ExecutionResult(
                        client_order_id="filled-order",
                        executed_at_utc=NOW,
                        status=ExecutionStatus.FILLED,
                        symbol="BTC/USD",
                        requested_volume=1.5,
                        requested_price=99.0,
                        mt5_order_ticket=301,
                        mt5_deal_ticket=201,
                    )
                )
                positions = read_positions(fake_mt5, broker_to_canonical=mapping, store=store)
                fills = read_deals(fake_mt5, broker_to_canonical=mapping, store=store)

                self.assertEqual(store.count_rows("positions_snapshots"), 1)
                self.assertEqual(store.count_rows("fills"), 1)

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "BTC/USD")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].symbol, "BTC/USD")
        self.assertAlmostEqual(fills[0].slippage_bps or 0.0, 101.01010101010101)
        self.assertIn("positions_get", fake_mt5.calls)
        self.assertIn("history_deals_get", fake_mt5.calls)
        self.assertNotIn("order_check", fake_mt5.calls)
        self.assertNotIn("order_send", fake_mt5.calls)


if __name__ == "__main__":
    unittest.main()
