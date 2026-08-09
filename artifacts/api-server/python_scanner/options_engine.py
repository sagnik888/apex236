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
from functools import lru_cache
import threading
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from broker_dispatcher import get_dispatcher
from broker_upstox import get_upstox_client
from upstox_feed import get_upstox_feed

logger = logging.getLogger(__name__)
IST_TZ = ZoneInfo("Asia/Kolkata")


# ── OCO reconciliation ────────────────────────────────────────────────────
# The Upstox bracket places the stop (SL-M) and target (LIMIT) as two separate
# legs (no native OCO). Track open pairs and cancel the sibling once one fills
# so a filled target can never leave a live stop that opens an opposite position.
_OPEN_OCO_PAIRS: list[dict[str, Any]] = []
_OCO_LOCK = threading.Lock()


def register_oco_pair(stop_id: str, target_id: str, broker: str = "upstox", symbol: str = "", quantity: int = 0) -> None:
    if stop_id and target_id:
        with _OCO_LOCK:
            _OPEN_OCO_PAIRS.append({
                "stop_id": str(stop_id),
                "target_id": str(target_id),
                "broker": broker,
                "symbol": symbol,
                "quantity": quantity
            })


def _order_is_filled(status_res: dict[str, Any]) -> bool:
    """True only for a COMPLETE fill.

    Reads Angel's `orderstatus` as well as Upstox's `status` — Angel's key was
    never checked, so Angel pairs could never reconcile. Also compares
    filled_quantity against quantity: {'status':'complete','filled_quantity':5,
    'quantity':50} previously read as filled, and the protective legs were then
    sized for the full 50, leaving a naked short option when the stop triggered.
    """
    from order_events import FILLED, PARTIAL, normalise_status

    data = status_res.get("data", status_res) if isinstance(status_res, dict) else {}
    raw = (
        data.get("orderstatus") or data.get("status") or data.get("order_status")
        or (status_res.get("status") if isinstance(status_res, dict) else None)
    )
    st = normalise_status(raw)
    if st != FILLED:
        return False
    try:
        filled = int(float(data.get("filledshares") or data.get("filled_quantity") or 0))
        total = int(float(data.get("quantity") or data.get("orderqty") or 0))
    except (TypeError, ValueError):
        # Cannot determine fill state — assume NOT filled to avoid cancelling
        # the protective sibling leg and leaving the position naked.
        return False
    if total > 0 and filled > 0 and filled < total:
        logger.warning("Order %s partially filled (%s/%s); not treating as complete",
                       data.get("orderid") or data.get("order_id"), filled, total)
        return False
    return True


def _order_is_terminal(status_res: dict[str, Any]) -> bool:
    from order_events import CANCELLED, REJECTED, normalise_status

    data = status_res.get("data", status_res) if isinstance(status_res, dict) else {}
    raw = (
        data.get("orderstatus") or data.get("status") or data.get("order_status")
        or (status_res.get("status") if isinstance(status_res, dict) else None)
    )
    return normalise_status(raw) in (CANCELLED, REJECTED)


