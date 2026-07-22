"""Intraday ATM Options Trading Engine (`options_engine.py`).

Provides:
  * Dynamic ATM strike resolution (`resolve_atm_option`) for underlying stocks
    (`RELIANCE.NS`) and indices (`Nifty 50`, `BankNifty`) across `NSE_FO` via Upstox
  * Historical and real-time option candle/greeks synchronization
  * Precision option stop-loss & take-profit translation (`calculate_option_stops`)
    via Delta Translation or Option ATR
  * End-to-end option trade execution (`execute_option_trade`) routing `CE` and `PE`
    bracket orders directly via Upstox API v2 (`broker_dispatcher`).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from broker_dispatcher import get_dispatcher
from broker_upstox import get_upstox_client
from upstox_feed import get_upstox_feed

logger = logging.getLogger(__name__)
IST_TZ = ZoneInfo("Asia/Kolkata")


def resolve_strike_step(underlying_symbol: str, spot_price: float) -> float:
    """Determine the standard option strike interval based on symbol and price level."""
    sym = underlying_symbol.upper().replace(".NS", "")
    if "NIFTY 50" in sym or sym in ("^NSEI", "NIFTY"):
        return 50.0
    if "BANKNIFTY" in sym or sym in ("^NSEBANK", "NIFTYBANK"):
        return 100.0
    if "SENSEX" in sym:
        return 100.0

    # Stock options strike step ladder
    if spot_price <= 100:
        return 2.5
    elif spot_price <= 500:
        return 5.0
    elif spot_price <= 1500:
        return 10.0
    elif spot_price <= 3000:
        return 20.0
    else:
        return 50.0


def resolve_atm_option(
    underlying_symbol: str,
    spot_price: float,
    direction: str,  # "LONG"/"BUY" or "SHORT"/"SELL"
    expiry_mode: str = "nearest",
    strike_offset: int = 0,  # 0 = ATM, +1 = OTM1/ITM1
) -> Optional[dict[str, Any]]:
    """Find the best-matching active option contract (`NSE_FO`) via Upstox master."""
    step = resolve_strike_step(underlying_symbol, spot_price)
    atm_strike = round(spot_price / step) * step

    is_long = direction.upper() in ("LONG", "BUY")
    option_type = "CE" if is_long else "PE"

    # If offset requested: CE OTM is higher strike, PE OTM is lower strike
    # We apply strike_offset directly to step (e.g. strike_offset=0 -> ATM)
    target_strike = atm_strike + (strike_offset * step * (1 if is_long else -1))

    client = get_upstox_client()
    contract = client.resolve_option_contract(underlying_symbol, option_type, target_strike)
    if not contract and strike_offset != 0:
        # Fall back to strict ATM if requested offset strike not found
        contract = client.resolve_option_contract(underlying_symbol, option_type, atm_strike)
    return contract


def fetch_option_ohlcv(
    instrument_key: str,
    timeframe: str = "15m",
    lookback_days: int = 30,
) -> Optional[pd.DataFrame]:
    """Fetch option chart history (`15m` / `1h`) via Upstox API v2."""
    client = get_upstox_client()
    to_dt = datetime.now(IST_TZ)
    from_dt = to_dt - timedelta(days=lookback_days)
    df = client.get_candles(instrument_key, timeframe, from_dt, to_dt)
    if df is not None and not df.empty:
        # Check if live forming candle exists in feed
        fc = get_upstox_feed().get_forming_candle(instrument_key)
        if fc:
            fc_start = fc["start"]
            if fc_start >= df.index[-1]:
                if fc_start == df.index[-1]:
                    df.loc[fc_start, "high"] = max(df.loc[fc_start, "high"], fc["high"])
                    df.loc[fc_start, "low"] = min(df.loc[fc_start, "low"], fc["low"])
                    df.loc[fc_start, "close"] = fc["close"]
                else:
                    new_row = pd.DataFrame([fc], index=pd.DatetimeIndex([fc_start]))
                    df = pd.concat([df, new_row])
    return df


def calculate_option_stops(
    entry_option: float,
    entry_spot: float,
    stop_spot: float,
    target_spot: float,
    option_delta: float = 0.5,
    stop_mode: str = "Delta-Translated",
    option_atr: Optional[float] = None,
) -> tuple[float, float]:
    """Compute precision option stop-loss (`SL_opt`) and take-profit (`TP_opt`)."""
    if entry_option <= 0:
        return 0.05, 0.10

    if stop_mode == "Option-ATR" and option_atr and option_atr > 0:
        sl_opt = max(0.05, entry_option - (1.5 * option_atr))
        tp_opt = entry_option + (3.0 * option_atr)
        return round(sl_opt, 2), round(tp_opt, 2)

    # Delta-Translated calculation
    # Ensure delta is within realistic bounds for ATM options (0.3 to 0.7)
    delta = abs(option_delta) if option_delta and 0.1 <= abs(option_delta) <= 0.95 else 0.5
    spot_sl_dist = abs(entry_spot - stop_spot)
    spot_tp_dist = abs(target_spot - entry_spot)

    opt_sl_dist = spot_sl_dist * delta
    opt_tp_dist = spot_tp_dist * delta

    # Prevent stop from wiping more than 60% of option premium
    max_risk = entry_option * 0.60
    if opt_sl_dist > max_risk:
        opt_sl_dist = max_risk

    sl_opt = max(0.05, entry_option - opt_sl_dist)
    tp_opt = entry_option + opt_tp_dist
    return round(sl_opt, 2), round(tp_opt, 2)


def execute_option_trade(
    underlying_symbol: str,
    spot_price: float,
    direction: str,  # "LONG" or "SHORT"
    quantity: int,
    stop_spot: float,
    target_spot: float,
    timeframe: str = "15m",
    stop_mode: str = "Delta-Translated",
    tag: str = "",
) -> dict[str, Any]:
    """Execute live option entry (`CE`/`PE`) via Upstox when an underlying intraday signal triggers.

    Returns complete option execution details and P&L state.
    """
    contract = resolve_atm_option(underlying_symbol, spot_price, direction)
    if not contract:
        logger.warning("No active option contract found for %s @ %s (%s)", underlying_symbol, spot_price, direction)
        return {
            "status": False,
            "message": f"Option contract resolution failed for {underlying_symbol} @ {spot_price}",
        }

    inst_key = contract.get("instrument_key", "")
    tsym = contract.get("tradingsymbol", "")
    strike = float(contract.get("strike", 0.0))
    opt_type = contract.get("option_type", "")

    # Fetch live quote or latest historical bar to get option LTP and Delta
    client = get_upstox_client()
    quotes = client.get_quote([inst_key])
    q_data = quotes.get(inst_key, {})
    
    # Try feed if quote empty
    if not q_data:
        q_data = get_upstox_feed().get_option_quote(inst_key) or {}

    entry_opt = float(q_data.get("last_price") or q_data.get("ltp") or 0.0)
    delta = float(q_data.get("option_greeks", {}).get("delta") or q_data.get("greeks", {}).get("delta") or 0.5)

    # If real-time quote unavailable, get last bar close from history
    option_atr = None
    if entry_opt <= 0:
        df_opt = fetch_option_ohlcv(inst_key, timeframe, lookback_days=10)
        if df_opt is not None and not df_opt.empty:
            entry_opt = float(df_opt.iloc[-1]["close"])
            if len(df_opt) >= 14:
                # Compute ATR(14)
                tr = pd.concat([
                    df_opt["high"] - df_opt["low"],
                    (df_opt["high"] - df_opt["close"].shift()).abs(),
                    (df_opt["low"] - df_opt["close"].shift()).abs(),
                ], axis=1).max(axis=1)
                option_atr = float(tr.rolling(14).mean().iloc[-1])

    if entry_opt <= 0:
        return {
            "status": False,
            "message": f"Could not determine option entry price for {tsym} ({inst_key})",
        }

    sl_opt, tp_opt = calculate_option_stops(
        entry_option=entry_opt,
        entry_spot=spot_price,
        stop_spot=stop_spot,
        target_spot=target_spot,
        option_delta=delta,
        stop_mode=stop_mode,
        option_atr=option_atr,
    )

    # Execute via MultiBrokerDispatcher directly to Upstox
    dispatcher = get_dispatcher()
    # Note: When taking LONG underlying (`CE`) or SHORT underlying (`PE`), we BUY the option contract!
    oms_res = dispatcher.place_bracket_order(
        symbol=tsym,
        transaction_type="BUY",
        quantity=quantity,
        entry_price=entry_opt,
        stop_loss=sl_opt,
        take_profit=tp_opt,
        tag=tag or f"OPT-{direction[:1]}-{uuid.uuid4().hex[:6]}",
        broker="upstox",
    )

    return {
        "status": oms_res.get("status", True),
        "message": oms_res.get("message", "Option order processed"),
        "data": oms_res.get("data", {}),
        "option_contract": {
            "instrument_key": inst_key,
            "tradingsymbol": tsym,
            "strike": strike,
            "option_type": opt_type,
            "expiry": contract.get("expiry", ""),
        },
        "option_pricing": {
            "entry_price": entry_opt,
            "stop_loss": sl_opt,
            "take_profit": tp_opt,
            "delta_used": delta,
        },
    }
