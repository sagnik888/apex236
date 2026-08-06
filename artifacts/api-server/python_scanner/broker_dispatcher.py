"""Multi-Broker Load Balancer & Dispatcher (`broker_dispatcher.py`).

Coordinates `broker_angel.py` (Angel One) and `broker_upstox.py` (Upstox API v2) to:
  * Divide and load balance equity historical requests (`split` balance mode)
    to double rate limit bandwidth ($3 + 6 \text{ req/s}$) during scans
  * Provide instant automatic failover between brokers when one hits rate limits
    or experiences network/WebSocket issues
  * Route equity orders cleanly per configuration while routing intraday option
    contracts (`NSE_FO`) strictly to Upstox.
"""
from __future__ import annotations

import logging
import threading
import zlib
from functools import lru_cache
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from broker_angel import get_client as get_angel_client, credentials_available as angel_available
from broker_upstox import (
    get_upstox_client,
    credentials_available as upstox_creds_available,
    session_available as upstox_session_available,
)


def upstox_available() -> bool:
    """Upstox is usable only with a real access token, not merely a config file.

    Gating on credentials_available() alone let the prefetcher assign 118 of the
    236 symbols to a broker with no OAuth token; every fetch for those symbols
    failed and they silently fell back to delayed Yahoo data until the failure
    counter tripped.
    """
    return upstox_creds_available() and upstox_session_available()


@lru_cache(maxsize=1)
def _universe_slots() -> dict[str, int]:
    """Exact 50/50 assignment over the known universe, computed once.

    Index parity over the SORTED universe is both perfectly balanced and stable
    across processes (unlike parity over the caller's list order, which changes
    whenever the caller filters or reorders).
    """
    try:
        from nifty50 import NIFTY236_SYMBOLS
    except Exception:
        return {}
    return {s: i % 2 for i, s in enumerate(sorted(NIFTY236_SYMBOLS))}


def symbol_slot(symbol: str) -> int:
    """Stable 0/1 partition of a symbol across the two brokers.

    Must NOT use the builtin hash(): Python salts str hashing per interpreter
    (PYTHONHASHSEED), so hash(symbol) % 2 is re-tossed on every process start
    and differs between the parent and each ProcessPoolExecutor worker. That
    made the broker a symbol was tried against nondeterministic across restarts
    AND unrelated to the index-parity split prefetch actually used, so the two
    paths disagreed on roughly half the universe.

    Symbols in the known universe get an exact 50/50 assignment; anything else
    falls back to a stable checksum.
    """
    slots = _universe_slots()
    if symbol in slots:
        return slots[symbol]
    bare = symbol.replace(".NS", "")
    if bare in slots:
        return slots[bare]
    return zlib.crc32(symbol.encode("utf-8")) % 2

logger = logging.getLogger(__name__)
IST_TZ = ZoneInfo("Asia/Kolkata")