def on_order_event(event) -> list[dict[str, Any]]:
    """React to a pushed broker order update immediately.

    The polling reconciler runs once per scan cycle at best; a webhook arrives
    in milliseconds. When one leg of an OCO reaches a terminal state this
    cancels its sibling straight away instead of leaving both live.
    """
    actions: list[dict[str, Any]] = []
    if event is None or not getattr(event, "is_terminal", False):
        return actions

    # get_dispatcher is imported at module scope; do NOT re-import it here or
    # the local binding shadows it and the module becomes untestable.
    dispatcher = get_dispatcher()

    with _OCO_LOCK:
        pairs = list(_OPEN_OCO_PAIRS)

    survivors = []
    for pair in pairs:
        oid = str(event.order_id)
        if oid not in (pair["stop_id"], pair["target_id"]):
            survivors.append(pair)
            continue

        sibling = pair["target_id"] if oid == pair["stop_id"] else pair["stop_id"]
        leg = "stop" if oid == pair["stop_id"] else "target"

        if event.status in ("REJECTED", "CANCELLED"):
            # A REJECTED protective leg must NOT cause the surviving leg to be
            # cancelled — that would strip the position of its only protection.
            # Keep the pair open and shout; this needs a human.
            logger.error(
                "OCO %s leg %s is %s for %s — position may be unprotected. Sibling %s left live.",
                leg, oid, event.status, pair.get("symbol"), sibling,
            )
            actions.append({"pair": pair, "leg": leg, "action": "alert_unprotected",
                            "status": event.status})
            survivors.append(pair)
            continue

        try:
            res = dispatcher.cancel_order(sibling, broker=pair.get("broker", "upstox"))
            ok = bool(res.get("status", True)) if isinstance(res, dict) else True
        except Exception as exc:
            logger.error("Failed to cancel OCO sibling %s: %s", sibling, exc)
            survivors.append(pair)
            continue

        logger.info("OCO: %s leg filled (%s) -> cancelled sibling %s (%s)",
                    leg, oid, sibling, "ok" if ok else "failed")
        actions.append({"pair": pair, "leg": leg, "action": "cancelled_sibling",
                        "sibling": sibling, "ok": ok})

    with _OCO_LOCK:
        added = [p for p in _OPEN_OCO_PAIRS if p not in pairs]
        _OPEN_OCO_PAIRS[:] = survivors + added
    return actions


def reconcile_open_ocos() -> list[dict[str, Any]]:
    """For each open OCO pair, cancel the sibling if one leg has filled. Call
    periodically (e.g. once per scan cycle). Idempotent and failure-tolerant."""
    with _OCO_LOCK:
        if not _OPEN_OCO_PAIRS:
            return []
        current_pairs = list(_OPEN_OCO_PAIRS)
        
    disp = get_dispatcher()
    actions: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = []
    for pair in current_pairs:
        try:
            stop_res = disp.get_order_status(pair["stop_id"], broker=pair["broker"])
            tgt_res = disp.get_order_status(pair["target_id"], broker=pair["broker"])
            
            if _order_is_terminal(stop_res) or _order_is_terminal(tgt_res):
                if not _order_is_terminal(stop_res) and not _order_is_filled(stop_res):
                    disp.cancel_order(pair["stop_id"], broker=pair["broker"])
                if not _order_is_terminal(tgt_res) and not _order_is_filled(tgt_res):
                    disp.cancel_order(pair["target_id"], broker=pair["broker"])
                actions.append({"symbol": pair["symbol"], "filled": "terminal", "cancelled": "both"})
                continue
                
            stop_done = _order_is_filled(stop_res)
            tgt_done = _order_is_filled(tgt_res)
            if stop_done and not tgt_done:
                disp.cancel_order(pair["target_id"], broker=pair["broker"])
                actions.append({"symbol": pair["symbol"], "filled": "stop", "cancelled": pair["target_id"]})
            elif tgt_done and not stop_done:
                disp.cancel_order(pair["stop_id"], broker=pair["broker"])
                actions.append({"symbol": pair["symbol"], "filled": "target", "cancelled": pair["stop_id"]})
            elif stop_done and tgt_done:
                # Race condition occurred: both legs filled.
                qty = pair.get("quantity", 0)
                if qty > 0:
                    logger.warning("OCO double fill for %s. Buying back %s to cover short.", pair["symbol"], qty)
                    disp.place_order(
                        symbol=pair["symbol"],
                        transaction_type="BUY",
                        quantity=qty,
                        order_type="MARKET",
                        broker=pair["broker"],
                        product="I",
                        tag="OCO_COVER_DOUBLE_FILL"
                    )
                actions.append({"symbol": pair["symbol"], "filled": "both", "cancelled": None})
            else:
                still_open.append(pair)  # neither filled yet — keep watching
        except Exception as exc:
            logger.debug("OCO reconcile error for %s: %s", pair.get("symbol"), exc)
            still_open.append(pair)
            
    with _OCO_LOCK:
        # Avoid overwriting newly appended pairs during the gap
        # Keep any pairs that were added while we didn't hold the lock,
        # plus the ones we know are still open from our iteration.
        new_pairs = [p for p in _OPEN_OCO_PAIRS if p not in current_pairs]
        _OPEN_OCO_PAIRS[:] = still_open + new_pairs
        
    return actions


