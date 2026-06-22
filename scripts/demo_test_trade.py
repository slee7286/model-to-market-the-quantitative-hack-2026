"""One-off DEMO trade test: place a single small long BAR/USD position.

Purpose: prove the MT5 terminal can actually trade (not just read data), by
running order_check and, only if it passes, order_send for the minimum volume.
This is a manual diagnostic, separate from the guarded bot. It places ONE real
order on the connected (demo) account. Run only against a demo account.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mt5_crypto_bot.config import load_config
from mt5_crypto_bot.data_collector import load_confirmed_symbol_map
from mt5_crypto_bot.mt5_client import (
    build_mt5_credentials,
    initialize_mt5,
    login_mt5,
    read_last_error,
    shutdown_mt5,
)

CANONICAL = "BAR/USD"
DONE_OK = {0, 10008, 10009}  # 0 = check ok, 10008 placed, 10009 done


def const(mt5, name, fallback):
    return int(getattr(mt5, name, fallback))


def main() -> int:
    config = load_config(env_file=".env")
    creds = build_mt5_credentials(config)
    broker = load_confirmed_symbol_map(target_symbols=[CANONICAL])[CANONICAL]
    print(f"Canonical {CANONICAL} -> broker symbol {broker}")

    mt5 = initialize_mt5(creds)
    try:
        login_mt5(creds, mt5)

        ti = mt5.terminal_info()
        ai = mt5.account_info()
        print("\n=== TRADE PERMISSION DIAGNOSTICS ===")
        print(f"  terminal.connected      : {getattr(ti,'connected',None)}")
        print(f"  terminal.trade_allowed  : {getattr(ti,'trade_allowed',None)}  (False => AutoTrading OFF)")
        print(f"  terminal.tradeapi_disabled: {getattr(ti,'tradeapi_disabled',None)}")
        print(f"  account.trade_allowed   : {getattr(ai,'trade_allowed',None)}  (False => read-only/investor login)")
        print(f"  account.trade_expert    : {getattr(ai,'trade_expert',None)}")
        print(f"  account.server          : {getattr(ai,'server',None)}")
        print(f"  account.balance/free    : {getattr(ai,'balance',None)} / {getattr(ai,'margin_free',None)}")

        if not mt5.symbol_select(broker, True):
            print(f"\nsymbol_select({broker}) failed; last_error={read_last_error(mt5)}")
            return 2
        info = mt5.symbol_info(broker)
        tick = mt5.symbol_info_tick(broker)
        volume = float(getattr(info, "volume_min", 0.01) or 0.01)
        price = float(getattr(tick, "ask", 0.0) or 0.0)
        print(f"\nBAR/USD ask={price} volume_min={volume} filling_mode={getattr(info,'filling_mode',None)}")

        base_request = {
            "action": const(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": broker,
            "volume": volume,
            "type": const(mt5, "ORDER_TYPE_BUY", 0),
            "price": price,
            "deviation": 20,
            "magic": int(config.bot_magic),
            "comment": "demo-test-long-bar",
            "type_time": const(mt5, "ORDER_TIME_GTC", 0),
        }

        # Try the broker's supported fill modes until order_check accepts one.
        fill_candidates = [
            ("IOC", const(mt5, "ORDER_FILLING_IOC", 1)),
            ("FOK", const(mt5, "ORDER_FILLING_FOK", 0)),
            ("RETURN", const(mt5, "ORDER_FILLING_RETURN", 2)),
        ]
        chosen = None
        print("\n=== order_check (no order placed yet) ===")
        for label, fill in fill_candidates:
            req = {**base_request, "type_filling": fill}
            check = mt5.order_check(req)
            if check is None:
                print(f"  filling={label}: order_check returned None  last_error={read_last_error(mt5)}")
                continue
            retcode = getattr(check, "retcode", None)
            comment = getattr(check, "comment", None)
            print(f"  filling={label}: retcode={retcode} comment={comment!r}")
            if retcode in DONE_OK:
                chosen = req
                print(f"  -> order_check PASSED with filling={label}")
                break

        if chosen is None:
            print("\nRESULT: order_check never passed. MT5 is NOT accepting trades yet.")
            print("See the diagnostics above (trade_allowed flags) and last_error for the cause.")
            return 1

        print("\n=== order_send (placing ONE live demo order) ===")
        result = mt5.order_send(chosen)
        if result is None:
            print(f"  order_send returned None  last_error={read_last_error(mt5)}")
            return 1
        retcode = getattr(result, "retcode", None)
        print(f"  retcode={retcode} comment={getattr(result,'comment',None)!r}")
        print(f"  order ticket={getattr(result,'order',None)} deal={getattr(result,'deal',None)} "
              f"price={getattr(result,'price',None)} volume={getattr(result,'volume',None)}")
        if retcode in DONE_OK:
            print("\nRESULT: SUCCESS — a long BAR/USD demo position was opened. MetaTrader trading works.")
            print("(Re-run with a close, or close it manually in the terminal.)")
            return 0
        print("\nRESULT: order_send was rejected by the broker (see retcode/comment above).")
        return 1
    finally:
        shutdown_mt5(mt5)


if __name__ == "__main__":
    raise SystemExit(main())
