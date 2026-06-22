"""Read-only MetaTrader 5 connection helpers.

This module deliberately exposes connection and account-inspection helpers only.
Trading APIs are not wrapped here, and the verification flow never submits
orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_crypto_bot.config import BotConfig


TERMINAL_INFO_FIELDS: tuple[str, ...] = (
    "name",
    "company",
    "connected",
    "trade_allowed",
    "tradeapi_disabled",
    "dlls_allowed",
    "build",
    "version",
    "language",
    "maxbars",
    "path",
    "data_path",
    "commondata_path",
)

ACCOUNT_INFO_FIELDS: tuple[str, ...] = (
    "server",
    "currency",
    "leverage",
    "trade_allowed",
    "trade_expert",
    "balance",
    "credit",
    "profit",
    "equity",
    "margin",
    "margin_free",
    "margin_level",
    "margin_so_call",
    "margin_so_so",
    "margin_initial",
    "margin_maintenance",
    "assets",
    "liabilities",
)


class MT5Error(RuntimeError):
    """Base class for MT5 verification failures."""


class MT5DependencyError(MT5Error):
    """Raised when the optional MetaTrader5 Python package is unavailable."""


class MT5ConfigurationError(MT5Error):
    """Raised when local MT5 settings are missing or unsafe."""


class MT5ConnectionError(MT5Error):
    """Raised when MT5 initialize/login/read-only inspection fails."""

    def __init__(self, message: str, *, last_error: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error or {}


@dataclass(frozen=True)
class MT5Credentials:
    """Validated local credentials needed to establish a read-only MT5 session."""

    path: Path
    login: int
    password: str
    server: str
    timeout_ms: int


@dataclass(frozen=True)
class MT5ReadOnlySnapshot:
    """Sanitized result of a successful MT5 verification run."""

    observed_at_utc: datetime
    trade_mode: str
    terminal_info: dict[str, Any]
    account_info: dict[str, Any]
    last_error: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for CLI output or logs."""
        return {
            "observed_at_utc": self.observed_at_utc.isoformat(),
            "trade_mode": self.trade_mode,
            "terminal_info": self.terminal_info,
            "account_info": self.account_info,
            "last_error": self.last_error,
        }


def mask_account_login(value: Any) -> str | None:
    """Mask an MT5 account login before display."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= 2:
        return "*" * len(text)
    if len(text) <= 6:
        return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def load_mt5_module() -> Any:
    """Import the optional MetaTrader5 module only when it is needed."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise MT5DependencyError(
            "MetaTrader5 is not installed in this Python environment. "
            "On the Windows MT5 host, run `pip install -e .[mt5]` or "
            "`pip install MetaTrader5`, then retry. No MT5 calls were made."
        ) from exc
    return mt5


