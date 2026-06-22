from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig, load_config
from mt5_crypto_bot.mt5_client import (
    MT5ConfigurationError,
    MT5ConnectionError,
    build_mt5_credentials,
    mask_account_login,
    verify_mt5_connection,
)


class FakeMT5:
    def __init__(
        self,
        *,
        initialize_result: bool = True,
        login_result: bool = True,
        terminal_info: object | None = None,
        account_info: object | None = None,
        last_error: tuple[object, ...] = (0, "OK"),
    ) -> None:
        self.initialize_result = initialize_result
        self.login_result = login_result
        self._terminal_info = terminal_info or SimpleNamespace(
            name="MetaTrader 5",
            company="Broker Demo",
            connected=True,
            trade_allowed=True,
            tradeapi_disabled=False,
            build=5000,
            path="C:/Program Files/MetaTrader 5/terminal64.exe",
        )
        self._account_info = account_info or SimpleNamespace(
            login=123456789,
            server="Demo-Server",
            currency="USD",
            leverage=30,
            balance=1_000_000.0,
            equity=999_500.0,
            profit=-500.0,
            margin=1_000.0,
            margin_free=998_500.0,
            margin_level=99_950.0,
            name="Sensitive Account Name",
        )
        self._last_error = last_error
        self.calls: list[str] = []
        self.login_password: str | None = None
        self.shutdown_called = False

    def initialize(self, path: str, *, timeout: int) -> bool:
        self.calls.append("initialize")
        self.initialize_path = path
        self.initialize_timeout = timeout
        return self.initialize_result

    def login(self, login: int, *, password: str, server: str, timeout: int) -> bool:
        self.calls.append("login")
        self.login_id = login
        self.login_password = password
        self.login_server = server
        self.login_timeout = timeout
        return self.login_result

    def terminal_info(self) -> object:
        self.calls.append("terminal_info")
        return self._terminal_info

    def account_info(self) -> object:
        self.calls.append("account_info")
        return self._account_info

    def last_error(self) -> tuple[object, ...]:
        self.calls.append("last_error")
        return self._last_error

    def shutdown(self) -> bool:
        self.calls.append("shutdown")
        self.shutdown_called = True
        return True


def make_config(terminal_path: Path) -> BotConfig:
    return BotConfig(
        mt5_path=terminal_path,
        mt5_login="123456789",
        mt5_password="dummy-password",
        mt5_server="Demo-Server",
        trade_mode="dry_run",
    )


class MT5ClientTests(unittest.TestCase):
    def test_mask_account_login(self) -> None:
        self.assertEqual(mask_account_login("123456789"), "12*****89")
        self.assertEqual(mask_account_login("1234"), "1**4")
        self.assertEqual(mask_account_login("12"), "**")
        self.assertIsNone(mask_account_login(""))

    def test_missing_required_settings_are_reported_without_values(self) -> None:
        config = load_config(env={}, env_file=None)

        with self.assertRaises(MT5ConfigurationError) as context:
            build_mt5_credentials(config)

        message = str(context.exception)
        self.assertIn("MT5_PATH", message)
        self.assertIn("MT5_LOGIN", message)
        self.assertIn("MT5_PASSWORD", message)
        self.assertIn("MT5_SERVER", message)

    def test_trade_mode_must_remain_dry_run(self) -> None:
        with self.assertRaises(ValidationError):
            BotConfig(trade_mode="live")

        with tempfile.TemporaryDirectory() as tmpdir:
            terminal = Path(tmpdir) / "terminal64.exe"
            terminal.write_text("", encoding="utf-8")
            config = make_config(terminal).model_copy(update={"trade_mode": "paper"})

            with self.assertRaises(MT5ConfigurationError):
                build_mt5_credentials(config)

    def test_successful_verification_masks_account_and_shuts_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            terminal = Path(tmpdir) / "terminal64.exe"
            terminal.write_text("", encoding="utf-8")
            fake_mt5 = FakeMT5()

            snapshot = verify_mt5_connection(make_config(terminal), mt5_module=fake_mt5)

        payload = json.dumps(snapshot.as_dict(), sort_keys=True)
        self.assertTrue(fake_mt5.shutdown_called)
        self.assertEqual(fake_mt5.calls[0], "initialize")
        self.assertIn("terminal_info", fake_mt5.calls)
        self.assertIn("account_info", fake_mt5.calls)
        self.assertNotIn("dummy-password", payload)
        self.assertNotIn("123456789", payload)
        self.assertNotIn("Sensitive Account Name", payload)
        self.assertIn("12*****89", payload)
        self.assertEqual(snapshot.account_info["balance"], 1_000_000.0)
        self.assertEqual(snapshot.account_info["equity"], 999_500.0)
        self.assertEqual(snapshot.account_info["margin"], 1_000.0)

    def test_initialize_failure_includes_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            terminal = Path(tmpdir) / "terminal64.exe"
            terminal.write_text("", encoding="utf-8")
            fake_mt5 = FakeMT5(
                initialize_result=False,
                last_error=(-10003, "IPC initialize failed"),
            )

            with self.assertRaises(MT5ConnectionError) as context:
                verify_mt5_connection(make_config(terminal), mt5_module=fake_mt5)

        self.assertFalse(fake_mt5.shutdown_called)
        self.assertEqual(context.exception.last_error["code"], -10003)
        self.assertEqual(context.exception.last_error["message"], "IPC initialize failed")

    def test_login_failure_shuts_down_and_includes_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            terminal = Path(tmpdir) / "terminal64.exe"
            terminal.write_text("", encoding="utf-8")
            fake_mt5 = FakeMT5(login_result=False, last_error=(-6, "Authorization failed"))

            with self.assertRaises(MT5ConnectionError) as context:
                verify_mt5_connection(make_config(terminal), mt5_module=fake_mt5)

        self.assertTrue(fake_mt5.shutdown_called)
        self.assertEqual(context.exception.last_error["code"], -6)
        self.assertEqual(context.exception.last_error["message"], "Authorization failed")


if __name__ == "__main__":
    unittest.main()
