"""AngelOne SmartAPI WebSocket 2.0 tick feed with 15-minute candle aggregation.

Runs a daemon thread that keeps a mode-2 (QUOTE) subscription for all
configured tokens, and aggregates ticks into NSE-session-aligned 15m candles:

  * forming candle per token (open/high/low/close from ticks,
    volume from day-cumulative volume deltas)
  * a small ring of recently COMPLETED candles per token, so the data
    provider can extend its historical store without REST calls
  * last-tick LTP + age for freshness checks

Read-only market data; no order traffic of any kind.
"""
from __future__ import annotations

import json
import logging
import struct
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST_TZ = ZoneInfo("Asia/Kolkata")
WS_FEED_URL = "wss://smartapisocket.angelone.in/smart-stream"

_SESSION_OPEN = dt_time(9, 15)
_SESSION_CLOSE = dt_time(15, 30)
# Connect a little early / disconnect a little late so boundary candles close.
_FEED_START = dt_time(9, 5)
_FEED_STOP = dt_time(15, 40)

_QUOTE_PACKET_MIN = 123          # mode-2 packet length
_COMPLETED_RING = 40             # completed 15m candles kept per token


def _bucket_start(ts: datetime) -> Optional[datetime]:
    """Floor a timestamp to its NSE 15m candle-open (09:15 anchored)."""
    ts = ts.astimezone(IST_TZ)
    day_open = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    if ts < day_open:
        return None
    minutes = int((ts - day_open).total_seconds() // 60)
    if minutes >= 375:  # after 15:30 → belongs to the final 15:15 bucket
        minutes = 374
    return day_open + timedelta(minutes=(minutes // 15) * 15)


class _Candle:
    __slots__ = ("start", "open", "high", "low", "close", "volume", "day_volume_at_start", "ticks")

    def __init__(self, start: datetime, price: float, day_volume: float):
        self.start = start
        self.open = self.high = self.low = self.close = price
        self.volume = 0.0
        self.day_volume_at_start = day_volume
        self.ticks = 1

    def update(self, price: float, day_volume: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if day_volume >= self.day_volume_at_start > 0:
            self.volume = day_volume - self.day_volume_at_start
        self.ticks += 1

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "ticks": self.ticks,
        }


class AngelTickFeed:
    """Singleton-style tick feed; start once with the full token list."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: list[str] = []
        self._forming: dict[str, _Candle] = {}
        self._completed: dict[str, list[dict]] = {}
        self._ltp: dict[str, tuple[float, datetime]] = {}
        self._day_volume: dict[str, float] = {}
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._last_packet_monotonic: float = 0.0
        self._packets = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, tokens: list[str]) -> None:
        with self._lock:
            self._tokens = [str(t) for t in tokens]
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_loop, name="angel-feed", daemon=True)
            self._thread.start()
            logger.info("Angel tick feed thread started (%s tokens)", len(self._tokens))

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ── status/snapshots ──────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def seconds_since_last_packet(self) -> Optional[float]:
        if self._last_packet_monotonic <= 0:
            return None
        return time.monotonic() - self._last_packet_monotonic

    def stats(self) -> dict:
        with self._lock:
            return {
                "connected": self.is_connected(),
                "tokens": len(self._tokens),
                "packets": self._packets,
                "symbols_with_ticks": len(self._ltp),
                "last_packet_age_s": self.seconds_since_last_packet(),
            }

    def get_ltp(self, token: str) -> Optional[tuple[float, datetime]]:
        with self._lock:
            return self._ltp.get(str(token))

    def get_forming_candle(self, token: str) -> Optional[dict]:
        with self._lock:
            candle = self._forming.get(str(token))
            return candle.as_dict() if candle else None

    def get_completed_candles(self, token: str, since: Optional[datetime] = None) -> list[dict]:
        with self._lock:
            candles = list(self._completed.get(str(token), []))
        if since is not None:
            candles = [c for c in candles if c["start"] > since]
        return candles

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _in_feed_window(now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(IST_TZ)
        if now.weekday() >= 5:
            return False
        t = now.time()
        return _FEED_START <= t <= _FEED_STOP

    def _run_loop(self) -> None:
        import websocket

        backoff = 2.0
        while not self._stop.is_set():
            if not self._in_feed_window():
                self._connected.clear()
                if self._stop.wait(30):
                    break
                continue
            try:
                from broker_angel import get_client
                jwt, api_key, client_id, feed_token = get_client().feed_credentials
            except Exception as exc:
                logger.warning("Angel feed: credentials unavailable: %s", exc)
                if self._stop.wait(60):
                    break
                continue

            def on_open(ws):
                self._connected.set()
                self._last_packet_monotonic = time.monotonic()
                subscription = {
                    "correlationID": "apex_feed",
                    "action": 1,
                    "params": {"mode": 2, "tokenList": [{"exchangeType": 1, "tokens": self._tokens}]},
                }
                ws.send(json.dumps(subscription))
                logger.info("Angel feed connected; subscribed %s tokens (mode 2)", len(self._tokens))

            def on_message(ws, message):
                if isinstance(message, (bytes, bytearray)):
                    self._handle_packet(message)

            def on_error(ws, error):
                logger.warning("Angel feed error: %s", error)

            def on_close(ws, *_args):
                self._connected.clear()

            ws = websocket.WebSocketApp(
                WS_FEED_URL,
                header={
                    "Authorization": f"Bearer {jwt}",
                    "x-api-key": api_key,
                    "x-client-code": client_id,
                    "x-feed-token": str(feed_token),
                },
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self._ws = ws
            try:
                ws.run_forever(ping_interval=25, ping_payload="ping")
            except Exception as exc:
                logger.warning("Angel feed run_forever crashed: %s", exc)
            self._connected.clear()
            self._ws = None
            if self._stop.is_set():
                break
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 1.7, 30.0)
            if self.seconds_since_last_packet() is not None and self.seconds_since_last_packet() < 120:
                backoff = 2.0  # recent data → transient blip, reconnect fast

    def _handle_packet(self, message: bytes) -> None:
        if len(message) < 51:
            return
        token = message[2:27].split(b"\x00")[0].decode(errors="ignore")
        if not token:
            return
        ltp = struct.unpack_from("<q", message, 43)[0] / 100.0
        if ltp <= 0:
            return
        exch_ts_ms = struct.unpack_from("<q", message, 35)[0]
        try:
            tick_time = datetime.fromtimestamp(exch_ts_ms / 1000.0, IST_TZ)
        except (OverflowError, OSError, ValueError):
            tick_time = datetime.now(IST_TZ)
        day_volume = 0.0
        if len(message) >= _QUOTE_PACKET_MIN:
            day_volume = float(struct.unpack_from("<q", message, 67)[0])

        bucket = _bucket_start(tick_time)
        with self._lock:
            self._packets += 1
            self._last_packet_monotonic = time.monotonic()
            self._ltp[token] = (ltp, tick_time)
            if day_volume > 0:
                self._day_volume[token] = day_volume
            if bucket is None:
                return
            candle = self._forming.get(token)
            if candle is None or candle.start != bucket:
                if candle is not None and candle.start < bucket:
                    ring = self._completed.setdefault(token, [])
                    ring.append(candle.as_dict())
                    del ring[:-_COMPLETED_RING]
                self._forming[token] = _Candle(bucket, ltp, day_volume or self._day_volume.get(token, 0.0))
            else:
                candle.update(ltp, day_volume or self._day_volume.get(token, 0.0))


_feed_lock = threading.Lock()
_feed: Optional[AngelTickFeed] = None


def get_feed() -> AngelTickFeed:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = AngelTickFeed()
        return _feed