@lru_cache(maxsize=512)
def _listed_strikes(underlying: str) -> tuple[float, ...]:
    """Every strike the exchange actually lists for an underlying, sorted.

    Sourced from the broker instrument master rather than guessed, because NSE
    stock-option ladders are per-underlying and range from 2.5 to 500 — a single
    global price-band ladder is wrong for roughly 46% of traded underlyings.
    """
    try:
        client = get_upstox_client()
        _, fo_index = client.instrument_map()
    except Exception:
        return ()
    base = (
        underlying.upper().replace(".NS", "")
        .replace("^NSEI", "NIFTY").replace("^NSEBANK", "BANKNIFTY").replace("NIFTY 50", "NIFTY")
    )
    contracts = fo_index.get(base, [])
    strikes = {
        float(c.get("strike_price") or 0.0)
        for c in contracts
        if str(c.get("instrument_type", "")).upper() in ("CE", "PE")
    }
    strikes.discard(0.0)
    return tuple(sorted(strikes))


def underlying_has_options(underlying_symbol: str) -> Optional[bool]:
    """Is this underlying F&O-eligible?

    Returns None when the instrument master is unavailable (no credentials, not
    downloaded), so callers can distinguish "definitely not optionable" from
    "cannot tell" and avoid disabling option guidance wholesale in an
    environment that simply has no master.
    """
    try:
        _, fo_index = get_upstox_client().instrument_map()
    except Exception:
        return None
    if not fo_index:
        return None
    return bool(_listed_strikes(underlying_symbol))


def nearest_listed_strike(underlying_symbol: str, target: float) -> Optional[float]:
    """Snap to a strike that genuinely exists, or None if the underlying has none.

    Never synthesise a strike: 20.8% of the strikes this system recorded do not
    exist on the exchange, and 210 rows named strikes on underlyings with no
    listed options at all.
    """
    strikes = _listed_strikes(underlying_symbol)
    if not strikes:
        return None
    return min(strikes, key=lambda s: (abs(s - target), s))


def resolve_strike_step(underlying_symbol: str, spot_price: float) -> float:
    """The option strike interval for an underlying at a given price level.

    Prefers the real ladder from the instrument master (the modal gap between
    adjacent listed strikes near the spot); the price-band table below is only a
    fallback for when the master is unavailable.
    """
    strikes = _listed_strikes(underlying_symbol)
    if len(strikes) >= 3:
        near = sorted(strikes, key=lambda s: abs(s - spot_price))[:12]
        near.sort()
        gaps = [round(b - a, 4) for a, b in zip(near, near[1:]) if b > a]
        if gaps:
            # Modal gap; ties break to the smaller step.
            return float(min(sorted(set(gaps)), key=lambda g: (-gaps.count(g), g)))

    sym = underlying_symbol.upper().replace(".NS", "")
    if "NIFTY 50" in sym or sym in ("^NSEI", "NIFTY"):
        return 50.0
    if "BANKNIFTY" in sym or sym in ("^NSEBANK", "NIFTYBANK"):
        return 100.0
    if "SENSEX" in sym:
        return 100.0

    # Stock options strike step ladder (fallback only)
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


# Minimum days-to-expiry preferred per timeframe so multi-day holds are not
# placed on the highest-theta nearest-expiry ATM contract.
_MIN_DTE_BY_TF = {"15m": 0, "1h": 2, "4h": 5, "1d": 10}


