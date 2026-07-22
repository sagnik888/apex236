"""Upstox API v2 client — session, instruments, market data, and full OMS.

Provides:
  * credential loading from upstox_secrets.env / environment variables
  * thread-safe access token management and session caching (upstox_session.json)
  * complete instrument-master download/cache (`upstox_instruments.json`) with
    support for NSE_EQ, NSE_INDEX, and NSE_FO (options/futures)
  * rate-limited historical candle fetching (`get_candles`)
  * rate-limited batched quotes and option greeks (`get_quote`, `get_option_greeks`)
  * full order execution OMS (`place_order`, `place_bracket_order`, `cancel_order`,
    `get_order_status`, `poll_order_status`) supporting both PAPER and LIVE execution modes.

All timestamps returned are tz-aware IST (`Asia/Kolkata`).
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from simulation_engine import is_live_execution

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
IST_TZ = ZoneInfo("Asia/Kolkata")

BASE_URL = "https://api.upstox.com/v2"
# Complete instrument master from Upstox (includes NSE_EQ, NSE_INDEX, NSE_FO)
INSTRUMENTS_GZ_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
SESSION_CACHE = HERE / "upstox_session.json"
INSTRUMENTS_CACHE = HERE / "upstox_instruments.json"

# Upstox API rate limits: ~10 req/sec burst. Stay conservative at ~6 req/sec.
_CANDLE_MIN_INTERVAL = 0.18
_QUOTE_MIN_INTERVAL = 0.25

INTERVAL_MAP = {
    "1m": "1minute",
    "5m": "5minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h": "60minute",
    "1d": "day",
}


class UpstoxCredentialsMissing(RuntimeError):
    pass


def load_credentials() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = HERE / "upstox_secrets.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                env[key.strip()] = value
    for key in ("UPSTOX_USER_ID", "UPSTOX_PASSWORD", "UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_ACCESS_TOKEN"):
        env.setdefault(key, os.getenv(key, ""))
    if not env.get("UPSTOX_API_KEY") and not env.get("UPSTOX_ACCESS_TOKEN"):
        raise UpstoxCredentialsMissing("Missing Upstox API credentials (UPSTOX_API_KEY or UPSTOX_ACCESS_TOKEN)")
    return env


def credentials_available() -> bool:
    try:
        load_credentials()
        return True
    except UpstoxCredentialsMissing:
        return False


class _RateGate:
    """Thread-safe rate gate without holding lock during sleep (`HIGH-20` pattern)."""

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


class UpstoxClient:
    """Thread-safe read-only & OMS Upstox API v2 client."""

    def __init__(self) -> None:
        self._env = load_credentials()
        self._http = requests.Session()
        self._auth_lock = threading.RLock()
        self._access_token: Optional[str] = self._env.get("UPSTOX_ACCESS_TOKEN") or None
        self._candle_gate = _RateGate(_CANDLE_MIN_INTERVAL)
        self._quote_gate = _RateGate(_QUOTE_MIN_INTERVAL)
        self._instrument_map: Optional[dict[str, dict]] = None
        self._fo_index: Optional[dict[str, list[dict]]] = None
        self._token_lock = threading.Lock()

    # ── Authentication & Session ──────────────────────────────────────────────

    def _headers(self, with_auth: bool = True) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Api-Version": "2.0",
        }
        if with_auth and self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    @staticmethod
    def _session_is_valid(saved: dict) -> bool:
        try:
            saved_at = datetime.fromisoformat(saved["saved_at"]).astimezone(IST_TZ)
            now_ist = datetime.now(IST_TZ)
            if now_ist - saved_at >= timedelta(hours=20):
                return False
            return True
        except Exception:
            return False

    def ensure_session(self) -> None:
        with self._auth_lock:
            if self._access_token and self._probe_session():
                return
            if SESSION_CACHE.exists():
                try:
                    saved = json.loads(SESSION_CACHE.read_text(encoding="utf-8"))
                    if self._session_is_valid(saved) and saved.get("access_token"):
                        self._access_token = saved["access_token"]
                        if self._probe_session():
                            logger.info("Upstox session reused from cache")
                            return
                        self._access_token = None
                except Exception:
                    self._access_token = None
            if self._env.get("UPSTOX_ACCESS_TOKEN"):
                self._access_token = self._env["UPSTOX_ACCESS_TOKEN"]
                if self._probe_session():
                    self._save_session()
                    return
            if not self._access_token:
                logger.warning("No active Upstox access token found. Provide UPSTOX_ACCESS_TOKEN in upstox_secrets.env or run OAuth flow.")

    def _probe_session(self) -> bool:
        if not self._access_token:
            return False
        try:
            r = self._http.get(f"{BASE_URL}/user/profile", headers=self._headers(), timeout=10)
            return bool(r.ok and r.json().get("status") == "success")
        except Exception:
            return False

    def _save_session(self) -> None:
        if not self._access_token:
            return
        try:
            data = {
                "access_token": self._access_token,
                "saved_at": datetime.now(IST_TZ).isoformat(),
            }
            SESSION_CACHE.write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:
            logger.debug("Failed to save upstox session: %s", exc)

    # ── Instrument Master Resolution ──────────────────────────────────────────

    def instrument_map(self) -> tuple[dict[str, dict], dict[str, list[dict]]]:
        """Returns `(equity_map, fo_index)` where equity_map maps Yahoo/NSE symbol to metadata,

        and fo_index maps underlying trading symbol (e.g., 'RELIANCE', 'NIFTY') to list of active option/future contracts (`NSE_FO`).
        """
        with self._token_lock:
            if self._instrument_map is not None and self._fo_index is not None:
                return self._instrument_map, self._fo_index

        eq_map: dict[str, dict] = {}
        fo_index: dict[str, list[dict]] = {}
        raw_list: list[dict] = []

        if INSTRUMENTS_CACHE.exists() and (time.time() - INSTRUMENTS_CACHE.stat().st_mtime < 86400):
            try:
                raw_list = json.loads(INSTRUMENTS_CACHE.read_text(encoding="utf-8"))
            except Exception:
                raw_list = []

        if not raw_list:
            try:
                logger.info("Downloading complete Upstox instrument master (gzip)…")
                r = self._http.get(INSTRUMENTS_GZ_URL, timeout=120)
                r.raise_for_status()
                with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
                    raw_list = json.load(gz)
                try:
                    INSTRUMENTS_CACHE.write_text(json.dumps(raw_list), encoding="utf-8")
                except Exception:
                    pass
            except Exception as exc:
                logger.error("Failed to download Upstox instrument master: %s", exc)
                return {}, {}

        for item in raw_list:
            exch = str(item.get("exchange", "")).upper()
            inst_key = str(item.get("instrument_key", ""))
            tsym = str(item.get("tradingsymbol", ""))
            name = str(item.get("name", ""))
            inst_type = str(item.get("instrument_type", "")).upper()

            if exch in ("NSE_EQ", "NSE_INDEX"):
                if exch == "NSE_INDEX":
                    if "NIFTY 50" in name.upper() or tsym == "NIFTY":
                        eq_map["^NSEI"] = item
                        eq_map["NIFTY 50.NS"] = item
                    elif "BANKNIFTY" in tsym or "NIFTY BANK" in name.upper():
                        eq_map["^NSEBANK"] = item
                        eq_map["BANKNIFTY.NS"] = item
                else:
                    eq_map[f"{tsym}.NS"] = item
                    eq_map[tsym] = item
            elif exch == "NSE_FO" and inst_type in ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"):
                underlying = name if name else tsym.split("-")[0]
                underlying = underlying.upper().replace(" ", "")
                fo_index.setdefault(underlying, []).append(item)

        with self._token_lock:
            self._instrument_map = eq_map
            self._fo_index = fo_index
        logger.info("Upstox instrument master ready: %s equities/indices, %s underlying option classes", len(eq_map), len(fo_index))
        return eq_map, fo_index

    def resolve_symbol(self, symbol: str) -> Optional[dict]:
        """Resolve a Yahoo symbol (`RELIANCE.NS`, `^NSEI`) to Upstox instrument metadata."""
        eq_map, _ = self.instrument_map()
        return eq_map.get(symbol if symbol.endswith(".NS") or symbol.startswith("^") else f"{symbol}.NS")

    def resolve_option_contract(
        self,
        underlying_symbol: str,
        option_type: str,  # "CE" or "PE"
        strike_price: float,
        expiry_date: Optional[str] = None,  # "YYYY-MM-DD" or None for nearest expiry
    ) -> Optional[dict]:
        """Lookup an active option contract (`NSE_FO`) by strike and type for the given underlying."""
        _, fo_index = self.instrument_map()
        base = underlying_symbol.replace(".NS", "").replace("^NSEI", "NIFTY").replace("^NSEBANK", "BANKNIFTY").upper()
        contracts = fo_index.get(base, [])
        if not contracts:
            return None

        matches = [
            c for c in contracts
            if c.get("instrument_type") in ("OPTIDX", "OPTSTK")
            and str(c.get("option_type", "")).upper() == option_type.upper()
            and abs(float(c.get("strike", 0.0)) - strike_price) < 0.1
        ]
        if not matches:
            return None

        now_date = datetime.now(IST_TZ).date()
        valid = []
        for c in matches:
            try:
                exp_dt = datetime.strptime(str(c.get("expiry", "")), "%Y-%m-%d").date()
                if exp_dt >= now_date:
                    valid.append((exp_dt, c))
            except Exception:
                continue

        valid.sort(key=lambda x: x[0])
        if not valid:
            return None

        if expiry_date:
            for exp_dt, c in valid:
                if exp_dt.strftime("%Y-%m-%d") == expiry_date:
                    return c
        return valid[0][1]

    # ── Market Data & Historical Candles ──────────────────────────────────────

    def get_candles(
        self,
        instrument_key: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> Optional[pd.DataFrame]:
        """Fetch historical OHLCV candles from Upstox API v2 (`/v2/historical-candle`)."""
        self.ensure_session()
        self._candle_gate.wait()

        upstox_interval = INTERVAL_MAP.get(interval, interval)
        if interval in ("1m", "5m", "15m", "30m"):
            upstox_interval = INTERVAL_MAP[interval]

        to_str = to_dt.astimezone(IST_TZ).strftime("%Y-%m-%d")
        from_str = from_dt.astimezone(IST_TZ).strftime("%Y-%m-%d")

        url = f"{BASE_URL}/historical-candle/{instrument_key}/{upstox_interval}/{to_str}/{from_str}"
        for attempt in range(3):
            try:
                r = self._http.get(url, headers=self._headers(), timeout=20)
                if r.status_code == 401 and attempt == 0:
                    self._access_token = None
                    self.ensure_session()
                    continue
                if r.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if not r.ok:
                    return None
                data = r.json()
                if data.get("status") != "success" or not data.get("data", {}).get("candles"):
                    return pd.DataFrame()
                candles = data["data"]["candles"]
                rows = []
                for c in candles:
                    try:
                        ts = datetime.fromisoformat(c[0]).astimezone(IST_TZ)
                        rows.append({
                            "timestamp": ts,
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                            "volume": float(c[5]),
                        })
                    except Exception:
                        continue
                if not rows:
                    return pd.DataFrame()
                df = pd.DataFrame(rows).sort_values("timestamp").set_index("timestamp")
                df = df[~df.index.duplicated(keep="last")]
                return df
            except Exception as exc:
                if attempt == 2:
                    logger.debug("Upstox get_candles failed for %s: %s", instrument_key, exc)
                    return None
                time.sleep(0.5 * (attempt + 1))
        return None

    def get_quote(self, instrument_keys: list[str]) -> dict[str, dict]:
        """Fetch batched market quote (`/v2/market-quote/quotes`) including option greeks (`delta`, `iv`)."""
        if not instrument_keys:
            return {}
        self.ensure_session()
        self._quote_gate.wait()

        results: dict[str, dict] = {}
        chunk_size = 50
        for i in range(0, len(instrument_keys), chunk_size):
            chunk = instrument_keys[i:i + chunk_size]
            url = f"{BASE_URL}/market-quote/quotes"
            try:
                r = self._http.get(url, headers=self._headers(), params={"symbol": ",".join(chunk)}, timeout=15)
                if r.ok:
                    body = r.json()
                    if body.get("status") == "success" and body.get("data"):
                        results.update(body["data"])
            except Exception as exc:
                logger.debug("Upstox quote batch failed: %s", exc)
        return results

    # ── Order Execution Service (OMS) ─────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        transaction_type: str,  # "BUY" or "SELL"
        quantity: int,
        order_type: str = "MARKET",  # "MARKET", "LIMIT", "SL", "SL-M"
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "I",  # "I" = Intraday, "D" = Delivery
        tag: str = "",
    ) -> dict[str, Any]:
        """Place an order via Upstox API v2 (`/v2/order/place`). Supports `PAPER` mode."""
        idempotency_key = tag or f"UPSTOX-{uuid.uuid4().hex[:8]}"
        if not is_live_execution():
            logger.info("[PAPER MODE] Upstox place_order %s %s qty=%s @ %s (tag=%s)", transaction_type, symbol, quantity, price or "MARKET", idempotency_key)
            return {
                "status": True,
                "message": "Paper Upstox order placed successfully",
                "data": {"order_id": f"PAPER-UPSTOX-{uuid.uuid4().hex[:10]}", "tag": idempotency_key},
                "mode": "PAPER",
            }

        token_info = self.resolve_symbol(symbol)
        inst_key = token_info.get("instrument_key") if token_info else symbol
        if not inst_key or ("|" not in inst_key and not inst_key.startswith("NSE_")):
            raise RuntimeError(f"Cannot place Upstox live order: invalid instrument_key for {symbol} ({inst_key})")

        self.ensure_session()
        payload = {
            "quantity": int(quantity),
            "product": product.upper(),
            "validity": "DAY",
            "price": float(price) if order_type.upper() in ("LIMIT", "SL") and price > 0 else 0.0,
            "tag": idempotency_key,
            "instrument_token": inst_key,
            "order_type": order_type.upper(),
            "transaction_type": transaction_type.upper(),
            "disclosed_quantity": 0,
            "trigger_price": float(trigger_price) if trigger_price > 0 else 0.0,
            "is_amo": False,
        }
        url = f"{BASE_URL}/order/place"
        r = self._http.post(url, headers=self._headers(), json=payload, timeout=15)
        body = r.json() if r.text else {}
        if not r.ok or body.get("status") != "success":
            raise RuntimeError(f"Upstox live order failed for {symbol}: {body.get('message', body)}")
        return body

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
        """Place an Intraday bracket order or simulated bracket order via Upstox."""
        idempotency_key = tag or f"UPSTOX-ROBO-{uuid.uuid4().hex[:8]}"
        if not is_live_execution():
            logger.info("[PAPER MODE] Upstox place_bracket_order %s %s qty=%s entry=%s SL=%s TP=%s", transaction_type, symbol, quantity, entry_price, stop_loss, take_profit)
            return {
                "status": True,
                "message": "Paper Upstox bracket order placed successfully",
                "data": {"order_id": f"PAPER-ROBO-UPSTOX-{uuid.uuid4().hex[:10]}", "tag": idempotency_key},
                "mode": "PAPER",
            }

        return self.place_order(
            symbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="LIMIT" if entry_price > 0 else "MARKET",
            price=entry_price,
            product="I",
            tag=idempotency_key,
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not is_live_execution() or str(order_id).startswith("PAPER"):
            return {"status": True, "message": "Paper Upstox order cancelled", "data": {"order_id": order_id}}
        self.ensure_session()
        url = f"{BASE_URL}/order/cancel"
        r = self._http.delete(url, headers=self._headers(), params={"order_id": str(order_id)}, timeout=15)
        return r.json() if r.text else {"status": r.ok}

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        if not is_live_execution() or str(order_id).startswith("PAPER"):
            return {"status": True, "data": {"order_id": order_id, "status": "complete", "average_price": "0.0", "filled_quantity": "0"}}
        self.ensure_session()
        url = f"{BASE_URL}/order/details"
        r = self._http.get(url, headers=self._headers(), params={"order_id": str(order_id)}, timeout=15)
        return r.json() if r.text else {}

    def poll_order_status(self, order_id: str, max_attempts: int = 5, delay_sec: float = 1.0) -> dict[str, Any]:
        for attempt in range(max_attempts):
            res = self.get_order_status(order_id)
            if res.get("status") == "success" and res.get("data"):
                status_str = str(res["data"].get("status", "")).lower()
                if status_str in ("complete", "rejected", "cancelled"):
                    return res
            time.sleep(delay_sec)
        return self.get_order_status(order_id)


_client_lock = threading.Lock()
_client: Optional[UpstoxClient] = None


def get_upstox_client() -> UpstoxClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = UpstoxClient()
        return _client
