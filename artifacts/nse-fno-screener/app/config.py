import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, default)


# Two-speed scheduler. The quote pulse is intentionally much faster than
# indicator recomputation: prices can change every tick, but a CLOSED 5m/15m/
# 30m candle only changes when that bar closes. This keeps the UI live without
# wasting historical-data calls or scoring the same closed bar repeatedly.
QUOTE_REFRESH_SECONDS = _env_int("QUOTE_REFRESH_SECONDS", 15, 5)
MICRO_SCAN_SECONDS = _env_int("MICRO_SCAN_SECONDS", 55, 30)      # 5m / 15m / 30m
MEDIUM_SCAN_SECONDS = _env_int("MEDIUM_SCAN_SECONDS", 120, 60)   # 1h / 4h
MACRO_SCAN_SECONDS = _env_int("MACRO_SCAN_SECONDS", 300, 120)    # 1d / 2d / 4d / 1w

# Outside live NSE hours, slow the loops down. A startup warm scan still runs
# immediately, so the dashboard is populated even when the market is closed.
CLOSED_QUOTE_REFRESH_SECONDS = _env_int("CLOSED_QUOTE_REFRESH_SECONDS", 180, 30)
CLOSED_MICRO_SCAN_SECONDS = _env_int("CLOSED_MICRO_SCAN_SECONDS", 300, 60)
CLOSED_MEDIUM_SCAN_SECONDS = _env_int("CLOSED_MEDIUM_SCAN_SECONDS", 600, 120)
CLOSED_MACRO_SCAN_SECONDS = _env_int("CLOSED_MACRO_SCAN_SECONDS", 900, 300)

# Guard against repeated manual-click hammering of the same source.
MANUAL_REFRESH_COOLDOWN_SECONDS = _env_int("MANUAL_REFRESH_COOLDOWN_SECONDS", 12, 5)

# Backward-compatible aliases for older .env files.
FAST_REFRESH_SECONDS = MICRO_SCAN_SECONDS
SLOW_REFRESH_SECONDS = MACRO_SCAN_SECONDS

INSTRUMENTS_PATH = os.environ.get("INSTRUMENTS_PATH", "data/instruments.json")
USE_UPSTOX_LIVE = os.environ.get("USE_UPSTOX_LIVE", "false").lower() == "true"
UPSTOX_ANALYTICS_TOKEN = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
TOP_ROWS = max(237, int(os.environ.get("TOP_ROWS", 237)))
