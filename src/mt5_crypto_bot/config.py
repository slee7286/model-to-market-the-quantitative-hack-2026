"""Configuration loading for the dry-run-first MT5 crypto bot."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator

from mt5_crypto_bot.constants import (
    ALLOWED_SYMBOLS,
    DEFAULT_DATABASE_URL,
    DEFAULT_STRATEGY_VERSION,
    DEFAULT_TRADE_MODE,
)
from mt5_crypto_bot.schemas import StrategyParams, StrictBaseModel, TradeMode, normalize_symbols


ENV_FIELD_MAP: dict[str, str] = {
    "mt5_login": "MT5_LOGIN",
    "mt5_password": "MT5_PASSWORD",
    "mt5_server": "MT5_SERVER",
    "mt5_path": "MT5_PATH",
    "mt5_timeout_ms": "MT5_TIMEOUT_MS",
    "trade_mode": "TRADE_MODE",
    "target_symbols": "TARGET_SYMBOLS",
    "bot_magic": "BOT_MAGIC",
    "strategy_version": "STRATEGY_VERSION",
    "entry_threshold": "ENTRY_THRESHOLD",
    "exit_threshold": "EXIT_THRESHOLD",
    "database_url": "DATABASE_URL",
    "postgres_uri": "POSTGRES_URI",
    "parquet_dir": "PARQUET_DIR",
    "max_gross_leverage": "MAX_GROSS_LEVERAGE",
    "max_symbol_leverage": "MAX_SYMBOL_LEVERAGE",
    "max_margin_usage": "MAX_MARGIN_USAGE",
    "daily_drawdown_stop": "DAILY_DRAWDOWN_STOP",
    "total_drawdown_stop": "TOTAL_DRAWDOWN_STOP",
    "logfire_token": "LOGFIRE_TOKEN",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "doubleword_api_key": "DOUBLEWORD_API_KEY",
    "doubleword_base_url": "DOUBLEWORD_BASE_URL",
    "pydantic_ai_gateway_url": "PYDANTIC_AI_GATEWAY_URL",
}


SECRET_FIELD_NAMES = {
    "mt5_login",
    "mt5_password",
    "postgres_uri",
    "logfire_token",
    "anthropic_api_key",
    "doubleword_api_key",
}


class BotConfig(StrictBaseModel):
    """Runtime configuration validated from environment variables and `.env`."""

    mt5_login: SecretStr | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None
    mt5_path: Path | None = None
    mt5_timeout_ms: int = Field(default=60_000, gt=0)

    trade_mode: TradeMode = TradeMode.DRY_RUN
    target_symbols: tuple[str, ...] = ALLOWED_SYMBOLS
    bot_magic: int = Field(default=20260621, ge=0)
    strategy_version: str = DEFAULT_STRATEGY_VERSION

    # PnL sprint tuning for the 2026-06-24 22:00 BST qualification cutoff.
    # Stored live-signal replay scored by return only favored 1.25 / 0.75.
    entry_threshold: float = Field(default=1.25, gt=0)
    exit_threshold: float = Field(default=0.75, ge=0)

    database_url: str = DEFAULT_DATABASE_URL
    postgres_uri: SecretStr | None = None
    parquet_dir: Path = Path("data/parquet")

    max_gross_leverage: float = Field(default=27.0, gt=0, le=27.0)
    max_symbol_leverage: float = Field(default=27.0, gt=0, le=27.0)
    max_margin_usage: float = Field(default=0.90, gt=0, le=0.90)
    daily_drawdown_stop: float = Field(default=0.06, gt=0, lt=0.50)
    total_drawdown_stop: float = Field(default=0.10, gt=0, lt=0.50)

    logfire_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    doubleword_api_key: SecretStr | None = None
    doubleword_base_url: str = "https://api.doubleword.ai/v1"
    pydantic_ai_gateway_url: str | None = None

    @field_validator(
        "mt5_login",
        "mt5_password",
        "postgres_uri",
        "logfire_token",
        "anthropic_api_key",
        "doubleword_api_key",
        mode="before",
    )
    @classmethod
    def empty_secret_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("mt5_server", "pydantic_ai_gateway_url", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("target_symbols", mode="before")
    @classmethod
    def validate_target_symbols(cls, value: Any) -> tuple[str, ...]:
        return normalize_symbols(value)

    @field_validator("trade_mode", mode="before")
    @classmethod
    def normalize_trade_mode(cls, value: Any) -> Any:
        if value is None or value == "":
            return DEFAULT_TRADE_MODE
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == TradeMode.LIVE.value:
                raise ValueError(
                    "TRADE_MODE=live is reserved for the separate guarded live runner; "
                    "keep shared config in dry_run or paper mode"
                )
            return normalized
        if getattr(value, "value", value) == TradeMode.LIVE.value:
            raise ValueError(
                "TRADE_MODE=live is reserved for the separate guarded live runner; "
                "keep shared config in dry_run or paper mode"
            )
        return value

    @model_validator(mode="after")
    def validate_risk_caps(self) -> BotConfig:
        if self.max_symbol_leverage > self.max_gross_leverage:
            raise ValueError("max_symbol_leverage cannot exceed max_gross_leverage")
        if self.daily_drawdown_stop > self.total_drawdown_stop:
            raise ValueError("daily_drawdown_stop cannot exceed total_drawdown_stop")
        if self.exit_threshold >= self.entry_threshold:
            raise ValueError("exit_threshold must be lower than entry_threshold")
        return self

    def strategy_params(self) -> StrategyParams:
        """Build strategy parameters from validated config risk caps."""
        return StrategyParams(
            strategy_version=self.strategy_version,
            entry_threshold=self.entry_threshold,
            exit_threshold=self.exit_threshold,
            max_gross_leverage=self.max_gross_leverage,
            max_symbol_leverage=self.max_symbol_leverage,
            max_margin_usage=self.max_margin_usage,
            daily_drawdown_stop=self.daily_drawdown_stop,
            total_drawdown_stop=self.total_drawdown_stop,
        )

    def safe_snapshot(self) -> dict[str, Any]:
        """Return config values safe for logs and tests, excluding secrets."""
        data = self.model_dump(mode="json", exclude=SECRET_FIELD_NAMES)
        data["target_symbols"] = tuple(data["target_symbols"])
        return data


def read_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Read a simple dotenv file without requiring optional runtime packages."""
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def collect_config_values(
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = ".env",
) -> dict[str, Any]:
    """Collect BotConfig field values from `.env` and process environment data."""
    raw_values: dict[str, str] = {}
    if env_file is not None:
        raw_values.update(read_env_file(env_file))
    raw_values.update(os.environ if env is None else env)

    config_values: dict[str, Any] = {}
    for field_name, env_name in ENV_FIELD_MAP.items():
        value = raw_values.get(env_name)
        if value is None or value == "":
            continue
        config_values[field_name] = value
    return config_values


def load_config(
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = ".env",
) -> BotConfig:
    """Load and validate configuration from `.env` plus environment variables."""
    return BotConfig(**collect_config_values(env=env, env_file=env_file))


def default_config_snapshot() -> dict[str, object]:
    """Return non-secret defaults used by the scaffold and tests."""
    config = BotConfig()
    return {
        "trade_mode": config.trade_mode,
        "target_symbols": config.target_symbols,
        "database_url": config.database_url,
    }
