"""AngelOne SmartAPI client — session, instruments, market data. READ-ONLY.

This module deliberately implements NO order placement/modification/cancel
endpoints. It provides:

  * credential loading (broker_secrets.env / environment variables)
  * a thread-safe session manager with TOTP login and daily token reuse
  * instrument-master download/cache and Yahoo-symbol -> NSE token resolution
  * rate-limited historical candles (getCandleData) with empty-body retry
  * rate-limited batched FULL quotes

All timestamps returned are tz-aware IST.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import socket
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from simulation_engine import is_live_execution

def _get_mac() -> str:
    mac_num = hex(uuid.getnode()).replace('0x', '').upper()
    mac_num = mac_num.zfill(12)
    return ':'.join(mac_num[i: i + 2] for i in range(0, 12, 2))

def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

_LOCAL_IP = _get_local_ip()
_MAC_ADDR = _get_mac()

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
IST_TZ = ZoneInfo("Asia/Kolkata")

BASE = "https://apiconnect.angelone.in"
INSTRUMENTS_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
SESSION_CACHE = HERE / "angel_session.json"
INSTRUMENTS_CACHE = HERE / "angel_instruments.json"

# SmartAPI documented limits: getCandleData 3 req/s, quote 1 req/s burst 10.
# Stay under them; the API also throttles with HTTP 200 + empty body.
_CANDLE_MIN_INTERVAL = 0.45
_QUOTE_MIN_INTERVAL = 1.05

INTERVAL_MAP = {
    "1m": "ONE_MINUTE",
    "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "1d": "ONE_DAY",
}

# Max lookback days per request accepted by SmartAPI per interval.
MAX_DAYS = {
    "ONE_MINUTE": 30,
    "FIVE_MINUTE": 90,
    "FIFTEEN_MINUTE": 180,
    "THIRTY_MINUTE": 180,
    "ONE_HOUR": 365,
    "ONE_DAY": 1900,
}


class AngelCredentialsMissing(RuntimeError):
    pass


def load_credentials() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = HERE / "broker_secrets.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    for key in ("ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_API_KEY", "ANGEL_TOTP_SECRET", "ANGEL_PUBLIC_IP"):
        env.setdefault(key, os.getenv(key, ""))
    required = ("ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_API_KEY", "ANGEL_TOTP_SECRET")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise AngelCredentialsMissing(f"Missing Angel credentials: {missing}")
    return env


def credentials_available() -> bool:
    try:
        load_credentials()
        return True
    except AngelCredentialsMissing:
        return False


class _RateGate:
    """Simple cross-thread minimum-interval gate without holding lock during sleep."""

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last)
            if delay > 0:
                self._last = now + delay
            else:
                self._last = now
                delay = 0.0
        if delay > 0:
            time.sleep(delay)


class AngelClient:
    """Thread-safe read-only SmartAPI client with cached daily session."""

    def __init__(self) -> None:
        self._env = load_credentials()
        self._http = requests.Session()
        self._auth_lock = threading.RLock()
        self._jwt: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._candle_gate = _RateGate(_CANDLE_MIN_INTERVAL)
        self._quote_gate = _RateGate(_QUOTE_MIN_INTERVAL)
        self._token_map: Optional[dict[str, dict]] = None
        self._token_lock = threading.Lock()

    # ── auth ──────────────────────────────────────────────────────────────────

    def _headers(self, with_auth: bool = True) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": _LOCAL_IP,
            "X-ClientPublicIP": self._env.get("ANGEL_PUBLIC_IP") or "106.0.0.1",
            "X-MACAddress": _MAC_ADDR,
            "X-PrivateKey": self._env["ANGEL_API_KEY"],
        }
        if with_auth and self._jwt:
            h["Authorization"] = f"Bearer {self._jwt}"
        return h

    @staticmethod
    def _session_is_todays(saved: dict) -> bool:
        """SmartAPI tokens expire after 6-12 hours; check same IST date and < 6 hours old."""
        try:
            saved_at = datetime.fromisoformat(saved["saved_at"]).astimezone(IST_TZ)
            now_ist = datetime.now(IST_TZ)
            if saved_at.date() != now_ist.date():
                return False
            if now_ist - saved_at >= timedelta(hours=6):
                return False
            return True
        except Exception:
            return False

    def ensure_session(self) -> None:
        with self._auth_lock:
            if self._jwt:
                return
            if SESSION_CACHE.exists():
                try:
                    saved = json.loads(SESSION_CACHE.read_text())
                    if self._session_is_todays(saved) and saved.get("client_id") == self._env["ANGEL_CLIENT_ID"]:
                        self._jwt = saved["jwt"]
                        self._feed_token = saved["feed_token"]
                        if self._probe_session():
                            logger.info("Angel session reused from cache")
                            return
                        self._jwt = self._feed_token = None
                except Exception:
                    self._jwt = self._feed_token = None
            self._login()

    def _probe_session(self) -> bool:
        try:
            r = self._http.get(f"{BASE}/rest/secure/angelbroking/user/v1/getProfile", headers=self._headers(), timeout=10)
            return bool(r.ok and r.json().get("status"))
        except Exception:
            return False

    def _login(self) -> None:
        import pyotp

        for attempt in range(3):
            try:
                totp = pyotp.TOTP(self._env["ANGEL_TOTP_SECRET"]).now()
                r = self._http.post(
                    f"{BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
                    headers=self._headers(with_auth=False),
                    json={
                        "clientcode": self._env["ANGEL_CLIENT_ID"],
                        "password": self._env["ANGEL_PIN"],
                        "totp": totp,
                    },
                    timeout=15,
                )
                body = r.json() if r.text.strip() else {}
                if not (r.ok and body.get("status") and body.get("data")):
                    raise RuntimeError(f"Angel login failed: HTTP {r.status_code} {body.get('message')!r} {body.get('errorcode')!r}")
                self._jwt = body["data"]["jwtToken"]
                self._feed_token = body["data"]["feedToken"]
                try:
                    import tempfile
                    import os
                    fd, tmp_path = tempfile.mkstemp(dir=SESSION_CACHE.parent, prefix="angel_session_tmp_")
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump({
                            "client_id": self._env["ANGEL_CLIENT_ID"],
                            "jwt": self._jwt,
                            "feed_token": self._feed_token,
                            "saved_at": datetime.now(IST_TZ).isoformat(),
                        }, f)
                    os.replace(tmp_path, SESSION_CACHE)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    pass
                logger.info("Angel login OK (fresh session)")
                return
            except Exception as exc:
                if attempt == 2:
                    raise
                logger.warning("Angel login attempt %s failed: %s", attempt + 1, exc)
                time.sleep(1.5 * (attempt + 1))

    def _relogin(self) -> None:
        with self._auth_lock:
            # H2-03: Re-check if session became valid while waiting for lock
            if self._jwt and self._probe_session():
                return
            self._jwt = self._feed_token = None
            self._login()

    @property
    def feed_credentials(self) -> tuple[str, str, str, str]:
        """(jwt, api_key, client_id, feed_token) for WebSocket clients."""
        self.ensure_session()
        if not self._jwt or not self._feed_token:
            raise RuntimeError("Could not establish valid Angel One session for WebSocket feed")
        return self._jwt, self._env["ANGEL_API_KEY"], self._env["ANGEL_CLIENT_ID"], self._feed_token

    # ── instruments ───────────────────────────────────────────────────────────

    def token_map(self) -> dict[str, dict]:
        """Map Yahoo-style symbol (RELIANCE.NS) -> {token, tradingsymbol}."""
        with self._token_lock:
            if self._token_map is not None:
                return self._token_map
            instruments = None
            if INSTRUMENTS_CACHE.exists() and time.time() - INSTRUMENTS_CACHE.stat().st_mtime < 20 * 3600:
                try:
                    instruments = json.loads(INSTRUMENTS_CACHE.read_text(encoding="utf-8"))
                except Exception:
                    instruments = None
            if instruments is None:
                try:
                    r = self._http.get(INSTRUMENTS_URL, timeout=180)
                    r.raise_for_status()
                    instruments = r.json()
                    try:
                        import tempfile
                        fd, tmp_path = tempfile.mkstemp(dir=INSTRUMENTS_CACHE.parent, prefix="angel_instruments_tmp_")
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(instruments, f)
                        os.replace(tmp_path, INSTRUMENTS_CACHE)
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        pass
                except Exception as exc:
                    logger.error("Failed to fetch angel instruments master: %s", exc)
                    return {}
            nse_eq = {}
            for row in instruments:
                exch = row.get("exch_seg")
                sym = str(row.get("symbol", ""))
                if exch in ("NSE", "BSE"):
                    for suffix in ("-EQ", "-BE", "-SM", "-GSM"):
                        if sym.endswith(suffix):
                            nse_eq[sym] = (row, suffix)
                            break
            mapping: dict[str, dict] = {}
            for symbol, (row, suffix) in nse_eq.items():
                base_sym = symbol[:-len(suffix)]
                mapping[f"{base_sym}.NS"] = {"token": str(row["token"]), "tradingsymbol": symbol}
            self._token_map = mapping
            logger.info("Angel instrument map ready: %s NSE/BSE equities", len(mapping))
            return mapping

    def resolve(self, yahoo_symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
        mapping = self.token_map()
        resolved, unresolved = {}, []
        for symbol in yahoo_symbols:
            row = mapping.get(symbol if symbol.endswith(".NS") else f"{symbol}.NS")
            if row:
                resolved[symbol] = row
            else:
                unresolved.append(symbol)
        if unresolved:
            logger.warning("Angel token resolution failed for: %s", unresolved)
        return resolved, unresolved

    # ── market data ───────────────────────────────────────────────────────────

    def _secure_post(self, path: str, payload: dict, timeout: int = 30) -> dict:
        self.ensure_session()
        for attempt in range(3):
            try:
                r = self._http.post(f"{BASE}{path}", headers=self._headers(), json=payload, timeout=timeout)
                if r.status_code == 401 and attempt == 0:
                    logger.info("Angel 401 — re-login and retry")
                    self._relogin()
                    continue
                if not r.text.strip():
                    return {}
                try:
                    return r.json()
                except ValueError:
                    return {}
            except requests.exceptions.RequestException as exc:
                if isinstance(exc, requests.exceptions.ReadTimeout):
                    if "/placeOrder" in path or "/cancelOrder" in path:
                        # C2-03: Do not retry order placement/cancellation on ReadTimeout
                        logger.warning("Order API ReadTimeout on %s, raising to avoid duplicate orders: %s", path, exc)
                        raise
                if attempt == 2:
                    logger.warning("_secure_post network error %s: %s", path, exc)
                    return {}
                time.sleep(1.0 * (attempt + 1))
        return {}

    def get_candles(
        self,
        symbol_token: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
        max_retries: int = 4,
    ) -> Optional[pd.DataFrame]:
        """Historical candles as an IST-indexed OHLCV frame (bar-open labels).

        SmartAPI intermittently throttles with empty bodies; retry with backoff.
        Returns None on persistent failure, empty frame when range has no data.
        """
        api_interval = INTERVAL_MAP.get(interval, interval)
        span_cap = MAX_DAYS.get(api_interval, 90)
        from_dt = max(from_dt, to_dt - timedelta(days=span_cap))
        payload = {
            "exchange": "NSE",
            "symboltoken": str(symbol_token),
            "interval": api_interval,
            "fromdate": from_dt.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M"),
        }
        for attempt in range(max_retries):
            self._candle_gate.wait()
            body = self._secure_post("/rest/secure/angelbroking/historical/v1/getCandleData", payload)
            if body.get("status") and body.get("data") is not None:
                rows = body["data"] or []
                frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
                if frame.empty:
                    return frame
                frame["timestamp"] = pd.to_datetime(frame["timestamp"])
                frame = frame.set_index("timestamp")
                frame.index = (
                    frame.index.tz_convert(IST_TZ) if frame.index.tz is not None else frame.index.tz_localize(IST_TZ)
                )
                return frame.astype(float).sort_index()
            message = str(body.get("message", "empty response"))
            if "rate" in message.lower() or not body:
                time.sleep(0.8 * (attempt + 1))
                continue
            logger.warning("getCandleData error token=%s: %s", symbol_token, message)
            time.sleep(0.8 * (attempt + 1))
        return None

    def get_quotes_full(self, tokens: list[str]) -> dict[str, dict]:
        """Batched FULL quotes keyed by token. Batches of 50 per API contract."""
        out: dict[str, dict] = {}
        for start in range(0, len(tokens), 50):
            batch = [str(t) for t in tokens[start:start + 50]]
            self._quote_gate.wait()
            body = self._secure_post(
                "/rest/secure/angelbroking/market/v1/quote/",
                {"mode": "FULL", "exchangeTokens": {"NSE": batch}},
                timeout=20,
            )
            data = (body.get("data") or {}) if body.get("status") else {}
            for item in data.get("fetched") or []:
                token = str(item.get("symbolToken", ""))
                if token:
                    out[token] = item
            for item in data.get("unfetched") or []:
                logger.debug("quote unfetched: %s", item)
        return out

    # ── order execution (OMS layer) ───────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        trigger_price: float = 0.0,
        variety: str = "NORMAL",
        product_type: str = "INTRADAY",
        tag: str = "",
    ) -> dict[str, Any]:
        """Place an order via SmartAPI v1/placeOrder or return simulated paper fill.

        Enforces idempotency tag and checks is_live_execution().
        """
        idempotency_key = tag or f"APEX-{uuid.uuid4().hex[:8]}"
        if not is_live_execution():
            logger.info("[PAPER MODE] place_order %s %s qty=%s @ %s (tag=%s)", transaction_type, symbol, quantity, price or "MARKET", idempotency_key)
            return {
                "status": True,
                "message": "Paper order placed successfully",
                "data": {"orderid": f"PAPER-{uuid.uuid4().hex[:10]}", "uniqueorderid": idempotency_key},
                "mode": "PAPER",
            }

        resolved, _ = self.resolve([symbol if symbol.endswith(".NS") else f"{symbol}.NS"])
        token_info = resolved.get(symbol if symbol.endswith(".NS") else f"{symbol}.NS")
        if not token_info:
            raise RuntimeError(f"Cannot place live order: symbol token not found for {symbol}")

        payload = {
            "variety": variety,
            "tradingsymbol": token_info["tradingsymbol"],
            "symboltoken": token_info["token"],
            "transactiontype": transaction_type.upper(),
            "exchange": "NSE",
            "ordertype": order_type.upper(),
            "producttype": product_type.upper(),
            "duration": "DAY",
            "quantity": str(quantity),
            "ordertag": idempotency_key,
        }
        if order_type.upper() in ("LIMIT", "STOPLOSS_LIMIT") and price > 0:
            payload["price"] = str(price)
        else:
            payload["price"] = "0"
        if trigger_price > 0:
            payload["triggerprice"] = str(trigger_price)

        res = self._secure_post("/rest/secure/angelbroking/order/v1/placeOrder", payload)
        if not res.get("status"):
            raise RuntimeError(f"Live order failed for {symbol}: {res.get('message', res)}")
        return res

    def place_bracket_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        tag: str = "",
    ) -> dict[str, Any]:
        """Place a bracket / ROBO order complete with stoploss and squareoff offsets."""
        idempotency_key = tag or f"APEX-ROBO-{uuid.uuid4().hex[:8]}"
        if not is_live_execution():
            logger.info("[PAPER MODE] place_bracket_order %s %s qty=%s entry=%s SL=%s TP=%s", transaction_type, symbol, quantity, entry_price, stop_loss, take_profit)
            return {
                "status": True,
                "message": "Paper bracket order placed successfully",
                "data": {"orderid": f"PAPER-ROBO-{uuid.uuid4().hex[:10]}", "uniqueorderid": idempotency_key},
                "mode": "PAPER",
            }

        resolved, _ = self.resolve([symbol if symbol.endswith(".NS") else f"{symbol}.NS"])
        token_info = resolved.get(symbol if symbol.endswith(".NS") else f"{symbol}.NS")
        if not token_info:
            raise RuntimeError(f"Cannot place bracket order: symbol token not found for {symbol}")

        if entry_price > 0:
            stop_dist = abs(entry_price - stop_loss)
            target_dist = abs(take_profit - entry_price)
        else:
            quotes = self.get_quotes_full([token_info["token"]])
            if token_info["token"] in quotes:
                ltp = float(quotes[token_info["token"]].get("lastTradedPrice", 0.0))
                if ltp > 0:
                    stop_dist = abs(ltp - stop_loss)
                    target_dist = abs(take_profit - ltp)
                else:
                    raise RuntimeError(f"Failed to resolve LTP for market bracket order on {symbol}")
            else:
                raise RuntimeError(f"Failed to fetch quote for market bracket order on {symbol}")

        payload = {
            "variety": "ROBO",
            "tradingsymbol": token_info["tradingsymbol"],
            "symboltoken": token_info["token"],
            "transactiontype": transaction_type.upper(),
            "exchange": "NSE",
            "ordertype": "LIMIT" if entry_price > 0 else "MARKET",
            "producttype": "BO",
            "duration": "DAY",
            "quantity": str(quantity),
            "price": str(entry_price) if entry_price > 0 else "0",
            "stoploss": f"{stop_dist:.2f}",
            "squareoff": f"{target_dist:.2f}",
            "ordertag": idempotency_key,
        }
        res = self._secure_post("/rest/secure/angelbroking/order/v1/placeOrder", payload)
        if not res.get("status"):
            raise RuntimeError(f"Live bracket order failed for {symbol}: {res.get('message', res)}")
        return res

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> dict[str, Any]:
        """Cancel an open order by order_id."""
        if not is_live_execution() or order_id.startswith("PAPER"):
            return {"status": True, "message": "Paper order cancelled", "data": {"orderid": order_id}}
        payload = {"variety": variety, "orderid": str(order_id)}
        res = self._secure_post("/rest/secure/angelbroking/order/v1/cancelOrder", payload)
        return res

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Fetch status details of an individual order."""
        if not is_live_execution() or order_id.startswith("PAPER"):
            return {"status": True, "data": {"orderid": order_id, "orderstatus": "complete", "averageprice": "0.0", "filledshares": "0"}}
        body = self._secure_post("/rest/secure/angelbroking/order/v1/details", {"uniqueorderid": str(order_id)})
        if body.get("status") and body.get("data"):
            return body
        # Fall back to order book scan if details endpoint returns empty
        book = self._secure_post("/rest/secure/angelbroking/order/v1/getOrderBook", {})
        for item in (book.get("data") or []):
            if str(item.get("orderid")) == str(order_id) or str(item.get("uniqueorderid")) == str(order_id):
                return {"status": True, "data": item}
        return {"status": False, "message": f"Order status for {order_id} not found"}

    def poll_order_status(self, order_id: str, max_attempts: int = 5, delay_sec: float = 1.0) -> dict[str, Any]:
        """Poll order status with retries until filled, rejected, or timeout."""
        for attempt in range(max_attempts):
            res = self.get_order_status(order_id)
            if res.get("status") and res.get("data"):
                status_str = str(res["data"].get("orderstatus", "")).lower()
                if status_str in ("complete", "rejected", "cancelled"):
                    return res
            time.sleep(delay_sec)
        return self.get_order_status(order_id)


_client_lock = threading.Lock()
_client: Optional[AngelClient] = None


def get_client() -> AngelClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AngelClient()
        return _client
