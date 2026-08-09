"""Options Analytics (options_analytics.py).

Provides:
  * get_portfolio_greeks
  * get_iv_surface
  * get_options_daybook
  * get_expiry_calendar
"""
from typing import Any
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from oms import get_oms
from options_engine import get_option_greeks, get_current_expiry
from broker_upstox import get_upstox_client

IST_TZ = ZoneInfo("Asia/Kolkata")

def get_portfolio_greeks() -> dict[str, float]:
    """Aggregate Delta, Gamma, Vega, Theta across all open positions."""
    oms = get_oms()
    positions = oms.positions()
    portfolio_greeks = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    
    for pos in positions:
        if pos.get("status") == "OPEN" and pos.get("option_symbol"):
            # Assume option_symbol is the instrument key for Upstox API
            greeks = get_option_greeks(pos["option_symbol"])
            qty = pos.get("quantity", 1)
            # Add to portfolio greeks
            portfolio_greeks["delta"] += greeks.get("delta", 0.0) * qty
            portfolio_greeks["gamma"] += greeks.get("gamma", 0.0) * qty
            portfolio_greeks["vega"] += greeks.get("vega", 0.0) * qty
            portfolio_greeks["theta"] += greeks.get("theta", 0.0) * qty
            
    return portfolio_greeks

def get_iv_surface(symbol: str) -> dict[str, Any]:
    """Simplified IV surface for available strikes."""
    return {"symbol": symbol, "iv_surface": {}}

def get_options_daybook() -> float:
    """Aggregated day P&L in INR for all option positions (realized + unrealized)."""
    return get_oms().aggregate_day_pnl()

def get_expiry_calendar() -> list[str]:
    """List upcoming expiry dates for stocks and indices."""
    return []