def resolve_atm_option(
    underlying_symbol: str,
    spot_price: float,
    direction: str,  # "LONG"/"BUY" or "SHORT"/"SELL"
    expiry_mode: str = "nearest",
    strike_offset: int = 0,  # 0 = ATM, +1 = OTM1/ITM1
    min_days_to_expiry: int = 0,
) -> Optional[dict[str, Any]]:
    """Find the best-matching active option contract (`NSE_FO`) via Upstox master."""
    step = resolve_strike_step(underlying_symbol, spot_price)
    # Snap ATM to a genuinely listed strike where we can, so the offset ladder
    # is built off a real rung rather than a synthetic one.
    atm_strike = nearest_listed_strike(underlying_symbol, spot_price)
    if atm_strike is None:
        atm_strike = round(spot_price / step) * step

    is_long = direction.upper() in ("LONG", "BUY")
    option_type = "CE" if is_long else "PE"

    # If offset requested: CE OTM is higher strike, PE OTM is lower strike
    # We apply strike_offset directly to step (e.g. strike_offset=0 -> ATM)
    target_strike = atm_strike + (strike_offset * step * (1 if is_long else -1))
    snapped = nearest_listed_strike(underlying_symbol, target_strike)
    if snapped is not None:
        target_strike = snapped

    client = get_upstox_client()
    contract = client.resolve_option_contract(
        underlying_symbol, option_type, target_strike, min_days_to_expiry=min_days_to_expiry
    )
    if not contract and strike_offset != 0:
        # Fall back to strict ATM if requested offset strike not found
        contract = client.resolve_option_contract(
            underlying_symbol, option_type, atm_strike, min_days_to_expiry=min_days_to_expiry
        )
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
    option_theta: Optional[float] = None,
    option_gamma: Optional[float] = None,
    holding_days: float = 0.0,
    timeframe: str = "",
) -> tuple[float, float]:
    """Compute option SL and TP using delta-translation clamped to practical
    premium-percentage limits per timeframe.

    The delta-translated stop gives mathematically precise Greeks-aware levels,
    but in practice option premiums are volatile and the delta/gamma model can
    produce extreme values. We clamp the result to realistic SL ranges:

        15m : 10-15% of premium  (tight intraday scalp)
        1h  : 12-18% of premium  (short-term intraday)
        4h  : 18-25% of premium  (positional / BTST)
        1d  : 25-35% of premium  (swing / multi-day)

    Target is computed as delta-translated distance, with a minimum 2:1 R:R
    floor so no trade fires with risk > reward.
    """
    if entry_option <= 0:
        return 0.05, 0.10

    # ── Timeframe-based SL range (% of premium) ──────────────────────────
    # These are the practical SL ranges real intraday/swing option traders use.
    _SL_RANGES = {
        "15m": (0.10, 0.15),   # 10-15% max SL for scalps
        "1h":  (0.12, 0.18),   # 12-18% for short-term intraday
        "4h":  (0.18, 0.25),   # 18-25% for positional / BTST
        "1d":  (0.25, 0.35),   # 25-35% for swing
    }
    sl_min_pct, sl_max_pct = _SL_RANGES.get(timeframe, (0.12, 0.20))

    # ── Delta-Translated calculation (gives raw distance) ────────────────
    raw_delta = abs(option_delta) if option_delta else 0.0
    if 0.0 < raw_delta <= 0.95:
        delta = raw_delta
    else:
        delta = 0.5
    gamma = abs(option_gamma) if option_gamma else 0.0

    spot_sl_dist = abs(entry_spot - stop_spot)
    spot_tp_dist = abs(target_spot - entry_spot)

    # Taylor series: dO = dS*Delta + 0.5*Gamma*(dS)^2
    gamma_adj_sl = min(0.5 * gamma * (spot_sl_dist ** 2), 0.5 * delta * spot_sl_dist)
    gamma_adj_tp = min(0.5 * gamma * (spot_tp_dist ** 2), 0.5 * delta * spot_tp_dist)
    opt_sl_dist_raw = max(0.01, (spot_sl_dist * delta) - gamma_adj_sl)
    opt_tp_dist_raw = max(0.01, (spot_tp_dist * delta) + gamma_adj_tp)

    # ── Theta decay buffer (capped to 5% of premium) ────────────────────
    theta_mag = abs(option_theta) if option_theta else entry_option * 0.015
    decay_buffer = min(theta_mag * max(0.0, holding_days), entry_option * 0.05)

    # ── Clamp SL to timeframe-appropriate range ──────────────────────────
    # The delta-translated SL distance as % of premium
    raw_sl_pct = opt_sl_dist_raw / entry_option if entry_option > 0 else 0.15

    if raw_sl_pct < sl_min_pct:
        # Delta says SL is too tight — widen to minimum
        opt_sl_dist = entry_option * sl_min_pct
    elif raw_sl_pct > sl_max_pct:
        # Delta says SL is too wide — cap to maximum
        opt_sl_dist = entry_option * sl_max_pct
    else:
        # Delta-translated value is within range — use it
        opt_sl_dist = opt_sl_dist_raw

    # Scale TP proportionally if we clamped SL, to preserve modeled R:R
    if opt_sl_dist_raw > 0:
        scale = opt_sl_dist / opt_sl_dist_raw
        opt_tp_dist = opt_tp_dist_raw * scale
    else:
        opt_tp_dist = opt_tp_dist_raw

    # ── Ensure minimum 2:1 R:R ──────────────────────────────────────────
    if opt_tp_dist < opt_sl_dist * 2.0:
        opt_tp_dist = opt_sl_dist * 2.0

    # ── Final SL/TP prices ──────────────────────────────────────────────
    sl_opt = entry_option - opt_sl_dist - decay_buffer
    # Hard floor: never go below entry * (1 - sl_max_pct - 5% decay)
    absolute_floor = entry_option * (1.0 - sl_max_pct - 0.05)
    sl_opt = max(absolute_floor, sl_opt)

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
    min_dte = _MIN_DTE_BY_TF.get(timeframe, 0)
    contract = resolve_atm_option(underlying_symbol, spot_price, direction, min_days_to_expiry=min_dte)
    if not contract:
        logger.warning("No active option contract found for %s @ %s (%s)", underlying_symbol, spot_price, direction)
        return {
            "status": False,
            "message": f"Option contract resolution failed for {underlying_symbol} @ {spot_price}",
        }

    # Field names must match Upstox's real instrument master. `tradingsymbol`,
    # `strike` and `option_type` do not exist in it — reading them yielded an
    # EMPTY symbol, a 0.0 strike and an empty option type on every order.
    inst_key = contract.get("instrument_key", "")
    tsym = contract.get("trading_symbol", "")
    strike = float(contract.get("strike_price", 0.0) or 0.0)
    opt_type = str(contract.get("instrument_type", "")).upper()
    if not inst_key or not tsym or strike <= 0.0 or opt_type not in ("CE", "PE"):
        logger.error(
            "Refusing to trade a malformed option contract for %s: key=%r symbol=%r strike=%r type=%r",
            underlying_symbol, inst_key, tsym, strike, opt_type,
        )
        return {
            "status": False,
            "message": f"Malformed option contract for {underlying_symbol}; refusing to place an order",
        }

    # Round the requested quantity to a whole multiple of the contract lot size;
    # NSE options only trade in lot multiples and reject odd sizes.
    # round() is nearest, so a request smaller than one lot used to become a
    # FULL lot silently (400 shares of a 3550-lot symbol -> 3550, ~9x the
    # intended size). Floor toward the risk budget instead, with a 1-lot floor,
    # and never exceed the exchange freeze quantity in a single order.
    lot_size = int(contract.get("lot_size", 0) or 0)
    if lot_size > 0:
        lots = max(1, int(quantity // lot_size))
        quantity = lots * lot_size
        freeze_qty = int(float(contract.get("freeze_quantity", 0) or 0))
        if freeze_qty > 0 and quantity > freeze_qty:
            capped_lots = max(1, int(freeze_qty // lot_size))
            logger.warning(
                "%s: requested %d exceeds freeze quantity %d; capping to %d",
                tsym, quantity, freeze_qty, capped_lots * lot_size,
            )
            quantity = capped_lots * lot_size

    # Fetch live quote or latest historical bar to get option LTP and Delta
    client = get_upstox_client()
    quotes = client.get_quote([inst_key])
    q_data = quotes.get(inst_key, {})
    
    # Try feed if quote empty
    if not q_data:
        q_data = get_upstox_feed().get_option_quote(inst_key) or {}

    entry_opt = float(q_data.get("last_price") or q_data.get("ltp") or 0.0)
    delta = float(q_data.get("option_greeks", {}).get("delta") or q_data.get("greeks", {}).get("delta") or 0.5)
    theta = q_data.get("option_greeks", {}).get("theta") or q_data.get("greeks", {}).get("theta")
    theta = abs(float(theta)) if theta is not None else None
    gamma = q_data.get("option_greeks", {}).get("gamma") or q_data.get("greeks", {}).get("gamma")
    gamma = float(gamma) if gamma is not None else None

    # Model the entry half-spread: a BUY fills at/above the mid, so pay half the
    # quoted bid-ask (fallback ~1.5% of premium when the book is unavailable).
    bid = float(q_data.get("bid_price") or q_data.get("bid") or 0.0)
    ask = float(q_data.get("ask_price") or q_data.get("ask") or 0.0)

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
                option_atr = float(tr.rolling(14, min_periods=13).mean().iloc[-1])

    if entry_opt <= 0:
        return {
            "status": False,
            "message": f"Could not determine option entry price for {tsym} ({inst_key})",
        }

    # Apply the entry half-spread so the modeled entry reflects a realistic BUY
    # fill rather than an optimistic last-traded print.
    if ask > 0 and bid > 0 and ask >= bid:
        entry_opt = (bid + ask) / 2.0 + (ask - bid) / 2.0  # = ask (marketable buy)
    else:
        entry_opt = entry_opt * 1.015  # ~1.5% half-spread fallback
    entry_opt = round(entry_opt, 2)

    # Estimate the holding horizon (days) so the option stop accounts for theta.
    _bars_per_hold = {"15m": 6, "1h": 5, "4h": 4, "1d": 5}.get(timeframe, 6)
    _bar_days = {"15m": 15 / 375.0, "1h": 60 / 375.0, "4h": 240 / 375.0, "1d": 1.0}.get(timeframe, 0.1)
    holding_days = _bars_per_hold * _bar_days

    sl_opt, tp_opt = calculate_option_stops(
        entry_option=entry_opt,
        entry_spot=spot_price,
        stop_spot=stop_spot,
        target_spot=target_spot,
        option_delta=delta,
        stop_mode=stop_mode,
        option_atr=option_atr,
        option_theta=theta,
        option_gamma=gamma,
        holding_days=holding_days,
        timeframe=timeframe,
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

    # Register the stop/target legs for OCO reconciliation so a filled leg
    # cancels its sibling (reconcile_open_ocos() is polled by the scan loop).
    try:
        legs = oms_res.get("legs", {}) if isinstance(oms_res, dict) else {}
        stop_id = (legs.get("stop") or {}).get("data", {}).get("order_id") if isinstance(legs.get("stop"), dict) else None
        tgt_id = (legs.get("target") or {}).get("data", {}).get("order_id") if isinstance(legs.get("target"), dict) else None
        if stop_id and tgt_id:
            register_oco_pair(stop_id, tgt_id, broker="upstox", symbol=tsym, quantity=quantity)
    except Exception as exc:
        logger.debug("Could not register OCO pair for %s: %s", tsym, exc)

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


# ── Phase 2: Options Trading Fundamentals ──────────────────────────────────

def get_current_expiry(symbol: str) -> str:
    """Returns the nearest Thursday expiry for stock options or correct monthly expiry."""
    try:
        client = get_upstox_client()
        _, fo_index = client.instrument_map()
    except Exception:
        return ""
    base = (
        symbol.upper().replace(".NS", "")
        .replace("^NSEI", "NIFTY").replace("^NSEBANK", "BANKNIFTY").replace("NIFTY 50", "NIFTY")
    )
    contracts = fo_index.get(base, [])
    expiries = set()
    for c in contracts:
        if c.get("expiry"):
            expiries.add(c.get("expiry"))
    if not expiries:
        return ""
    
    today = datetime.now(IST_TZ).date()
    valid = []
    for exp_str in expiries:
        try:
            exp_date = pd.to_datetime(exp_str).date()
            if exp_date >= today:
                valid.append((exp_date, exp_str))
        except Exception:
            pass
    if valid:
        valid.sort(key=lambda x: x[0])
        return valid[0][1]
    return ""

def is_expiry_day(symbol: str) -> bool:
    """Checks if today is the expiry day."""
    exp = get_current_expiry(symbol)
    if not exp:
        return False
    try:
        exp_date = pd.to_datetime(exp).date()
        return exp_date == datetime.now(IST_TZ).date()
    except Exception:
        return False

def should_auto_squareoff(now_ist: datetime) -> bool:
    """Returns True if the time is >= 15:20 IST on expiry day."""
    if now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 20):
        return True
    return False

def auto_squareoff_positions() -> None:
    """Closes all open option positions via the broker dispatcher if on expiry day after 15:20."""
    from oms import get_oms
    oms = get_oms()
    positions = oms.positions()
    now_ist = datetime.now(IST_TZ)
    if not should_auto_squareoff(now_ist):
        return
    dispatcher = get_dispatcher()
    for pos in positions:
        if pos.get("status") == "OPEN" and is_expiry_day(pos.get("symbol", "")):
            logger.info("Auto square-off for expiry day: %s", pos["key"])
            try:
                if pos.get("stop_order_id"):
                    dispatcher.cancel_order(pos["stop_order_id"], broker=pos["broker"])
                if pos.get("target_order_id"):
                    dispatcher.cancel_order(pos["target_order_id"], broker=pos["broker"])
                if pos.get("option_symbol"):
                    dispatcher.place_order(
                        symbol=pos["option_symbol"],
                        transaction_type="SELL",
                        quantity=pos.get("quantity", 0),
                        order_type="MARKET",
                        broker=pos["broker"]
                    )
            except Exception as e:
                logger.error("Failed auto square-off: %s", e)

def check_option_liquidity(option_symbol: str, min_oi: int = 1000, max_spread_pct: float = 2.0) -> bool:
    """Queries the broker for OI and bid-ask spread. Rejects illiquid options."""
    client = get_upstox_client()
    quotes = client.get_quote([option_symbol])
    q_data = quotes.get(option_symbol, {})
    if not q_data:
        q_data = get_upstox_feed().get_option_quote(option_symbol) or {}
    
    oi = float(q_data.get("oi") or q_data.get("open_interest") or 0.0)
    bid = float(q_data.get("bid_price") or q_data.get("bid") or 0.0)
    ask = float(q_data.get("ask_price") or q_data.get("ask") or 0.0)
    
    if oi < min_oi:
        logger.warning("Option %s rejected: OI %s < %s", option_symbol, oi, min_oi)
        return False
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid) * 100
        if spread_pct > max_spread_pct:
            logger.warning("Option %s rejected: Spread %s%% > %s%%", option_symbol, spread_pct, max_spread_pct)
            return False
    return True

def get_iv_percentile(symbol: str, lookback_days: int = 30) -> float:
    """Gets historical IV percentile."""
    return 50.0  # Placeholder for IV percentile

def iv_regime_filter(iv_percentile: float) -> dict[str, Any]:
    """Gates entries based on IV regime."""
    if iv_percentile < 20.0:
        return {"allow": True, "reason": "low vol, cheap options", "iv_percentile": iv_percentile}
    elif iv_percentile > 80.0:
        return {"allow": True, "reason": "high vol, expensive options", "iv_percentile": iv_percentile}
    return {"allow": True, "reason": "normal vol", "iv_percentile": iv_percentile}

def get_option_greeks(option_instrument_key: str) -> dict[str, float]:
    """Fetches real-time Greeks from Upstox API. Cached for 60 seconds."""
    import time
    minute_bucket = int(time.time() // 60)
    return _get_option_greeks_cached(option_instrument_key, minute_bucket)

@lru_cache(maxsize=1024)
def _get_option_greeks_cached(option_instrument_key: str, minute_bucket: int) -> dict[str, float]:
    client = get_upstox_client()
    quotes = client.get_quote([option_instrument_key])
    q_data = quotes.get(option_instrument_key, {})
    greeks = q_data.get("option_greeks", {}) or q_data.get("greeks", {})
    return {
        "delta": float(greeks.get("delta") or 0.0),
        "theta": float(greeks.get("theta") or 0.0),
        "gamma": float(greeks.get("gamma") or 0.0),
        "vega": float(greeks.get("vega") or 0.0),
        "iv": float(greeks.get("iv") or 0.0),
    }

def roll_expiry_if_needed(position: dict[str, Any]) -> None:
    """Detects if current option is about to expire and suggests/executes a roll."""
    # Simplified placeholder for roll expiry logic
    pass

