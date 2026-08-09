"""AngelOne SmartAPI read-only connectivity smoke test.

Tests (in order):
  1. TOTP generation
  2. loginByPassword  -> jwt / refresh / feed tokens
  3. getProfile       -> account identity + enabled exchanges
  4. getRMS           -> funds (read-only)
  5. Instrument master download + token resolution for scanner symbols
  6. Market quote (FULL mode, batched)
  7. Historical 15m candles + freshness comparison vs Yahoo Finance
  8. Market-data WebSocket v2 (live ticks, mode LTP)
  9. Order-status WebSocket handshake (listen only)
 10. logout

This script NEVER touches order endpoints. It is strictly read-only.
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import json
import os
import struct
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")

BASE = "https://apiconnect.angelone.in"
INSTRUMENTS_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
WS_FEED_URL = "wss://smartapisocket.angelone.in/smart-stream"
WS_ORDER_URL = "wss://tns.angelone.in/smart-order-update"

RESULTS: list[tuple[str, str, str]] = []  # (step, PASS/FAIL/WARN, detail)


def record(step: str, ok: bool, detail: str, warn: bool = False) -> None:
    status = "WARN" if warn else ("PASS" if ok else "FAIL")
    RESULTS.append((step, status, detail))
    print(f"[{status}] {step}: {detail}", flush=True)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = HERE / "broker_secrets.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for key in ("ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_API_KEY", "ANGEL_TOTP_SECRET"):
        env.setdefault(key, os.getenv(key, ""))
    return env


def headers(api_key: str, jwt: str | None = None, public_ip: str = "106.0.0.1") -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "192.168.1.10",
        "X-ClientPublicIP": public_ip,
        "X-MACAddress": "00:0a:95:9d:68:16",
        "X-PrivateKey": api_key,
    }
    if jwt:
        h["Authorization"] = f"Bearer {jwt}"
    return h


def main() -> int:
    env = load_env()
    missing = [k for k in ("ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_API_KEY", "ANGEL_TOTP_SECRET") if not env.get(k)]
    if missing:
        record("credentials", False, f"missing {missing}")
        return 1

    api_key = env["ANGEL_API_KEY"]
    public_ip = env.get("ANGEL_PUBLIC_IP") or "106.0.0.1"

    # ── 1. TOTP ───────────────────────────────────────────────────────────────
    try:
        import pyotp
        totp = pyotp.TOTP(env["ANGEL_TOTP_SECRET"]).now()
        record("totp", True, f"generated 6-digit code ({totp[:2]}****)")
    except Exception as exc:
        record("totp", False, repr(exc))
        return 1

    session = requests.Session()

    # ── 2. Login ──────────────────────────────────────────────────────────────
    jwt = feed_token = refresh_token = None
    try:
        r = session.post(
            f"{BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
            headers=headers(api_key, public_ip=public_ip),
            json={"clientcode": env["ANGEL_CLIENT_ID"], "password": env["ANGEL_PIN"], "totp": totp},
            timeout=15,
        )
        body = r.json()
        if r.ok and body.get("status") and body.get("data"):
            jwt = body["data"]["jwtToken"]
            refresh_token = body["data"]["refreshToken"]
            feed_token = body["data"]["feedToken"]
            record("login", True, f"HTTP {r.status_code}; jwt len={len(jwt)}, feedToken len={len(str(feed_token))}")
        else:
            record("login", False, f"HTTP {r.status_code}; message={body.get('message')!r} errorcode={body.get('errorcode')!r}")
            return 1
    except Exception as exc:
        record("login", False, repr(exc))
        return 1

    auth = headers(api_key, jwt, public_ip)

    # ── 3. Profile ────────────────────────────────────────────────────────────
    try:
        r = session.get(f"{BASE}/rest/secure/angelbroking/user/v1/getProfile", headers=auth, timeout=15)
        body = r.json()
        d = body.get("data") or {}
        ok = r.ok and body.get("status")
        record("profile", ok, f"name={d.get('name')!r} clientcode={d.get('clientcode')} exchanges={d.get('exchanges')} products={d.get('products')}")
    except Exception as exc:
        record("profile", False, repr(exc))

    # ── 4. RMS / funds (read-only) ────────────────────────────────────────────
    try:
        r = session.get(f"{BASE}/rest/secure/angelbroking/user/v1/getRMS", headers=auth, timeout=15)
        body = r.json()
        d = body.get("data") or {}
        record("rms_funds", bool(r.ok and body.get("status")), f"availablecash={d.get('availablecash')} net={d.get('net')}")
    except Exception as exc:
        record("rms_funds", False, repr(exc))

    # ── 5. Instrument master + token resolution ──────────────────────────────
    tokens: dict[str, dict] = {}
    try:
        cache = HERE / "angel_instruments.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
            instruments = json.loads(cache.read_text(encoding="utf-8"))
            src = "cache"
        else:
            r = session.get(INSTRUMENTS_URL, timeout=120)
            r.raise_for_status()
            instruments = r.json()
            cache.write_text(json.dumps(instruments), encoding="utf-8")
            src = "download"
        nse_eq = {
            row["symbol"]: row for row in instruments
            if row.get("exch_seg") == "NSE" and str(row.get("symbol", "")).endswith("-EQ")
        }
        record("instruments", True, f"{len(instruments)} rows ({src}); NSE -EQ symbols: {len(nse_eq)}")

        from nifty50 import NIFTY236_SYMBOLS
        unresolved = []
        for ys in NIFTY236_SYMBOLS:
            base_sym = ys[:-3] if ys.endswith(".NS") else ys  # RELIANCE.NS -> RELIANCE
            row = nse_eq.get(f"{base_sym}-EQ")
            if row:
                tokens[base_sym] = row
            else:
                unresolved.append(base_sym)
        record(
            "token_resolution",
            len(unresolved) == 0,
            f"resolved {len(tokens)}/{len(NIFTY236_SYMBOLS)}; unresolved={unresolved}",
            warn=bool(unresolved) and len(unresolved) <= 5,
        )
        # Specifically check the suspected-dead ticker from the audit
        ltm = "LTM" in tokens
        ltim = nse_eq.get("LTIM-EQ")
        record("ticker_LTM_vs_LTIM", True, f"LTM-EQ exists={ltm}; LTIM-EQ exists={ltim is not None}", warn=not ltm)
    except Exception as exc:
        record("instruments", False, repr(exc))

    # ── 6. Market quote (FULL mode) ───────────────────────────────────────────
    quote_syms = [s for s in ("RELIANCE", "HDFCBANK", "M&M", "BAJAJ-AUTO") if s in tokens]
    try:
        tok_list = [tokens[s]["token"] for s in quote_syms]
        r = session.post(
            f"{BASE}/rest/secure/angelbroking/market/v1/quote/",
            headers=auth,
            json={"mode": "FULL", "exchangeTokens": {"NSE": tok_list}},
            timeout=15,
        )
        body = r.json()
        fetched = (body.get("data") or {}).get("fetched") or []
        detail = "; ".join(
            f"{q.get('tradingSymbol')} ltp={q.get('ltp')} feedTime={q.get('exchFeedTime')}" for q in fetched
        )
        record("quote_full", bool(r.ok and body.get("status") and fetched), detail or f"empty response: {body}")
    except Exception as exc:
        record("quote_full", False, repr(exc))

    # ── 7. Historical 15m candles + freshness vs Yahoo ───────────────────────
    try:
        now = datetime.now(IST)
        frm = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
        to = now.strftime("%Y-%m-%d %H:%M")
        body = {}
        last_status = None
        for attempt in range(4):
            time.sleep(1.2)  # SmartAPI historical returns empty bodies when throttled
            r = session.post(
                f"{BASE}/rest/secure/angelbroking/historical/v1/getCandleData",
                headers=auth,
                json={
                    "exchange": "NSE",
                    "symboltoken": tokens["RELIANCE"]["token"],
                    "interval": "FIFTEEN_MINUTE",
                    "fromdate": frm,
                    "todate": to,
                },
                timeout=30,
            )
            last_status = f"HTTP {r.status_code} body[:80]={r.text[:80]!r}"
            if r.status_code == 200 and r.text.strip():
                body = r.json()
                if body.get("data"):
                    break
        candles = body.get("data") or []
        if not candles:
            record("historical_15m", False, f"no data after retries; last={last_status}")
            raise RuntimeError("historical empty")
        last = candles[-1] if candles else None
        angel_last_ts = datetime.fromisoformat(last[0]) if last else None
        angel_age_min = (now - angel_last_ts).total_seconds() / 60 if angel_last_ts else None
        record(
            "historical_15m",
            bool(candles),
            f"{len(candles)} candles; last={last[0] if last else None} close={last[4] if last else None} (age {angel_age_min:.1f} min)"
            if candles else f"empty: {body.get('message')}",
        )

        # Yahoo comparison for the same symbol
        try:
            import yfinance as yf
            ydf = yf.Ticker("RELIANCE.NS").history(interval="15m", period="1d")
            if len(ydf):
                y_last = ydf.index[-1].tz_convert(IST)
                y_age_min = (now - y_last).total_seconds() / 60
                record(
                    "freshness_vs_yahoo",
                    True,
                    f"Angel last bar age={angel_age_min:.1f}min vs Yahoo last bar open age={y_age_min:.1f}min "
                    f"(yahoo close={ydf['Close'].iloc[-1]:.2f}, angel close={last[4] if last else None})",
                )
            else:
                record("freshness_vs_yahoo", True, "yahoo returned no data", warn=True)
        except Exception as exc:
            record("freshness_vs_yahoo", True, f"yahoo fetch failed: {exc!r}", warn=True)
    except Exception as exc:
        record("historical_15m", False, repr(exc))

    # ── 8. Market-data WebSocket v2 (live ticks) ─────────────────────────────
    try:
        import websocket

        ticks: list[tuple[str, float, datetime]] = []
        reliance_token = tokens["RELIANCE"]["token"]
        hdfc_token = tokens["HDFCBANK"]["token"]

        def on_open(ws):
            sub = {
                "correlationID": "apex_smoke",
                "action": 1,
                "params": {"mode": 1, "tokenList": [{"exchangeType": 1, "tokens": [reliance_token, hdfc_token]}]},
            }
            ws.send(json.dumps(sub))

        def on_message(ws, message):
            if isinstance(message, (bytes, bytearray)) and len(message) >= 51:
                token = message[2:27].split(b"\x00")[0].decode(errors="ignore")
                exch_ts_ms = struct.unpack_from("<q", message, 35)[0]
                ltp_paise = struct.unpack_from("<q", message, 43)[0]
                ticks.append((token, ltp_paise / 100.0, datetime.fromtimestamp(exch_ts_ms / 1000, IST)))
                if len(ticks) >= 8:
                    ws.close()

        def on_error(ws, error):
            ticks.append(("__error__", 0.0, datetime.now(IST)))
            print(f"      ws error: {error}", flush=True)

        ws = websocket.WebSocketApp(
            WS_FEED_URL,
            header={
                "Authorization": f"Bearer {jwt}",
                "x-api-key": api_key,
                "x-client-code": env["ANGEL_CLIENT_ID"],
                "x-feed-token": str(feed_token),
            },
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
        )
        import threading
        t = threading.Thread(target=lambda: ws.run_forever(ping_interval=25, ping_payload="ping"), daemon=True)
        t.start()
        t.join(timeout=15)
        try:
            ws.close()
        except Exception:
            pass
        real = [tk for tk in ticks if tk[0] != "__error__"]
        if real:
            sample = real[-1]
            record("ws_market_feed", True, f"{len(real)} ticks in <=15s; last: token={sample[0]} ltp={sample[1]:.2f} exch_time={sample[2].strftime('%H:%M:%S')}")
        else:
            record("ws_market_feed", False, f"no ticks received in 15s (errors={sum(1 for tk in ticks if tk[0]=='__error__')})")
    except Exception as exc:
        record("ws_market_feed", False, repr(exc))

    # ── 9. Order-status WebSocket handshake (listen only) ────────────────────
    try:
        import websocket as wsmod
        got: dict[str, object] = {}

        def o_open(ws):
            got["open"] = True

        def o_msg(ws, message):
            got["msg"] = message[:120] if isinstance(message, str) else repr(message[:60])
            ws.close()

        wso = wsmod.WebSocketApp(
            WS_ORDER_URL,
            header={"Authorization": f"Bearer {jwt}"},
            on_open=o_open,
            on_message=o_msg,
        )
        import threading as th
        t2 = th.Thread(target=lambda: wso.run_forever(ping_interval=9, ping_payload="ping"), daemon=True)
        t2.start()
        t2.join(timeout=8)
        try:
            wso.close()
        except Exception:
            pass
        record(
            "ws_order_status",
            bool(got.get("open")),
            f"handshake={'ok' if got.get('open') else 'failed'}; first message={got.get('msg', '(none - expected when no order activity)')}",
        )
    except Exception as exc:
        record("ws_order_status", False, repr(exc))

    # ── 10. Logout ────────────────────────────────────────────────────────────
    try:
        r = session.post(
            f"{BASE}/rest/secure/angelbroking/user/v1/logout",
            headers=auth,
            json={"clientcode": env["ANGEL_CLIENT_ID"]},
            timeout=15,
        )
        body = r.json()
        record("logout", bool(r.ok and body.get("status")), str(body.get("message")))
    except Exception as exc:
        record("logout", False, repr(exc))

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n===== SMOKE TEST SUMMARY =====")
    fails = 0
    for step, status, _ in RESULTS:
        print(f"  {status:4s}  {step}")
        fails += status == "FAIL"
    print(f"Result: {'ALL GREEN' if fails == 0 else f'{fails} step(s) FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
