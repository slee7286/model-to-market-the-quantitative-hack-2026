from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5_crypto_bot.config import BotConfig
from mt5_crypto_bot.constants import ALLOWED_SYMBOLS
from mt5_crypto_bot.symbols import bootstrap_symbols, build_symbol_map


def make_symbol(name: str, **overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "name": name,
        "description": f"{name} crypto against US Dollar",
        "path": f"Crypto\\{name}",
        "currency_base": name.replace("/", "").replace(".r", "").replace("_i", "")[:3],
        "currency_profit": "USD",
        "currency_margin": "USD",
        "digits": 2,
        "point": 0.01,
        "trade_contract_size": 1.0,
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "spread": 20,
        "trade_mode": 4,
        "filling_mode": 1,
        "margin_initial": 0.0,
        "margin_maintenance": 0.0,
        "margin_hedged": 0.0,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class FakeMT5Symbols:
    def __init__(self, symbols: list[SimpleNamespace]) -> None:
        self.symbols = symbols
        self.symbols_by_name = {symbol.name: symbol for symbol in symbols}
        self.calls: list[str] = []
        self.shutdown_called = False

    def initialize(self, path: str, *, timeout: int) -> bool:
        self.calls.append("initialize")
        return True

    def login(self, login: int, *, password: str, server: str, timeout: int) -> bool:
        self.calls.append("login")
        return True

    def symbols_get(self) -> list[SimpleNamespace]:
        self.calls.append("symbols_get")
        return self.symbols

    def symbol_info(self, symbol: str) -> SimpleNamespace | None:
        self.calls.append(f"symbol_info:{symbol}")
        return self.symbols_by_name.get(symbol)

    def last_error(self) -> tuple[int, str]:
        self.calls.append("last_error")
        return (0, "OK")

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


class SymbolDiscoveryTests(unittest.TestCase):
    def test_unique_exact_match_writes_confirmed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            terminal = root / "terminal64.exe"
            terminal.write_text("", encoding="utf-8")
            map_path = root / "config" / "symbol_map.json"
            metadata_path = root / "data" / "symbol_metadata.json"
            fake_mt5 = FakeMT5Symbols(
                [
                    make_symbol(
                        "BTCUSD",
                        description="Bitcoin vs US Dollar",
                        currency_base="BTC",
                    )
                ]
            )

            result = bootstrap_symbols(
                make_config(terminal),
                symbol_map_path=map_path,
                metadata_path=metadata_path,
                mt5_module=fake_mt5,
            )

            map_payload = json.loads(map_path.read_text(encoding="utf-8"))
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertTrue(fake_mt5.shutdown_called)
        self.assertEqual(set(map_payload["canonical_to_broker"]), set(ALLOWED_SYMBOLS))
        self.assertEqual(result.symbol_map["canonical_to_broker"]["BTC/USD"], "BTCUSD")
        self.assertEqual(result.symbol_map["symbols"]["BTC/USD"]["status"], "confirmed")
        self.assertEqual(metadata_payload["metadata"]["BTC/USD"]["broker_symbol"], "BTCUSD")
        self.assertEqual(metadata_payload["metadata"]["BTC/USD"]["contract_size"], 1.0)
        self.assertEqual(metadata_payload["metadata"]["BTC/USD"]["tick_size"], 0.01)
        self.assertEqual(metadata_payload["metadata"]["BTC/USD"]["volume_step"], 0.01)

    def test_ambiguous_candidates_are_not_auto_selected(self) -> None:
        raw_symbols = [
            make_symbol("BTCUSD.r", currency_base="BTC").__dict__,
            make_symbol("BTCUSD_i", currency_base="BTC").__dict__,
        ]

        payload = build_symbol_map(
            raw_symbols,
            {},
            generated_at_utc=datetime.now(timezone.utc),
        )

        self.assertIsNone(payload["canonical_to_broker"]["BTC/USD"])
        self.assertEqual(payload["symbols"]["BTC/USD"]["status"], "ambiguous")
        self.assertEqual(len(payload["symbols"]["BTC/USD"]["candidates"]), 2)

    def test_manual_mapping_confirms_one_candidate_after_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            terminal = root / "terminal64.exe"
            terminal.write_text("", encoding="utf-8")
            map_path = root / "config" / "symbol_map.json"
            metadata_path = root / "data" / "symbol_metadata.json"
            map_path.parent.mkdir(parents=True)
            map_path.write_text(
                json.dumps({"canonical_to_broker": {"BTC/USD": "BTCUSD.r"}}),
                encoding="utf-8",
            )
            fake_mt5 = FakeMT5Symbols(
                [
                    make_symbol("BTCUSD.r", currency_base="BTC"),
                    make_symbol("BTCUSD_i", currency_base="BTC"),
                ]
            )

            result = bootstrap_symbols(
                make_config(terminal),
                symbol_map_path=map_path,
                metadata_path=metadata_path,
                mt5_module=fake_mt5,
            )

        self.assertEqual(result.symbol_map["symbols"]["BTC/USD"]["status"], "confirmed")
        self.assertEqual(result.symbol_map["symbols"]["BTC/USD"]["source"], "manual")
        self.assertEqual(result.metadata["metadata"]["BTC/USD"]["broker_symbol"], "BTCUSD.r")

    def test_bar_usd_can_match_hbar_hedera_symbol(self) -> None:
        raw_symbols = [
            make_symbol(
                "HBARUSD",
                description="Hedera HBAR vs US Dollar",
                currency_base="HBAR",
            ).__dict__
        ]

        payload = build_symbol_map(
            raw_symbols,
            {},
            generated_at_utc=datetime.now(timezone.utc),
        )

        self.assertEqual(payload["canonical_to_broker"]["BAR/USD"], "HBARUSD")
        self.assertEqual(payload["symbols"]["BAR/USD"]["status"], "confirmed")
        reasons = " ".join(payload["symbols"]["BAR/USD"]["candidates"][0]["reasons"])
        self.assertIn("HBAR/Hedera", reasons)

    def test_forex_cross_can_match_non_usd_quote(self) -> None:
        raw_symbols = [
            make_symbol(
                "EURCHF",
                description="Euro vs Swiss Franc",
                path="Forex\\EURCHF",
                currency_base="EUR",
                currency_profit="CHF",
                currency_margin="EUR",
            ).__dict__
        ]

        payload = build_symbol_map(
            raw_symbols,
            {},
            generated_at_utc=datetime.now(timezone.utc),
        )

        self.assertEqual(payload["canonical_to_broker"]["EUR/CHF"], "EURCHF")
        self.assertEqual(payload["symbols"]["EUR/CHF"]["status"], "confirmed")
        reasons = " ".join(payload["symbols"]["EUR/CHF"]["candidates"][0]["reasons"])
        self.assertIn("canonical base and quote", reasons)

    def test_invalid_manual_mapping_to_unsupported_symbol_is_not_confirmed(self) -> None:
        raw_symbols = [
            make_symbol("BTCUSD", currency_base="BTC").__dict__,
            make_symbol("DOGEUSD", currency_base="DOGE").__dict__,
        ]

        payload = build_symbol_map(
            raw_symbols,
            {"BTC/USD": "DOGEUSD"},
            generated_at_utc=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )

        self.assertIsNone(payload["canonical_to_broker"]["BTC/USD"])
        self.assertEqual(payload["symbols"]["BTC/USD"]["status"], "invalid_manual")
        self.assertEqual(payload["symbols"]["BTC/USD"]["manual_broker_symbol"], "DOGEUSD")


if __name__ == "__main__":
    unittest.main()