class MultiBrokerDispatcher:
    """Unified multi-broker load balancing and order execution router."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.balance_mode = "split"  # "split", "angel_primary", "upstox_primary"
        self.equity_broker = "split" # "split", "angel", "upstox"
        self.options_broker = "upstox"
        self._angel_failures = 0
        self._upstox_failures = 0

    def status(self) -> dict[str, Any]:
        """Report the ACTUAL split, measured from the same partition function
        the prefetcher uses.

        This previously hardcoded 118/118 and "50/50 Equal Load Balancing"
        whenever credentials merely parsed, so the dashboard showed a healthy
        dual-broker load at the exact moment split_symbols was routing 236/0 to
        a single broker because the other one's failure counter had tripped.
        """
        try:
            from nifty50 import NIFTY236_SYMBOLS as universe
        except Exception:
            universe = []
        split = self.split_symbols(list(universe))
        angel_count, upstox_count = len(split["angel"]), len(split["upstox"])
        total = angel_count + upstox_count
        if total == 0:
            ratio = "NO BROKER AVAILABLE"
        elif angel_count and upstox_count:
            ratio = f"{round(100 * angel_count / total)}/{round(100 * upstox_count / total)} Angel/Upstox"
        else:
            ratio = "100% Angel One" if angel_count else "100% Upstox"
        return {
            "balance_mode": self.balance_mode,
            "equity_broker": self.equity_broker,
            "options_broker": self.options_broker,
            # Credentials parsing is not health. Report each layer separately so
            # "the config file exists" can never be mistaken for "we are logged in".
            "angel_credentials": angel_available(),
            "upstox_credentials": upstox_creds_available(),
            "upstox_authenticated": upstox_session_available(),
            "angel_available": angel_available() and self._angel_failures < 5,
            "upstox_available": upstox_available() and self._upstox_failures < 5,
            "upstox_status": (
                "OK" if upstox_session_available()
                else "NO_ACCESS_TOKEN - run upstox_login.py to complete the OAuth flow "
                     "(Upstox tokens expire daily ~03:30 IST and cannot self-renew)"
            ),
            "angel_failures": self._angel_failures,
            "upstox_failures": self._upstox_failures,
            "total_symbols_managed": len(universe),
            "angel_assigned_count": angel_count,
            "upstox_assigned_count": upstox_count,
            "split_ratio": ratio,
        }

    def split_symbols(self, symbols: list[str]) -> dict[str, list[str]]:
        """Divide a symbol list across active brokers for load balanced prefetching."""
        angel_ok = angel_available() and self._angel_failures < 5
        upstox_ok = upstox_available() and self._upstox_failures < 5

        if self.balance_mode == "split" and angel_ok and upstox_ok:
            # Partition by the SAME stable function that fetch_ohlcv's failover
            # order uses. Index parity was used here while failover used
            # hash(symbol), so the two disagreed on roughly half the universe:
            # a symbol prefetched into Angel's store would be tried against
            # Upstox first on a live fetch.
            angel_syms = [s for s in symbols if symbol_slot(s) == 0]
            upstox_syms = [s for s in symbols if symbol_slot(s) == 1]
            return {"angel": angel_syms, "upstox": upstox_syms}
        elif upstox_ok and (not angel_ok or self.balance_mode == "upstox_primary"):
            return {"angel": [], "upstox": list(symbols)}
        elif angel_ok:
            return {"angel": list(symbols), "upstox": []}
        else:
            return {"angel": [], "upstox": []}

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
        preferred_broker: Optional[str] = None,
    ) -> tuple[Optional[pd.DataFrame], str]:
        """Fetch historical candles with automatic failover between brokers.

        Returns `(df, source_name)` where source_name is 'angel', 'upstox', or 'none'.
        """
        if to_dt is None:
            to_dt = datetime.now(IST_TZ)
        if from_dt is None:
            if timeframe == "15m":
                from_dt = to_dt - timedelta(days=60)
            elif timeframe in ("1h", "4h"):
                from_dt = to_dt - timedelta(days=365)
            else:
                from_dt = to_dt - timedelta(days=730)

        order = []
        if preferred_broker == "upstox":
            order = ["upstox", "angel"]
        elif preferred_broker == "angel":
            order = ["angel", "upstox"]
        elif self.balance_mode == "upstox_primary":
            order = ["upstox", "angel"]
        else:
            # Default or split mode uses symbol hash or failure counts
            if symbol_slot(symbol) == 0:
                order = ["angel", "upstox"]
            else:
                order = ["upstox", "angel"]

        for b in order:
            if b == "upstox" and upstox_available():
                try:
                    client = get_upstox_client()
                    token_info = client.resolve_symbol(symbol)
                    inst_key = token_info.get("instrument_key") if token_info else symbol
                    if inst_key:
                        df = client.get_candles(inst_key, timeframe, from_dt, to_dt)
                        if df is not None and not df.empty:
                            with self._lock:
                                self._upstox_failures = max(0, self._upstox_failures - 1)
                            return df, "upstox"
                        else:
                            with self._lock:
                                self._upstox_failures += 1
                except Exception as exc:
                    logger.debug("Upstox fetch_ohlcv failover for %s: %s", symbol, exc)
                    with self._lock:
                        self._upstox_failures += 1

            elif b == "angel" and angel_available():
                try:
                    client = get_angel_client()
                    resolved, _ = client.resolve([symbol if symbol.endswith(".NS") else f"{symbol}.NS"])
                    row = resolved.get(symbol if symbol.endswith(".NS") else f"{symbol}.NS")
                    if row and row.get("token"):
                        df = client.get_candles(row["token"], timeframe, from_dt, to_dt)
                        if df is not None and not df.empty:
                            with self._lock:
                                self._angel_failures = max(0, self._angel_failures - 1)
                            return df, "angel"
                        else:
                            with self._lock:
                                self._angel_failures += 1
                except Exception as exc:
                    logger.debug("Angel fetch_ohlcv failover for %s: %s", symbol, exc)
                    with self._lock:
                        self._angel_failures += 1

        return None, "none"

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "I",
        tag: str = "",
        broker: Optional[str] = None,
    ) -> dict[str, Any]:
        """Route order to appropriate broker (equities balanced/preferred, options -> Upstox)."""
        target = broker or self._resolve_target_broker(symbol)
        if target == "upstox":
            client = get_upstox_client()
            return client.place_order(symbol, transaction_type, quantity, order_type, price, trigger_price, product, tag)
        else:
            client = get_angel_client()
            # Call by keyword: Angel's signature has variety/product_type BEFORE
            # tag, so passing tag positionally (the old bug) bound it to variety
            # and corrupted the order while losing the idempotency tag.
            return client.place_order(
                symbol, transaction_type, quantity,
                order_type=order_type, price=price, trigger_price=trigger_price,
                product_type=("INTRADAY" if str(product).upper() in ("I", "MIS", "INTRADAY") else "DELIVERY"),
                tag=tag,
            )

    def place_bracket_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        tag: str = "",
        broker: Optional[str] = None,
    ) -> dict[str, Any]:
        target = broker or self._resolve_target_broker(symbol)
        if target == "upstox":
            client = get_upstox_client()
            return client.place_bracket_order(symbol, transaction_type, quantity, entry_price, stop_loss, take_profit, tag)
        else:
            client = get_angel_client()
            return client.place_bracket_order(symbol, transaction_type, quantity, entry_price, stop_loss, take_profit, tag)

    def cancel_order(self, order_id: str, broker: str = "upstox") -> dict[str, Any]:
        if broker == "upstox" or "UPSTOX" in str(order_id):
            return get_upstox_client().cancel_order(order_id)
        return get_angel_client().cancel_order(order_id)

    def get_order_status(self, order_id: str, broker: str = "upstox") -> dict[str, Any]:
        if broker == "upstox" or "UPSTOX" in str(order_id):
            return get_upstox_client().get_order_status(order_id)
        return get_angel_client().get_order_status(order_id)

    def _resolve_target_broker(self, symbol: str) -> str:
        # Options / NSE_FO / CE / PE contracts go strictly to options_broker (default Upstox)
        if "NSE_FO" in symbol or symbol.endswith("CE") or symbol.endswith("PE") or "OPT" in symbol:
            return self.options_broker
        if self.equity_broker in ("angel", "upstox"):
            return self.equity_broker
        if symbol_slot(symbol) == 0 and angel_available():
            return "angel"
        return "upstox" if upstox_available() else "angel"


_dispatcher_lock = threading.Lock()
_dispatcher: Optional[MultiBrokerDispatcher] = None


def get_dispatcher() -> MultiBrokerDispatcher:
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = MultiBrokerDispatcher()
        return _dispatcher
