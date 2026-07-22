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
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from broker_angel import get_client as get_angel_client, credentials_available as angel_available
from broker_upstox import get_upstox_client, credentials_available as upstox_available

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
        angel_ok = angel_available()
        upstox_ok = upstox_available()
        # In split mode across our 236 Nifty universe:
        angel_count = 118 if (angel_ok and upstox_ok and self.balance_mode == "split") else (236 if angel_ok else 0)
        upstox_count = 118 if (angel_ok and upstox_ok and self.balance_mode == "split") else (236 if upstox_ok else 0)
        return {
            "balance_mode": self.balance_mode,
            "equity_broker": self.equity_broker,
            "options_broker": self.options_broker,
            "angel_available": angel_ok,
            "upstox_available": upstox_ok,
            "angel_failures": self._angel_failures,
            "upstox_failures": self._upstox_failures,
            "total_symbols_managed": 236,
            "angel_assigned_count": angel_count,
            "upstox_assigned_count": upstox_count,
            "split_ratio": "50/50 Equal Load Balancing" if (angel_count == 118 and upstox_count == 118) else ("100% Upstox" if upstox_count == 236 else "100% Angel One"),
        }

    def split_symbols(self, symbols: list[str]) -> dict[str, list[str]]:
        """Divide a symbol list across active brokers for load balanced prefetching."""
        angel_ok = angel_available() and self._angel_failures < 5
        upstox_ok = upstox_available() and self._upstox_failures < 5

        if self.balance_mode == "split" and angel_ok and upstox_ok:
            # 50/50 split based on index
            angel_syms = [s for i, s in enumerate(symbols) if i % 2 == 0]
            upstox_syms = [s for i, s in enumerate(symbols) if i % 2 == 1]
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
            if hash(symbol) % 2 == 0:
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
            return client.place_order(symbol, transaction_type, quantity, order_type, price, trigger_price, tag)

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
        if hash(symbol) % 2 == 0 and angel_available():
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