def build_mt5_credentials(config: BotConfig) -> MT5Credentials:
    """Validate local config and extract the fields needed for MT5 login."""
    trade_mode = _trade_mode_value(config)
    if trade_mode != "dry_run":
        raise MT5ConfigurationError(
            "MT5 connection verification must run with TRADE_MODE=dry_run. "
            f"Current trade mode is {trade_mode!r}; no MT5 calls were made."
        )

    missing: list[str] = []
    if config.mt5_path is None:
        missing.append("MT5_PATH")
    if config.mt5_login is None:
        missing.append("MT5_LOGIN")
    if config.mt5_password is None:
        missing.append("MT5_PASSWORD")
    if config.mt5_server is None:
        missing.append("MT5_SERVER")

    if missing:
        missing_values = ", ".join(missing)
        raise MT5ConfigurationError(
            "Missing required MT5 settings for read-only connection verification: "
            f"{missing_values}. Set them in local `.env` or process environment. "
            "Keep TRADE_MODE=dry_run. Do not commit real credentials."
        )

    assert config.mt5_path is not None
    assert config.mt5_login is not None
    assert config.mt5_password is not None
    assert config.mt5_server is not None

    path = config.mt5_path.expanduser()
    if not path.is_file():
        raise MT5ConfigurationError(
            f"MT5_PATH does not point to an existing terminal executable: {path}. "
            "Set MT5_PATH to terminal64.exe or the broker-provided MT5 terminal path."
        )

    raw_login = config.mt5_login.get_secret_value().strip()
    raw_password = config.mt5_password.get_secret_value()
    server = config.mt5_server.strip()

    if not raw_login:
        missing.append("MT5_LOGIN")
    if not raw_password:
        missing.append("MT5_PASSWORD")
    if not server:
        missing.append("MT5_SERVER")
    if missing:
        missing_values = ", ".join(sorted(set(missing)))
        raise MT5ConfigurationError(
            "Missing required MT5 settings for read-only connection verification: "
            f"{missing_values}. Values were not printed."
        )

    try:
        login = int(raw_login)
    except ValueError as exc:
        raise MT5ConfigurationError(
            "MT5_LOGIN must be numeric. The configured value was not printed."
        ) from exc

    return MT5Credentials(
        path=path,
        login=login,
        password=raw_password,
        server=server,
        timeout_ms=config.mt5_timeout_ms,
    )


def read_last_error(mt5_module: Any) -> dict[str, Any]:
    """Return a JSON-safe MT5 last_error payload."""
    try:
        raw = mt5_module.last_error()
    except Exception as exc:  # pragma: no cover - defensive around external extension
        return {"code": None, "message": f"last_error unavailable: {exc.__class__.__name__}"}

    if raw is None:
        return {"code": None, "message": None}
    if isinstance(raw, tuple):
        code = raw[0] if len(raw) >= 1 else None
        message = raw[1] if len(raw) >= 2 else None
        payload: dict[str, Any] = {"code": code, "message": message}
        if len(raw) > 2:
            payload["extra"] = [_json_safe(value) for value in raw[2:]]
        return payload
    return {"code": None, "message": _json_safe(raw)}

def initialize_mt5(credentials: MT5Credentials, mt5_module: Any | None = None) -> Any:
    """Initialize MT5 with a few safe connection variants.

    Some terminals reject authenticated ``initialize`` when they have stale saved
    account state, while others need the credentials embedded in ``initialize``.
    Try the authenticated path first, then fall back to initializing the
    terminal and letting ``login_mt5`` authenticate explicitly.
    """
    mt5 = mt5_module or load_mt5_module()
    attempts = (
        (
            "path_with_credentials",
            lambda: mt5.initialize(
                str(credentials.path),
                login=credentials.login,
                password=credentials.password,
                server=credentials.server,
                timeout=credentials.timeout_ms,
            ),
        ),
        (
            "path_then_login",
            lambda: mt5.initialize(str(credentials.path), timeout=credentials.timeout_ms),
        ),
        (
            "default_terminal_with_credentials",
            lambda: mt5.initialize(
                login=credentials.login,
                password=credentials.password,
                server=credentials.server,
                timeout=credentials.timeout_ms,
            ),
        ),
        (
            "default_terminal_then_login",
            lambda: mt5.initialize(timeout=credentials.timeout_ms),
        ),
    )
    errors: list[dict[str, Any]] = []
    for label, attempt in attempts:
        try:
            initialized = bool(attempt())
        except TypeError as exc:
            errors.append(
                {
                    "attempt": label,
                    "code": None,
                    "message": f"initialize signature mismatch: {exc.__class__.__name__}",
                }
            )
            continue
        if initialized:
            return mt5
        last_error = read_last_error(mt5)
        last_error["attempt"] = label
        errors.append(last_error)
        shutdown_mt5(mt5)

    last_error = dict(errors[-1]) if errors else read_last_error(mt5)
    last_error["attempts"] = errors
    raise MT5ConnectionError(
        "MT5 initialize failed after authenticated and terminal-only attempts. "
        "Check MT5_PATH, the exact MT5 server name, terminal installation, and "
        "Windows permissions. Credentials were not printed.",
        last_error=last_error,
        )


