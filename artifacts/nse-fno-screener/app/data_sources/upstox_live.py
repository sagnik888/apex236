"""
Upstox live market-data adapter (Market Data Feed V3, WebSocket, Protobuf).

SECURITY — READ THIS FIRST:
This module reads credentials ONLY from environment variables (see
.env.example). Never hardcode UPSTOX_API_KEY / UPSTOX_API_SECRET /
UPSTOX_ANALYTICS_TOKEN anywhere in source. If a key has ever been pasted
into a chat, a document, or committed to git, treat it as compromised
and regenerate it from the Upstox developer console
(account.upstox.com/developer/apps) before pointing this module at it.

TOKEN STRATEGY (verified against upstox.com/developer docs, Sept 2026 —
this is newer than most write-ups of the Upstox API, including the one
that was audited for this rebuild, so it's worth double-checking against
the live docs again before you build against it):

Upstox now offers two token types:
  - Standard access token: must be regenerated daily via the OAuth2
    browser-login flow (auth code -> token exchange). Required for order
    placement / account actions.
  - Analytics token: generated ONCE, does NOT need daily re-authorization,
    and explicitly powers Market Data + Realtime & Streaming APIs — which
    is everything this screener needs, since it never places orders. Use
    UPSTOX_ANALYTICS_TOKEN here and the daily-re-auth problem (ChatGPT's
    plan calls this out as its #2 "prioritized gap") doesn't need solving
    for the screener at all.
  If this project later grows an execution/order-placement layer (e.g.
  feeding your CAT-IEIS auto-trading spec), THAT piece needs the standard token + daily
  OAuth flow — keep it in a separate module so the read-only screener
  never inherits order-placement complexity, or credentials scoped for
  order placement, that it doesn't need.

SUBSCRIPTION MODE: use LTPC or "full" (quote) mode, NOT "D30" full
market-depth mode — D30 is capped around 50 instruments per connection
(it's built for deep order-book views of a handful of symbols), while
LTPC/full-quote mode supports thousands of instrument keys on a single
connection. 237 symbols is comfortably within that on ONE WebSocket
connection; there's no need to shard the universe across connections or
juggle Upstox's (paid-tier-gated) multi-connection allowance.

WHAT'S IMPLEMENTED vs WHAT'S INTENTIONALLY LEFT:
Implemented: config/token loading from env, the authorize-then-connect
handshake shape, a reconnect/backoff skeleton, and the public interface
the rest of the app calls against.
Left for you to finish — deliberately, because it needs your own
rotated credentials and Upstox's .proto schema file, neither of which
should live in a file an AI assistant generates: decoding the actual
protobuf tick payload into the OHLCV dict this adapter returns. Upstox
publishes an official Agent Skills pack for this
(`npx skills add upstox/upstox-skills --skill upstox`) that gives an
AI coding agent guardrailed, current knowledge of this exact API
surface — including F&O lot-size validation, which is directly relevant
to this instrument list — worth installing for that last step.
"""
import asyncio
import os
from dataclasses import dataclass, field

import pandas as pd

from app.data_sources.base import MarketDataSource


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"{key} is not set. Copy .env.example to .env and fill in your "
            f"OWN rotated value — see the module docstring before you do."
        )
    return val


@dataclass
class UpstoxConfig:
    api_key: str = field(default_factory=lambda: _require_env("UPSTOX_API_KEY"))
    api_secret: str = field(default_factory=lambda: _require_env("UPSTOX_API_SECRET"))
    analytics_token: str = field(default_factory=lambda: _require_env("UPSTOX_ANALYTICS_TOKEN"))
    ws_authorize_url: str = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    subscription_mode: str = "full"  # "full" (LTPC+quote) not "d30"


class UpstoxLiveSource(MarketDataSource):
    name = "upstox_live"

    def __init__(self, config: UpstoxConfig | None = None):
        self.config = config or UpstoxConfig()
        self._tick_cache: dict[str, dict] = {}   # instrument_key -> latest tick
        self._connected = False
        self._reconnect_delay = 1  # seconds, doubles on failure up to a cap

    def freshness_label(self) -> str:
        return "LIVE" if self._connected else "OFFLINE"

    async def connect(self, instrument_keys: list[str]):
        """
        1. GET {ws_authorize_url} with `Authorization: Bearer
           {analytics_token}` — the API responds by redirecting to a
           wss:// endpoint; configure your WS client to follow that
           redirect rather than parsing this call's body as the feed.
        2. Open the wss:// connection, send a `sub` control message with
           `instrumentKeys=instrument_keys` and
           `mode=self.config.subscription_mode`.
        3. Decode each incoming Protobuf frame (Upstox's
           MarketDataFeedV3 schema — download the current .proto from
           their docs, don't hand-roll the schema from memory) and
           update self._tick_cache, then set self._connected = True.
        4. On disconnect: exponential backoff (self._reconnect_delay,
           doubling, capped at ~30s) and re-subscribe on reconnect.
           Set self._connected = False THE MOMENT the socket drops —
           before any retry logic runs — so freshness_label() flips to
           OFFLINE immediately rather than continuing to claim LIVE
           while quietly serving a stale tick cache. That flag is what
           keeps the UI honest about what it's showing.
        """
        raise NotImplementedError(
            "Wire this against your rotated credentials + Upstox's current "
            "protobuf schema when you're ready — see the module docstring "
            "for the exact steps and the Agent Skills pack pointer."
        )

    async def get_bars(self, symbol: str, interval: str, lookback_bars: int) -> pd.DataFrame:
        """
        For 5min/15min/1h: build from the accumulating tick cache via
        engine.timeframes.resample_ohlcv(), bar-sealed.
        For 4h/1d/2d/4d/1w: prefer Upstox's historical-candle REST
        endpoint (exchange-official closes) over resampling from live
        ticks you only started collecting today — you cannot
        resample-your-way to 60 days of 4H history from a feed that's
        been connected for an hour.
        """
        raise NotImplementedError("Implement after connect() — see the module docstring above.")
