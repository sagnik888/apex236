"""User-tunable scanner settings, persisted to apex_settings.json.

Settings reach the process-pool scan workers via a revision number (file
mtime): the parent passes the revision with each task and workers reload
the file when it changes.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).resolve().parent / "apex_settings.json"

DEFAULTS: dict[str, Any] = {
    # Which timeframes the background scanner runs.
    "enabled_timeframes": ["15m", "1h", "4h", "1d"],
    # APEX signal gates. These mirror the manual APEX Hybrid Pro panel rather
    # than silently keeping a different set of per-timeframe values in the API.
    "min_score": 60.0,
    "conflict_margin": 20.0,
    "min_adx": 20.0,
    "use_htf": True,
    "signal_cooldown": 1,
    # Stop-loss: "auto" keeps per-timeframe defaults (0.5% fixed intraday,
    # ATR elsewhere); "fixed" forces fixed_sl_pct everywhere.
    "sl_mode": "auto",
    "fixed_sl_pct": 0.5,
    "atr_mult": 1.5,
    # Targets: "rr" uses R-multiples of the stop distance; "fixed" targets a
    # user % move from entry (t1 = pct, t2 = 1.5x, t3 = 2x).
    "target_mode": "rr",
    "t1_r": 2.0,
    "t2_r": 3.0,
    "t3_r": 4.0,
    "fixed_tp_pct": 0.75,
    # Book the FULL position the moment T1 is touched (user's 0.5–1% style)
    # instead of trailing for larger moves.
    "exit_at_t1": False,
    "use_trail": True,
    "trail_start_r": 1.5,
    "trail_mult": 1.8,
    "lock_at_t1": True,
    "exit_confirmation_bars": 3,
    "max_consecutive_losses": 3,
    "circuit_pause_bars": 5,
    "use_session": True,
    "block_open_noise": False,
    "block_close_noise": False,
    # Execution & Risk Management
    "execution_mode": "PAPER",
    "daily_max_loss_pct": 2.0,
    "slippage_pct": 0.1,
    "max_open_positions": 5,
    "max_per_sector": 2,
    # Options Guidance & Intraday Trading Engine
    "enable_options": True,
    "strike_mode": "Smart Auto",
    "trade_options_intraday": True,
    "options_broker": "upstox",
    "options_stop_mode": "Delta-Translated",
}

_RANGES: dict[str, tuple[float, float]] = {
    "min_score": (0.0, 100.0),
    "conflict_margin": (0.0, 100.0),
    "min_adx": (0.0, 100.0),
    "signal_cooldown": (0.0, 50.0),
    "fixed_sl_pct": (0.1, 10.0),
    "atr_mult": (0.5, 10.0),
    "t1_r": (0.5, 20.0),
    "t2_r": (0.5, 30.0),
    "t3_r": (0.5, 50.0),
    "fixed_tp_pct": (0.1, 25.0),
    "trail_start_r": (0.5, 20.0),
    "trail_mult": (0.5, 10.0),
    "exit_confirmation_bars": (1, 10),
    "max_consecutive_losses": (1, 20),
    "circuit_pause_bars": (1, 50),
    "daily_max_loss_pct": (0.1, 20.0),
    "slippage_pct": (0.0, 5.0),
    "max_open_positions": (1, 100),
    "max_per_sector": (1, 20),
}

_lock = threading.Lock()


def _read_file() -> dict[str, Any]:
    try:
        if SETTINGS_PATH.exists():
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read settings file: %s", exc)
    return {}


def get_settings() -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in _read_file().items() if k in DEFAULTS})
    return merged


def validate(partial: dict[str, Any]) -> dict[str, Any]:
    """Validate/coerce a partial settings dict; raises ValueError."""
    clean: dict[str, Any] = {}
    for key, value in partial.items():
        if key not in DEFAULTS:
            continue  # ignore unknown keys silently (forward compat)
        if key == "enabled_timeframes":
            tfs = [tf for tf in value if tf in ("15m", "1h", "4h", "1d")] if isinstance(value, list) else []
            if not tfs:
                raise ValueError("enabled_timeframes must contain at least one of 15m/1h/4h/1d")
            clean[key] = tfs
        elif key == "sl_mode":
            if value not in ("auto", "fixed"):
                raise ValueError("sl_mode must be 'auto' or 'fixed'")
            clean[key] = value
        elif key == "target_mode":
            if value not in ("rr", "fixed"):
                raise ValueError("target_mode must be 'rr' or 'fixed'")
            clean[key] = value
        elif key == "execution_mode":
            if str(value).upper() not in ("PAPER", "LIVE"):
                raise ValueError("execution_mode must be 'PAPER' or 'LIVE'")
            clean[key] = str(value).upper()
        elif key == "strike_mode":
            if value not in ("Smart Auto", "Always ATM", "Always OTM1", "Always ITM1"):
                raise ValueError("strike_mode must be one of: Smart Auto, Always ATM, Always OTM1, Always ITM1")
            clean[key] = str(value)
        elif key == "options_broker":
            if value not in ("upstox", "angelone"):
                raise ValueError("options_broker must be 'upstox' or 'angelone'")
            clean[key] = str(value)
        elif key == "options_stop_mode":
            if value not in ("Delta-Translated", "Option ATR"):
                raise ValueError("options_stop_mode must be 'Delta-Translated' or 'Option ATR'")
            clean[key] = str(value)
        elif key in ("exit_at_t1", "use_trail", "use_htf", "lock_at_t1", "use_session", "block_open_noise", "block_close_noise", "enable_options", "trade_options_intraday"):
            clean[key] = bool(value)
        else:
            try:
                f = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
            lo, hi = _RANGES[key]
            if not (lo <= f <= hi):
                raise ValueError(f"{key} must be between {lo} and {hi}")
            # These settings configure bar counts, not fractional quantities.
            clean[key] = int(f) if key in ("signal_cooldown", "exit_confirmation_bars", "max_consecutive_losses", "circuit_pause_bars", "max_open_positions", "max_per_sector") else f
    if {"t1_r", "t2_r", "t3_r"} & clean.keys():
        merged = {**get_settings(), **clean}
        if not (merged["t1_r"] <= merged["t2_r"] <= merged["t3_r"]):
            raise ValueError("Targets must satisfy t1_r <= t2_r <= t3_r")
    return clean


def update_settings(partial: dict[str, Any]) -> dict[str, Any]:
    clean = validate(partial)
    with _lock:
        merged = get_settings()
        merged.update(clean)
        SETTINGS_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    logger.info("Settings updated: %s", clean)
    return merged


def revision() -> int:
    """Monotonic-enough revision for cross-process cache invalidation."""
    try:
        return int(SETTINGS_PATH.stat().st_mtime_ns)
    except OSError:
        return 0
