from __future__ import annotations

import tempfile
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig, load_config
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS
from mt5_crypto_bot.schemas import (
    AccountSnapshot,
    Direction,
    ExecutionResult,
    ExecutionStatus,
    Fill,
    FillSide,
    OrderIntent,
    OrderSide,
    PositionSide,
    PositionSnapshot,
    RiskCheck,
    Signal,
    SignalDecision,
    StrategyParams,
    SymbolConfig,
)


class ConfigValidationTests(unittest.TestCase):
    def test_defaults_are_dry_run_and_do_not_require_secrets(self) -> None:
        config = load_config(env={}, env_file=None)

        self.assertEqual(config.trade_mode, "dry_run")
        self.assertEqual(config.target_symbols, ALLOWED_SYMBOLS)
        self.assertEqual(config.entry_threshold, 1.25)
        self.assertEqual(config.exit_threshold, 0.75)
        self.assertEqual(config.max_gross_leverage, 27.0)
        self.assertEqual(config.max_symbol_leverage, 27.0)
        self.assertEqual(config.max_margin_usage, 0.90)
        self.assertIsNone(config.mt5_login)
        self.assertIsNone(config.mt5_password)

    def test_target_symbols_parse_csv_from_environment(self) -> None:
        config = load_config(
            env={"TARGET_SYMBOLS": "btc/usd, ETH/USD,sol/usd"},
            env_file=None,
        )

        self.assertEqual(config.target_symbols, ("BTC/USD", "ETH/USD", "SOL/USD"))

    def test_invalid_target_symbols_fail_fast(self) -> None:
        with self.assertRaises(ValidationError):
            load_config(env={"TARGET_SYMBOLS": "BTC/USD,DOGE/USD"}, env_file=None)

    def test_live_trade_mode_is_not_available_in_current_non_live_build(self) -> None:
        with self.assertRaises(ValidationError):
            load_config(env={"TRADE_MODE": "live"}, env_file=None)

    def test_environment_overrides_dotenv_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "TRADE_MODE=dry_run\nTARGET_SYMBOLS=BTC/USD\nBOT_MAGIC=111\n",
                encoding="utf-8",
            )

            config = load_config(
                env={"TARGET_SYMBOLS": "ETH/USD,XRP/USD", "BOT_MAGIC": "222"},
                env_file=env_path,
            )

        self.assertEqual(config.target_symbols, ("ETH/USD", "XRP/USD"))
        self.assertEqual(config.bot_magic, 222)

    def test_risk_caps_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            BotConfig(max_symbol_leverage=28.0, max_gross_leverage=27.0)

    def test_strategy_threshold_overrides_flow_into_strategy_params(self) -> None:
        config = load_config(
            env={"ENTRY_THRESHOLD": "0.75", "EXIT_THRESHOLD": "0.25"},
            env_file=None,
        )

        params = config.strategy_params()

        self.assertEqual(params.entry_threshold, 0.75)
        self.assertEqual(params.exit_threshold, 0.25)


class SchemaValidationTests(unittest.TestCase):
    def test_symbol_config_enforces_allow_list(self) -> None:
        self.assertEqual(SymbolConfig(symbol="bar/usd").symbol, "BAR/USD")

        with self.assertRaises(ValidationError):
            SymbolConfig(symbol="DOGE/USD")

    def test_strategy_params_validate_ordered_thresholds(self) -> None:
        params = StrategyParams()
        self.assertEqual(params.strategy_version, "momo_v1")
        self.assertEqual(params.entry_threshold, 1.25)
        self.assertEqual(params.exit_threshold, 0.75)
        self.assertEqual(params.max_gross_leverage, 27.0)
        self.assertEqual(params.max_symbol_leverage, 27.0)
        self.assertEqual(params.max_margin_usage, 0.90)

        with self.assertRaises(ValidationError):
            StrategyParams(entry_threshold=0.25, exit_threshold=0.35)

    def test_trading_schemas_accept_allowed_symbols_only(self) -> None:
        now = datetime(2026, 6, 22, tzinfo=timezone.utc)

        signal = Signal(
            signal_id="sig-1",
            created_at_utc=now,
            strategy_version="momo_v1",
            symbol="BTC/USD",
            timeframe="M5",
            direction=Direction.LONG,
            score=1.5,
            target_leverage=0.5,
            decision=SignalDecision.ENTER,
        )
        self.assertEqual(signal.symbol, "BTC/USD")

        risk_check = RiskCheck(
            checked_at_utc=now,
            passed=True,
            symbol="ETH/USD",
            margin_usage=0.1,
            gross_leverage=1.0,
        )
        self.assertTrue(risk_check.passed)

        order_intent = OrderIntent(
            client_order_id="order-1",
            created_at_utc=now,
            symbol="SOL/USD",
            side=OrderSide.BUY,
            requested_volume=1.0,
        )
        self.assertEqual(order_intent.side, "buy")

        result = ExecutionResult(
            client_order_id="order-1",
            executed_at_utc=now,
            status=ExecutionStatus.DRY_RUN,
            symbol="SOL/USD",
            requested_volume=1.0,
        )
        self.assertEqual(result.trade_mode, "dry_run")

        AccountSnapshot(observed_at_utc=now, balance=1_000_000, equity=1_000_000)
        PositionSnapshot(
            observed_at_utc=now,
            symbol="XRP/USD",
            side=PositionSide.LONG,
            volume=10.0,
        )
        Fill(
            symbol="BAR/USD",
            filled_at_utc=now,
            side=FillSide.BUY,
            volume=1.0,
            price=0.10,
        )

        with self.assertRaises(ValidationError):
            OrderIntent(
                client_order_id="bad-order",
                created_at_utc=now,
                symbol="EUR/USD",
                side=OrderSide.BUY,
                requested_volume=1.0,
            )


if __name__ == "__main__":
    unittest.main()
