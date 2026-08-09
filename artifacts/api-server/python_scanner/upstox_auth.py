"""Upstox session lifecycle — daily token refresh and login state.

Upstox cannot be logged into with a user id and password. The only supported
flow is OAuth:

    1. send the operator to login_url()
    2. they authenticate + 2FA in a browser; Upstox redirects back with ?code=
    3. complete_login(code) exchanges it for an access token

The access token expires every morning at 03:30 IST, so this is a once-per-
trading-day manual step. The account password is deliberately never used here:
Upstox has no password-based API login, so holding one would be pure liability.
(`UPSTOX_PASSWORD` sits in the secrets file today and this module ignores it.)

Modelled on the Kite/Fyers auth helpers in the rp46-3 system so the dashboard
and the runtime watchdog can treat every broker the same:

    status()          -> {connected, auth_required, reason, user_id, token_updated_at,
                          expires_at, seconds_remaining}
    login_url()       -> the browser URL to start a session
    complete_login()  -> exchange the redirect code for a token
    load_cached_session() -> restore a still-valid token on startup

Freshness is decided by ASKING Upstox, not by inferring from the clock. The
clock heuristic is wrong in both directions: it calls a token issued at 03:31
"fresh" for 24 hours even after the app is revoked, and it would call a
perfectly good token dead the moment it crosses a boundary. One cheap profile
call settles it; the cutoff is only a fallback for when Upstox is unreachable.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / "upstox_secrets.env"
TOKEN_FILE = HERE / "upstox_session.json"

# Upstox invalidates every access token at 03:30 IST daily.
TOKEN_CUTOFF = dt_time(3, 30)

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"

_PLACEHOLDERS = {"", "your_api_key", "your_api_secret", "changeme", "xxxx", "none", "null"}


def most_recent_token_cutoff(now: Optional[datetime] = None) -> datetime:
    """The boundary before which any Upstox token has certainly expired."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    today = now.replace(hour=TOKEN_CUTOFF.hour, minute=TOKEN_CUTOFF.minute,
                        second=0, microsecond=0)
    return today if now >= today else today - timedelta(days=1)


def next_token_expiry(now: Optional[datetime] = None) -> datetime:
    """When the CURRENT token will die."""
    return most_recent_token_cutoff(now) + timedelta(days=1)


def decode_jwt_expiry(token: str) -> Optional[datetime]:
    """Read `exp` out of the Upstox JWT without verifying it.

    Upstox tokens are JWTs carrying their own expiry, which beats any assumption
    about the daily cutoff. Signature is NOT checked — this is used only to
    display and to pre-empt an expiry, never to authorise anything.
    """
    try:
        import base64
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), tz=ZoneInfo("UTC")).astimezone(IST)
    except Exception:
        pass
    return None


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI",
                "UPSTOX_ACCESS_TOKEN"):
        env.setdefault(key, os.getenv(key, ""))
    return env