def login_mt5(credentials: MT5Credentials, mt5_module: Any) -> None:
    """Log in to MT5 using validated local credentials."""
    logged_in = mt5_module.login(
        credentials.login,
        password=credentials.password,
        server=credentials.server,
        timeout=credentials.timeout_ms,
    )
    if not logged_in:
        raise MT5ConnectionError(
            "MT5 login failed. Check MT5_LOGIN, MT5_SERVER, MT5_PASSWORD, and account "
            "activation status. Secret values were not printed.",
            last_error=read_last_error(mt5_module),
        )


def shutdown_mt5(mt5_module: Any | None = None) -> bool:
    """Shutdown the MT5 Python session, returning whether the call succeeded."""
    mt5 = mt5_module or load_mt5_module()
    try:
        return bool(mt5.shutdown())
    except Exception:  # pragma: no cover - defensive around external extension
        return False


def read_terminal_info(mt5_module: Any) -> dict[str, Any]:
    """Read and sanitize terminal_info from an initialized MT5 session."""
    terminal_info = mt5_module.terminal_info()
    if terminal_info is None:
        raise MT5ConnectionError(
            "MT5 terminal_info returned no data after login.",
            last_error=read_last_error(mt5_module),
        )
    return sanitize_terminal_info(terminal_info)


def read_account_info(mt5_module: Any) -> dict[str, Any]:
    """Read and sanitize account_info from an initialized MT5 session."""
    account_info = mt5_module.account_info()
    if account_info is None:
        raise MT5ConnectionError(
            "MT5 account_info returned no data after login.",
            last_error=read_last_error(mt5_module),
        )
    return sanitize_account_info(account_info)


def verify_mt5_connection(
    config: BotConfig,
    mt5_module: Any | None = None,
    *,
    shutdown: bool = True,
) -> MT5ReadOnlySnapshot:
    """Initialize, login, read sanitized status, and shut down cleanly.

    The flow uses only read-only MT5 calls: initialize, login, terminal_info,
    account_info, last_error, and shutdown.
    """
    credentials = build_mt5_credentials(config)
    mt5 = mt5_module or load_mt5_module()
    initialized = False
    try:
        initialize_mt5(credentials, mt5)
        initialized = True
        login_mt5(credentials, mt5)
        return MT5ReadOnlySnapshot(
            observed_at_utc=datetime.now(timezone.utc),
            trade_mode=_trade_mode_value(config),
            terminal_info=read_terminal_info(mt5),
            account_info=read_account_info(mt5),
            last_error=read_last_error(mt5),
        )
    finally:
        if shutdown and initialized:
            shutdown_mt5(mt5)


def sanitize_terminal_info(terminal_info: Any) -> dict[str, Any]:
    """Keep terminal status fields that are useful and safe to print."""
    raw = _object_to_mapping(terminal_info)
    return {
        field: _json_safe(raw[field])
        for field in TERMINAL_INFO_FIELDS
        if field in raw and raw[field] is not None
    }


def sanitize_account_info(account_info: Any) -> dict[str, Any]:
    """Keep account risk/balance fields and mask the account login."""
    raw = _object_to_mapping(account_info)
    sanitized: dict[str, Any] = {}
    if "login" in raw:
        sanitized["login_masked"] = mask_account_login(raw["login"])
    for field in ACCOUNT_INFO_FIELDS:
        if field in raw and raw[field] is not None:
            sanitized[field] = _json_safe(raw[field])
    return sanitized


def _object_to_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        return dict(asdict())
    try:
        return dict(vars(value))
    except TypeError:
        pass
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:  # pragma: no cover - defensive around external objects
            continue
        if not callable(item):
            result[name] = item
    return result


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _trade_mode_value(config: BotConfig) -> str:
    value = config.trade_mode
    return str(getattr(value, "value", value))
