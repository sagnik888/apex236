"""Upstox API v2 real-time WebSocket market data & forming candle feed.

Connects via Upstox authorization endpoint (`/v2/feed/market-data-feed/authorize`),
subscribes to underlying equities (`NSE_EQ`), indices (`NSE_INDEX`), and option
contracts (`NSE_FO`), and maintains local forming candle snapshots and real-time
option quotes (`ltp`, `greeks`) for instant access by the scanner and options engine.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)
IST_TZ = ZoneInfo("Asia/Kolkata")

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore


class UpstoxFeed:
    """Real-time Upstox WebSocket market data feed with forming candle tracking."""

    def __init__(self) -> None:
        self._ws: Optional[Any] = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._stop_requested = False
        self._subscribed_keys: set[str] = set()
        self._forming_candles: dict[str, dict[str, Any]] = {}
        self._completed_candles: dict[str, list[dict[str, Any]]] = {}
        self._option_quotes: dict[str, dict[str, Any]] = {}
        self._last_packet_time: float = 0.0
        self._thread: Optional[threading.Thread] = None

    def start(self, instrument_keys: list[str]) -> None:
        """Authorize and connect to Upstox WebSocket feed."""
        if not websocket:
            logger.warning("websocket-client not installed; Upstox feed disabled")
            return
        with self._lock:
            for key in instrument_keys:
                if key:
                    self._subscribed_keys.add(key)
            if self._thread and self._thread.is_alive():
                self._resubscribe()
                return
            self._stop_requested = False
            self._thread = threading.Thread(target=self._run_loop, name="upstox-feed", daemon=True)
            self._thread.start()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def seconds_since_last_packet(self) -> Optional[float]:
        if not self._last_packet_time:
            return None
        return time.monotonic() - self._last_packet_time

    def get_forming_candle(self, instrument_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            candle = self._forming_candles.get(instrument_key)
            return dict(candle) if candle else None

    def get_completed_candles(self, instrument_key: str, since: Optional[datetime] = None) -> list[dict[str, Any]]:
        with self._lock:
            candles = list(self._completed_candles.get(instrument_key, []))
        if not since:
            return candles
        out = []
        for c in candles:
            try:
                start_dt = c["start"] if isinstance(c["start"], datetime) else datetime.fromisoformat(str(c["start"]))
                if start_dt > since:
                    out.append(c)
            except Exception:
                continue
        return out

    def get_option_quote(self, instrument_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            q = self._option_quotes.get(instrument_key)
            return dict(q) if q else None

    def _get_auth_url(self) -> Optional[str]:
        from broker_upstox import get_upstox_client, BASE_URL
        client = get_upstox_client()
        client.ensure_session()
        if not client._access_token:
            return None
        url = f"{BASE_URL}/feed/market-data-feed/authorize"
        try:
            r = client._http.get(url, headers=client._headers(), timeout=10)
            if r.ok:
                body = r.json()
                if body.get("status") == "success" and body.get("data", {}).get("authorized_redirect_uri"):
                    return str(body["data"]["authorized_redirect_uri"])
        except Exception as exc:
            logger.debug("Upstox WS auth failed: %s", exc)
        return None

    def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop_requested:
            auth_url = self._get_auth_url()
            if not auth_url:
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)
                continue

            try:
                self._ws = websocket.WebSocketApp(
                    auth_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                logger.debug("Upstox WebSocket error: %s", exc)

            self._connected.clear()
            if not self._stop_requested:
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    def _on_open(self, ws: Any) -> None:
        logger.info("Upstox WebSocket connected")
        self._connected.set()
        self._resubscribe()

    def _resubscribe(self) -> None:
        if not self._ws or not self._connected.is_set():
            return
        with self._lock:
            keys = list(self._subscribed_keys)
        if not keys:
            return
        msg = {
            "guid": str(uuid.uuid4()),
            "method": "sub",
            "data": {
                "instrumentKeys": keys,
                "mode": "full",
            },
        }
        try:
            self._ws.send(json.dumps(msg))
            logger.debug("Subscribed Upstox WS to %s keys", len(keys))
        except Exception as exc:
            logger.warning("Upstox WS subscribe failed: %s", exc)

    def _on_message(self, ws: Any, message: Any) -> None:
        self._last_packet_time = time.monotonic()
        try:
            # Upstox v2 feed returns JSON text or binary protobuf. If text/JSON:
            if isinstance(message, bytes):
                # Attempt decode JSON or log binary
                try:
                    message = message.decode("utf-8")
                except UnicodeDecodeError:
                    return
            data = json.loads(message)
            feeds = data.get("feeds", {}) if isinstance(data, dict) else {}
            for inst_key, feed_item in feeds.items():
                if not isinstance(feed_item, dict):
                    continue
                # Check for full feed or ltp
                ff = feed_item.get("ff", {}) or feed_item.get("ltpc", {})
                if not ff:
                    continue
                ltp = float(ff.get("ltp") or ff.get("lastPrice") or 0.0)
                if ltp <= 0:
                    continue

                # Store option quotes if NSE_FO
                if "NSE_FO" in inst_key or "OPT" in inst_key:
                    with self._lock:
                        self._option_quotes[inst_key] = {
                            "ltp": ltp,
                            "timestamp": datetime.now(IST_TZ),
                            "greeks": ff.get("marketFF", {}).get("optionGreeks", {}),
                        }

                # Update forming candle
                now_ist = datetime.now(IST_TZ)
                bucket_min = (now_ist.minute // 15) * 15
                bucket_start = now_ist.replace(minute=bucket_min, second=0, microsecond=0)

                with self._lock:
                    fc = self._forming_candles.get(inst_key)
                    if not fc or fc["start"] != bucket_start:
                        if fc and fc["start"] < bucket_start:
                            self._completed_candles.setdefault(inst_key, []).append(dict(fc))
                            # Keep last 100 completed candles
                            self._completed_candles[inst_key] = self._completed_candles[inst_key][-100:]
                        self._forming_candles[inst_key] = {
                            "start": bucket_start,
                            "open": ltp,
                            "high": ltp,
                            "low": ltp,
                            "close": ltp,
                            "volume": float(ff.get("v") or ff.get("volume") or 0.0),
                        }
                    else:
                        fc["high"] = max(fc["high"], ltp)
                        fc["low"] = min(fc["low"], ltp)
                        fc["close"] = ltp
                        if ff.get("v") or ff.get("volume"):
                            fc["volume"] = float(ff.get("v") or ff.get("volume") or fc["volume"])
        except Exception:
            pass

    def _on_error(self, ws: Any, error: Any) -> None:
        logger.debug("Upstox WS feed error: %s", error)

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        logger.info("Upstox WS feed closed (%s: %s)", close_status_code, close_msg)
        self._connected.clear()


_feed_lock = threading.Lock()
_feed: Optional[UpstoxFeed] = None


def get_upstox_feed() -> UpstoxFeed:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = UpstoxFeed()
        return _feed
