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
from datetime import date, datetime, timedelta
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


def session_available() -> bool:
    """True when a usable Upstox ACCESS TOKEN exists locally. No network call.

    credentials_available() only proves upstox_secrets.env parses and carries an
    API key — it is satisfied by a file that has never been through OAuth. That
    made the dispatcher report a healthy 50/50 dual-broker split while Upstox
    could not make a single authenticated call, and made split_symbols hand 118
    symbols to a broker guaranteed to fail on every one of them.

    Unlike Angel (which holds a TOTP secret and can re-mint its own session),
    Upstox uses OAuth: the token expires daily at ~03:30 IST and can only be
    renewed by a human completing the browser authorization flow.
    """
    # Delegate to upstox_auth when available so the dispatcher, the dashboard
    # and the login script all agree about one session, including its expiry.
    try:
        from upstox_auth import get_upstox_auth
        st = get_upstox_auth().status()
        return bool(st.get("connected"))
    except Exception:
        pass
    try:
        env = load_credentials()
    except UpstoxCredentialsMissing:
        return False
    if env.get("UPSTOX_ACCESS_TOKEN"):
        return True
    if SESSION_CACHE.exists():
        try:
            saved = json.loads(SESSION_CACHE.read_text(encoding="utf-8"))
            return bool(saved.get("access_token")) and UpstoxClient._session_is_valid(saved)
        except Exception:
            return False
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