class UpstoxAuth:
    """Process-wide Upstox session state."""

    def __init__(self, token_file: Path = TOKEN_FILE) -> None:
        self.token_file = Path(token_file)
        self._lock = threading.RLock()
        self._access_token: str = ""
        self._user_id: str = ""
        self._token_issued_at: Optional[datetime] = None
        self._token_expires_at: Optional[datetime] = None
        self._auth_required: bool = True
        self._auth_reason: str = "not yet loaded"

    # ── Credentials ──────────────────────────────────────────────────────────
    @property
    def api_key(self) -> str:
        return _read_env().get("UPSTOX_API_KEY", "")

    def missing_credentials(self) -> list[str]:
        env = _read_env()
        return [
            name for name in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI")
            if str(env.get(name, "")).strip().lower() in _PLACEHOLDERS
        ]

    def _mark_auth_required(self, reason: str) -> None:
        with self._lock:
            self._auth_required = True
            self._auth_reason = reason
            self._access_token = ""
        logger.warning("Upstox authentication required: %s", reason)

    # ── Manual login flow ────────────────────────────────────────────────────
    def login_url(self) -> str:
        import urllib.parse
        missing = self.missing_credentials()
        if missing:
            message = f"Missing Upstox credentials in upstox_secrets.env: {', '.join(missing)}"
            self._mark_auth_required(message)
            raise ValueError(message)
        env = _read_env()
        params = {
            "response_type": "code",
            "client_id": env["UPSTOX_API_KEY"],
            "redirect_uri": env["UPSTOX_REDIRECT_URI"],
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def extract_code(raw: str) -> str:
        """Accept the bare code OR the whole redirect URL.

        Pasting the full URL is the obvious thing to do and used to fail silently.
        """
        import urllib.parse
        raw = (raw or "").strip().strip('"').strip("'")
        if not raw:
            return ""
        if "code=" in raw:
            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query or parsed.fragment or "")
            if qs.get("code"):
                return qs["code"][0].strip()
            return raw.split("code=", 1)[1].split("&")[0].strip()
        return raw

    def complete_login(self, code_or_url: str) -> dict[str, Any]:
        """Exchange a browser-issued code for a daily access token."""
        code = self.extract_code(code_or_url)
        if not code:
            raise ValueError("an authorization code (or the redirect URL) is required")
        missing = self.missing_credentials()
        if missing:
            raise ValueError(f"Missing Upstox credentials: {', '.join(missing)}")

        env = _read_env()
        resp = requests.post(
            TOKEN_URL,
            headers={"accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": env["UPSTOX_API_KEY"],
                "client_secret": env["UPSTOX_API_SECRET"],
                "redirect_uri": env["UPSTOX_REDIRECT_URI"],
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Upstox rejected the token exchange ({resp.status_code}): {resp.text[:300]}. "
                "Usually a truncated/expired/reused code, or a redirect_uri that does not "
                "exactly match the app registration."
            )
        result = resp.json() or {}
        access_token = str(result.get("access_token") or "")
        if not access_token:
            raise RuntimeError(f"Upstox returned no access_token: {result}")

        # Verify before persisting, so a bad token is never cached as good.
        ok, reason = self._verify_live(access_token)
        if ok is False:
            raise RuntimeError(f"Upstox issued a token that immediately failed: {reason}")

        with self._lock:
            self._access_token = access_token
            self._user_id = str(result.get("user_id") or result.get("user_name") or "")
            self._token_issued_at = datetime.now(IST)
            self._token_expires_at = decode_jwt_expiry(access_token) or next_token_expiry()
            self._auth_required = False
            self._auth_reason = ""
        self._persist(access_token, result)
        logger.info("Upstox session established for %s", self._user_id or "user")
        return {
            "user_id": self._user_id,
            "issued_at": self._token_issued_at.isoformat(),
            "expires_at": self._token_expires_at.isoformat() if self._token_expires_at else None,
        }

    def _persist(self, access_token: str, session: dict[str, Any]) -> None:
        """Cache the token atomically. Never writes api_secret or any password."""
        payload = {
            "access_token": access_token,
            "user_id": session.get("user_id", "") or session.get("user_name", ""),
            "issued_at": (self._token_issued_at or datetime.now(IST)).isoformat(),
            "expires_at": self._token_expires_at.isoformat() if self._token_expires_at else None,
            # Bound to the app it was issued for: a regenerated app leaves a token
            # that authenticates as nothing, and that should be said plainly rather
            # than discovered inside an order.
            "api_key": self.api_key,
        }
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=str(self.token_file.parent), suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.token_file)   # write-then-rename: no half-written token
        except Exception as exc:
            logger.error("Failed to cache the Upstox token: %s", exc)

    # ── Restore ──────────────────────────────────────────────────────────────
    def load_cached_session(self) -> bool:
        """Restore a still-valid token. False when a login is required."""
        env = _read_env()
        token = ""
        issued_at: Optional[datetime] = None
        source = ""

        if self.token_file.exists():
            try:
                payload = json.loads(self.token_file.read_text(encoding="utf-8"))
                if str(payload.get("api_key") or "") not in ("", self.api_key):
                    self._mark_auth_required("cached Upstox token belongs to a different api_key")
                    return False
                token = str(payload.get("access_token") or "")
                issued_at = self._parse_ist(payload.get("issued_at"))
                source = "session cache"
            except Exception as exc:
                logger.warning("Unreadable Upstox token cache: %s", exc)

        if not token and env.get("UPSTOX_ACCESS_TOKEN"):
            token = env["UPSTOX_ACCESS_TOKEN"]
            source = "upstox_secrets.env"

        if not token:
            self._mark_auth_required("no Upstox access token (run upstox_login.py)")
            return False

        expires_at = decode_jwt_expiry(token)
        if expires_at and expires_at <= datetime.now(IST):
            self._mark_auth_required(
                f"Upstox token expired at {expires_at:%Y-%m-%d %H:%M} IST"
            )
            return False

        verdict, reason = self._verify_live(token)
        if verdict is False:
            self._mark_auth_required(reason)
            return False
        if verdict is None:
            # Could not reach Upstox. Fall back to the cutoff so an obviously stale
            # token is still refused offline, without failing a plausibly-good one.
            reference = issued_at or expires_at
            if reference and reference < most_recent_token_cutoff():
                self._mark_auth_required(
                    f"Upstox unreachable and the cached token predates the "
                    f"{TOKEN_CUTOFF:%H:%M} IST daily cutoff"
                )
                return False
            logger.warning("Could not verify the Upstox token (%s); using it provisionally.", reason)

        with self._lock:
            self._access_token = token
            self._token_issued_at = issued_at
            self._token_expires_at = expires_at or next_token_expiry()
            self._auth_required = False
            self._auth_reason = ""
        logger.info("Upstox session restored from %s", source)
        return True

    @staticmethod
    def _verify_live(token: str) -> tuple[Optional[bool], str]:
        """(True|False|None, reason). None means 'could not tell', not 'invalid'."""
        try:
            r = requests.get(
                PROFILE_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                         "Api-Version": "2.0"},
                timeout=10,
            )
        except Exception as exc:
            return None, f"could not reach Upstox: {exc}"
        if r.status_code in (401, 403):
            return False, f"Upstox rejected the token ({r.status_code}) - expired or revoked"
        if not r.ok:
            return None, f"unexpected Upstox response {r.status_code}"
        try:
            return (True, "") if (r.json() or {}).get("status") == "success" else (
                False, "Upstox did not confirm the session")
        except Exception:
            return None, "unparseable Upstox profile response"

    @staticmethod
    def _parse_ist(value: Any) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(str(value))
            return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
        except Exception:
            return None

    # ── Runtime ──────────────────────────────────────────────────────────────
    @property
    def access_token(self) -> str:
        with self._lock:
            return self._access_token

    def invalidate(self, reason: str) -> None:
        """Drop the session after Upstox reports the token is dead (401/403)."""
        logger.error("Upstox session invalidated: %s", reason)
        self._mark_auth_required(reason)

    def status(self) -> dict[str, Any]:
        with self._lock:
            expires = self._token_expires_at
            remaining = int((expires - datetime.now(IST)).total_seconds()) if expires else None
            return {
                "broker": "upstox",
                "connected": bool(self._access_token) and not self._auth_required,
                "auth_required": self._auth_required,
                "reason": self._auth_reason,
                "user_id": self._user_id,
                "token_updated_at": self._token_issued_at.isoformat() if self._token_issued_at else None,
                "expires_at": expires.isoformat() if expires else None,
                "seconds_remaining": max(0, remaining) if remaining is not None else None,
                "hours_remaining": round(max(0, remaining) / 3600, 1) if remaining is not None else None,
                # The dashboard nags before the session dies rather than after.
                "expiring_soon": bool(remaining is not None and 0 < remaining <= 3600),
                "daily_cutoff_ist": TOKEN_CUTOFF.strftime("%H:%M"),
                "missing_credentials": self.missing_credentials(),
            }


_SHARED: Optional[UpstoxAuth] = None
_SHARED_GUARD = threading.Lock()


def get_upstox_auth() -> UpstoxAuth:
    """Process-wide Upstox session."""
    global _SHARED
    with _SHARED_GUARD:
        if _SHARED is None:
            _SHARED = UpstoxAuth()
            try:
                _SHARED.load_cached_session()
            except Exception as exc:
                logger.error("Upstox session restore failed: %s", exc)
        return _SHARED


def reset_upstox_auth() -> None:
    global _SHARED
    with _SHARED_GUARD:
        _SHARED = None