def _parse_expiry(value) -> Optional[date]:
    """Parse an Upstox `expiry` field to a date.

    The real instrument master stores expiry as epoch MILLISECONDS
    (e.g. 1793125799000), not "YYYY-MM-DD". The previous code only tried
    strptime("%Y-%m-%d"), so every contract raised, was swallowed by a bare
    `except: continue`, and the candidate list came out empty — meaning
    resolve_option_contract returned None even once the strike matched.
    Both shapes are accepted here so a schema change cannot silently disarm it.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        raw = float(value)
        # Heuristic: anything past ~year 2286 in seconds must be milliseconds.
        if raw > 1e11:
            raw /= 1000.0
        try:
            return datetime.fromtimestamp(raw, IST_TZ).date()
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    return None


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
        from upstox_auth import get_upstox_auth
        with self._auth_lock:
            auth = get_upstox_auth()
            # If upstox_auth says we're connected, adopt its token.
            if auth.status()["connected"]:
                self._access_token = auth.access_token
                return
            # If not, clear our own copy.
            self._access_token = None
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
            import tempfile
            import os
            fd, tmp_path = tempfile.mkstemp(dir=SESSION_CACHE.parent, prefix="upstox_session_tmp_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_path, SESSION_CACHE)
        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
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
                        import tempfile
                        fd, tmp_path = tempfile.mkstemp(dir=INSTRUMENTS_CACHE.parent, prefix="upstox_instruments_tmp_")
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(raw_list, f)
                        os.replace(tmp_path, INSTRUMENTS_CACHE)
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        pass
                except Exception as exc:
                    logger.error("Failed to download Upstox instrument master: %s", exc)
                    return {}, {}

            # Field names below match Upstox's real complete.json.gz. The
            # previous parser read `exchange` expecting "NSE_EQ"/"NSE_FO" (the
            # file carries exchange="NSE" with the venue in `segment`),
            # `instrument_type` expecting "OPTSTK"/"OPTIDX" (the file carries
            # "CE"/"PE"), and `tradingsymbol`/`strike` (the file carries
            # `trading_symbol`/`strike_price`). Every lookup therefore missed,
            # eq_map and fo_index were ALWAYS empty, every Upstox equity fetch
            # failed, _upstox_failures passed its cutoff, and split_symbols
            # silently routed all 236 symbols to Angel — so the advertised
            # 50/50 dual-broker load was a single-broker load, and every option
            # resolution returned None.
            for item in raw_list:
                segment = str(item.get("segment", "")).upper()
                tsym = str(item.get("trading_symbol", "")).upper()
                name = str(item.get("name", ""))
                inst_type = str(item.get("instrument_type", "")).upper()

                if segment == "NSE_INDEX":
                    # Exact names only. `"NIFTY 50" in name` is a substring test
                    # that also matches "Nifty 500", which resolved ^NSEI to the
                    # wrong index entirely.
                    upper_name = name.upper().strip()
                    if upper_name == "NIFTY 50" or tsym == "NIFTY":
                        eq_map["^NSEI"] = item
                        eq_map["NIFTY 50.NS"] = item
                    elif upper_name == "NIFTY BANK" or tsym == "BANKNIFTY":
                        eq_map["^NSEBANK"] = item
                        eq_map["BANKNIFTY.NS"] = item
                elif segment == "NSE_EQ" and inst_type == "EQ":
                    eq_map[f"{tsym}.NS"] = item
                    eq_map[tsym] = item
                elif segment == "NSE_FO" and inst_type in ("CE", "PE", "FUT"):
                    # `underlying_symbol` / `asset_symbol` give the real
                    # underlying directly, so the option class no longer has to
                    # be guessed from a trading-symbol prefix.
                    underlying = (
                        str(item.get("underlying_symbol") or item.get("asset_symbol") or name or tsym.split(" ")[0])
                        .upper().replace(" ", "")
                    )
                    fo_index.setdefault(underlying, []).append(item)

            if not eq_map or not fo_index:
                # Fail loudly. Silently empty maps are what let a broken parser
                # masquerade as a healthy dual-broker setup for weeks.
                logger.error(
                    "Upstox instrument master parsed to %d equities and %d option classes from %d records "
                    "— the master schema has probably changed; Upstox routing is disabled.",
                    len(eq_map), len(fo_index), len(raw_list),
                )

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
        min_days_to_expiry: int = 0,  # prefer the nearest expiry at least this far out
    ) -> Optional[dict]:
        """Lookup an active option contract (`NSE_FO`) by strike and type for the given underlying."""
        _, fo_index = self.instrument_map()
        base = underlying_symbol.replace(".NS", "").replace("^NSEI", "NIFTY").replace("^NSEBANK", "BANKNIFTY").replace("NIFTY 50", "NIFTY").upper()
        # Alias mapping for symbols whose scanner universe name differs from
        # the NSE F&O underlying ticker used in Upstox instrument master.
        _FO_ALIASES = {"LTM": "LTIM", "PIRAMALFIN": "PEL", "ETERNAL": "ZOMATO"}
        base = _FO_ALIASES.get(base, base)
        contracts = fo_index.get(base, [])
        if not contracts:
            return None

        # In the real master the option type IS instrument_type ("CE"/"PE");
        # there is no separate `option_type` field, and the strike lives in
        # `strike_price`. Reading the old names matched nothing.
        matches = [
            c for c in contracts
            if str(c.get("instrument_type", "")).upper() == option_type.upper()
            and abs(float(c.get("strike_price", 0.0) or 0.0) - strike_price) < 0.1
        ]
        if not matches:
            return None

        now_date = datetime.now(IST_TZ).date()
        valid = []
        for c in matches:
            exp_dt = _parse_expiry(c.get("expiry"))
            if exp_dt is not None and exp_dt >= now_date:
                valid.append((exp_dt, c))

        valid.sort(key=lambda x: x[0])
        if not valid:
            return None

        if expiry_date:
            for exp_dt, c in valid:
                if exp_dt.strftime("%Y-%m-%d") == expiry_date:
                    return c
        # Prefer the nearest expiry at least `min_days_to_expiry` out so that
        # multi-day holds are not put on the highest-theta soon-to-expire ATM.
        if min_days_to_expiry > 0:
            for exp_dt, c in valid:
                if (exp_dt - now_date).days >= min_days_to_expiry:
                    return c
            return valid[-1][1]  # none far enough — take the farthest available
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

        api_interval = interval
        needs_resample = False
        if interval in ("5m", "15m", "1h"):
            api_interval = "1m"
            needs_resample = True
            # 1minute data can only be fetched for max 30 days at once
            if (to_dt - from_dt).days > 30:
                from_dt = max(from_dt, to_dt - timedelta(days=30))

        upstox_interval = INTERVAL_MAP.get(api_interval, api_interval)
        if api_interval in ("1m", "5m", "15m", "30m"):
            upstox_interval = INTERVAL_MAP[api_interval]

        to_str = to_dt.astimezone(IST_TZ).strftime("%Y-%m-%d")
        from_str = from_dt.astimezone(IST_TZ).strftime("%Y-%m-%d")

        url = f"{BASE_URL}/historical-candle/{instrument_key}/{upstox_interval}/{to_str}/{from_str}"
        for attempt in range(3):
            try:
                r = self._http.get(url, headers=self._headers(), timeout=20)
                if r.status_code == 401 and attempt == 0:
                    self._access_token = None
                    from upstox_auth import get_upstox_auth
                    get_upstox_auth().invalidate(f"API returned 401 Unauthorized for get_quote")
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
                        if ts >= from_dt and ts <= to_dt:
                            rows.append({
                                "timestamp": ts,
                                "open": float(c[1]),
                                "high": float(c[2]),
                                "low": float(c[3]),
                                "close": float(c[4]),
                                "volume": int(c[5]),
                            })
                    except Exception:
                        continue
                if not rows:
                    return pd.DataFrame()
                df = pd.DataFrame(rows).sort_values("timestamp").set_index("timestamp")
                df = df[~df.index.duplicated(keep="last")]
                
                if needs_resample:
                    rule = "5min" if interval == "5m" else "15min" if interval == "15m" else "60min"
                    df = df.resample(rule, closed="left", label="left").agg({
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum"
                    }).dropna()
                    
                return df
            except Exception as exc:
                if attempt == 2:
                    logger.debug("Upstox get_candles failed for %s: %s", instrument_key, exc)
                    return None
                time.sleep(0.5 * (attempt + 1))
        return None

    def get_quote(self, instrument_keys: list[str]) -> dict[str, dict]:
        """Fetch batched market quote (`/v2/market-quote/quotes`) including option greeks (`delta`, `iv`).

        Retries once on failure with 401 token invalidation (matching get_candles pattern).
        """
        if not instrument_keys:
            return {}
        self.ensure_session()
        self._quote_gate.wait()

        results: dict[str, dict] = {}
        chunk_size = 50
        for i in range(0, len(instrument_keys), chunk_size):
            chunk = instrument_keys[i:i + chunk_size]
            url = f"{BASE_URL}/market-quote/quotes"
            for attempt in range(2):  # 1 retry
                try:
                    r = self._http.get(url, headers=self._headers(), params={"symbol": ",".join(chunk)}, timeout=8)
                    if r.status_code == 401 and attempt == 0:
                        logger.info("Upstox quote got 401 — invalidating session and retrying")
                        self._invalidate_session()
                        self.ensure_session()
                        self._quote_gate.wait()
                        continue
                    if r.ok:
                        body = r.json()
                        if body.get("status") == "success" and body.get("data"):
                            results.update(body["data"])
                    break  # success or non-retryable error
                except Exception as exc:
                    if attempt == 0:
                        logger.debug("Upstox quote batch failed (attempt 1): %s — retrying", exc)
                        continue
                    logger.debug("Upstox quote batch failed (attempt 2): %s", exc)
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
        """Place an Intraday bracket on Upstox as three legs: a marketable ENTRY,
        a protective SL-M STOP, and a target LIMIT.

        Upstox API v2 has no native bracket/OCO product, so the previous
        implementation silently dropped the stop and target and left every live
        position naked. This version transmits real protective legs and refuses
        to report success unless the STOP was accepted. The two exit legs are
        not exchange-OCO: a monitor must cancel the sibling when one fills
        (``requires_oco_monitor`` is returned True to signal this).
        """
        idempotency_key = tag or f"UPSTOX-ROBO-{uuid.uuid4().hex[:8]}"
        exit_txn = "SELL" if transaction_type.upper() == "BUY" else "BUY"
        if not is_live_execution():
            logger.info("[PAPER MODE] Upstox bracket %s %s qty=%s entry=%s SL=%s TP=%s", transaction_type, symbol, quantity, entry_price, stop_loss, take_profit)
            return {
                "status": True,
                "message": "Paper Upstox bracket order placed successfully",
                "data": {"order_id": f"PAPER-ROBO-UPSTOX-{uuid.uuid4().hex[:10]}", "tag": idempotency_key},
                "legs": {"entry": "PAPER", "stop": "PAPER" if stop_loss > 0 else None, "target": "PAPER" if take_profit > 0 else None},
                "requires_oco_monitor": True,
                "mode": "PAPER",
            }

        # 1) Marketable entry — a LIMIT pinned at the last price only fills when
        #    price trades back to it (winners run away, losers fill), so use MARKET.
        entry_res = self.place_order(
            symbol=symbol, transaction_type=transaction_type, quantity=quantity,
            order_type="MARKET", product="I", tag=f"{idempotency_key}-E",
        )
        legs: dict[str, Any] = {"entry": entry_res}

        # 2) Protective stop (SL-M) — REQUIRED. If this fails the entry is naked;
        #    surface the failure loudly instead of pretending the bracket is set.
        stop_ok = False
        if stop_loss and stop_loss > 0:
            try:
                legs["stop"] = self.place_order(
                    symbol=symbol, transaction_type=exit_txn, quantity=quantity,
                    order_type="SL-M", trigger_price=float(stop_loss), product="I",
                    tag=f"{idempotency_key}-SL",
                )
                stop_ok = True
            except Exception as exc:
                logger.error("Upstox STOP leg FAILED for %s (position is unprotected!): %s", symbol, exc)
                legs["stop"] = {"status": False, "error": str(exc)}
                
                # C2-05: Emergency flatten if STOP leg fails
                logger.warning("Emergency flattening entry leg for %s due to STOP leg failure.", symbol)
                try:
                    self.place_order(
                        symbol=symbol, transaction_type=exit_txn, quantity=quantity,
                        order_type="MARKET", product="I", tag=f"{idempotency_key}-FLATTEN"
                    )
                except Exception as flat_exc:
                    logger.critical("EMERGENCY FLATTEN FAILED for %s: %s", symbol, flat_exc)

        # 3) Target (LIMIT) — best effort; a monitor must cancel this if the stop fills.
        target_ok = False
        if take_profit and take_profit > 0 and stop_ok:
            try:
                legs["target"] = self.place_order(
                    symbol=symbol, transaction_type=exit_txn, quantity=quantity,
                    order_type="LIMIT", price=float(take_profit), product="I",
                    tag=f"{idempotency_key}-TP",
                )
                target_ok = True
            except Exception as exc:
                logger.warning("Upstox TARGET leg failed for %s: %s", symbol, exc)
                legs["target"] = {"status": False, "error": str(exc)}

        # M2-06: Make target success transparent in the overall status
        status_msg = "Upstox bracket placed"
        if not stop_ok:
            status_msg = "Upstox entry placed but STOP leg missing — entry flattened"
        elif not target_ok and take_profit and take_profit > 0:
            status_msg = "Upstox bracket placed, but TARGET leg failed"

        return {
            "status": bool(stop_ok),
            "target_status": bool(target_ok),
            "message": status_msg,
            "data": entry_res.get("data", {}) if isinstance(entry_res, dict) else {},
            "legs": legs,
            "requires_oco_monitor": True,
        }

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
