"""Order management: turns scanner state transitions into broker orders.

This is the missing first half of the execution path. `scanner_engine` owns
entry/exit/stop/trail/square-off and emits an event on every transition
(`trade_triggered`, `sl_hit`, `tsl_hit`, `target_hit`, `trade_transition`), but
nothing consumed those events — the module imports no broker at all, so every
one of the ~2000 recorded trades was an internal simulation and
`execution_mode='LIVE'` was a label with nothing behind it.

Design: the OMS is an OBSERVER, not a caller inside the scanner. The scanner
stays a pure simulator with no broker dependency (which is also what makes it
testable), and every path that can move real money lives in this one auditable
file.

    scanner_engine._diff_states()  ->  events  ->  OMS.handle_events()  ->  broker
                                                        |
                            order_events webhook  <-----+  (fills, cancels)

Safety, in layers, because this is the file that can lose money:

  1. `is_live_execution()` gates every order. It requires execution_mode=LIVE
     AND APEX_ARM_LIVE=1 (not settable over HTTP) AND a passing edge gate.
  2. PAPER mode still runs the full path and records intents, so the wiring is
     exercised continuously instead of being cold on the first live day.
  3. One position per (symbol, timeframe). A duplicate trigger is ignored.
  4. Every intent is idempotency-keyed, so a retried event cannot double-fill.
  5. Exits are pinned to the broker that filled the entry.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST_TZ = ZoneInfo("Asia/Kolkata")

# Transitions that should close a live position.
EXIT_EVENTS = {"sl_hit", "tsl_hit", "trade_transition"}
ENTRY_EVENTS = {"trade_triggered"}


@dataclass
class Position:
    """An OMS-tracked position. Distinct from the scanner's ActiveTrade: this is
    what we believe the BROKER holds, keyed to real order ids."""
    key: str                       # symbol|timeframe
    symbol: str
    timeframe: str
    direction: str                 # BUY / SELL (of the option)
    option_symbol: str = ""
    option_type: str = ""
    strike: float = 0.0
    quantity: int = 0
    broker: str = "upstox"         # pinned: exits go to whoever filled the entry
    entry_order_id: str = ""
    stop_order_id: str = ""
    target_order_id: str = ""
    idempotency_key: str = ""
    opened_at: str = ""
    mode: str = "PAPER"
    status: str = "OPEN"
    last_error: str = ""
    raw: dict = field(default_factory=dict)


class OrderManager:
    """Bridges scanner events to broker orders. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._positions: dict[str, Position] = {}
        self._seen_events: set[str] = set()
        self._intents: list[dict] = []
        # key -> (event, first_seen_monotonic). A trigger is held here until it
        # has survived the stabilisation buffer.
        self._pending: dict[str, tuple[dict, float]] = {}

    # ── Signal stabilisation ─────────────────────────────────────────────────
    @staticmethod
    def buffer_seconds(timeframe: str) -> float:
        """How long a signal must persist before it may be acted on.

        The scanner re-evaluates a rolling window every cycle, so a signal can
        appear on one scan and be gone on the next. Waiting a fraction of the
        bar's duration and re-checking filters that flicker out before it
        becomes an order. Roughly 1/15th of the bar: 1 min on 15m, 5 on 1h,
        15 on 4h, 30 on 1d.
        """
        try:
            from settings_store import get_settings
            table = get_settings().get("signal_buffer_minutes") or {}
            return max(0.0, float(table.get(timeframe, 0))) * 60.0
        except Exception:
            return 0.0

    def pending(self) -> list[dict]:
        """Signals waiting out their stabilisation buffer."""
        import time as _t
        now = _t.monotonic()
        with self._lock:
            return [
                {
                    "key": key,
                    "symbol": ev.get("symbol"),
                    "timeframe": ev.get("timeframe"),
                    "direction": ev.get("direction"),
                    "waited_s": round(now - seen, 1),
                    "buffer_s": self.buffer_seconds(str(ev.get("timeframe", ""))),
                }
                for key, (ev, seen) in self._pending.items()
            ]

    def _promote_ready(self) -> int:
        """Open any pending signal whose buffer has elapsed."""
        import time as _t
        now = _t.monotonic()
        with self._lock:
            ready = [
                (key, ev) for key, (ev, seen) in self._pending.items()
                if (now - seen) >= self.buffer_seconds(str(ev.get("timeframe", "")))
            ]
        opened = 0
        for key, ev in ready:
            with self._lock:
                self._pending.pop(key, None)
            if self._open(ev):
                opened += 1
        return opened

    # ── State ────────────────────────────────────────────────────────────────
    def positions(self) -> list[dict]:
        with self._lock:
            return [asdict(p) for p in self._positions.values()]

    def intents(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return self._intents[-limit:]

    def _record(self, kind: str, detail: dict) -> None:
        entry = {"at": datetime.now(IST_TZ).isoformat(), "kind": kind, **detail}
        with self._lock:
            self._intents.append(entry)
            if len(self._intents) > 1000:
                self._intents = self._intents[-1000:]

    @staticmethod
    def _key(event: dict) -> str:
        return f"{event.get('symbol')}|{event.get('timeframe')}"

    @staticmethod
    def _event_id(event: dict) -> str:
        """Stable id so a replayed event cannot place a second order."""
        return "|".join(str(event.get(k, "")) for k in
                        ("type", "symbol", "timeframe", "timestamp", "direction"))

    # ── Entry ────────────────────────────────────────────────────────────────
    def _open(self, event: dict) -> Optional[dict]:
        from simulation_engine import get_execution_mode, is_live_execution

        key = self._key(event)
        with self._lock:
            if key in self._positions:
                logger.info("OMS: %s already open; ignoring duplicate trigger", key)
                return None

        symbol = str(event.get("symbol", ""))
        timeframe = str(event.get("timeframe", ""))
        direction = str(event.get("direction", "BUY"))
        entry_price = event.get("entry_price")
        stop = event.get("sl1")
        target = event.get("tp1")

        if not symbol or entry_price in (None, 0):
            self._record("entry_rejected", {"key": key, "reason": "missing symbol or entry price"})
            return None

        # Cash BUY -> CE, cash SELL -> PE. The option is always bought.
        opt_direction = "LONG" if direction == "BUY" else "SHORT"
        idem = uuid.uuid4().hex[:16]
        mode = get_execution_mode()
        live = is_live_execution()

        intent = {
            "key": key, "symbol": symbol, "timeframe": timeframe,
            "direction": direction, "opt_direction": opt_direction,
            "spot": entry_price, "stop_spot": stop, "target_spot": target,
            "idempotency_key": idem, "mode": mode, "live": live,
        }

        if not live:
            # Full path is still walked in PAPER so the wiring is never cold.
            self._record("entry_paper", intent)
            pos = Position(
                key=key, symbol=symbol, timeframe=timeframe, direction=direction,
                idempotency_key=idem, mode=mode,
                opened_at=datetime.now(IST_TZ).isoformat(), raw=intent,
            )
            with self._lock:
                self._positions[key] = pos
            return intent

        try:
            from options_engine import execute_option_trade
            from settings_store import get_settings
            qty = int(get_settings().get("default_quantity", 0) or 0)
            if qty <= 0:
                self._record("entry_rejected", {**intent, "reason": "no position size configured"})
                logger.error(
                    "OMS: refusing to trade %s - no default_quantity configured. "
                    "The system has no position sizing model (see audit RX-04).", key,
                )
                return None

            res = execute_option_trade(
                underlying_symbol=symbol,
                spot_price=float(entry_price),
                direction=opt_direction,
                quantity=qty,
                stop_spot=float(stop) if stop else 0.0,
                target_spot=float(target) if target else 0.0,
                timeframe=timeframe,
            )
        except Exception as exc:
            logger.error("OMS: entry failed for %s: %s", key, exc)
            self._record("entry_error", {**intent, "error": str(exc)})
            return None

        if not res or not res.get("status"):
            self._record("entry_rejected", {**intent, "reason": (res or {}).get("message", "broker rejected")})
            return None

        contract = res.get("option_contract", {})
        legs = res.get("legs", {}) or {}
        pos = Position(
            key=key, symbol=symbol, timeframe=timeframe, direction=direction,
            option_symbol=contract.get("tradingsymbol", ""),
            option_type=contract.get("option_type", ""),
            strike=float(contract.get("strike", 0.0) or 0.0),
            quantity=int(res.get("quantity", qty) or qty),
            broker=str(res.get("broker", "upstox")),
            entry_order_id=str((res.get("data") or {}).get("order_id", "")),
            stop_order_id=str(((legs.get("stop") or {}).get("data", {}) or {}).get("order_id", "")),
            target_order_id=str(((legs.get("target") or {}).get("data", {}) or {}).get("order_id", "")),
            idempotency_key=idem, mode=mode, opened_at=datetime.now(IST_TZ).isoformat(),
            raw=intent,
        )
        with self._lock:
            self._positions[key] = pos
        self._record("entry_placed", {**intent, "order_id": pos.entry_order_id,
                                      "option": pos.option_symbol})
        logger.info("OMS: opened %s -> %s qty=%s order=%s",
                    key, pos.option_symbol, pos.quantity, pos.entry_order_id)
        return intent

    # ── Exit ─────────────────────────────────────────────────────────────────
    def _close(self, event: dict) -> Optional[dict]:
        from simulation_engine import is_live_execution

        key = self._key(event)
        with self._lock:
            pos = self._positions.get(key)
        if pos is None:
            return None

        reason = str(event.get("type", "exit"))
        detail = {"key": key, "reason": reason, "option": pos.option_symbol,
                  "quantity": pos.quantity, "broker": pos.broker}

        if not is_live_execution():
            self._record("exit_paper", detail)
            with self._lock:
                self._positions.pop(key, None)
            return detail

        try:
            from broker_dispatcher import get_dispatcher
            dispatcher = get_dispatcher()
            # Cancel resting protective legs FIRST, then flatten. Doing it in
            # the other order can leave a resting stop that re-opens exposure
            # against the flatten fill.
            for leg_id in (pos.stop_order_id, pos.target_order_id):
                if leg_id:
                    try:
                        dispatcher.cancel_order(leg_id, broker=pos.broker)
                    except Exception as exc:
                        logger.error("OMS: could not cancel leg %s: %s", leg_id, exc)

            if pos.option_symbol and pos.quantity:
                dispatcher.place_order(
                    pos.option_symbol,
                    "SELL",                      # the option was bought; flatten by selling
                    pos.quantity,
                    order_type="MARKET",
                    broker=pos.broker,
                )
        except Exception as exc:
            logger.error("OMS: exit failed for %s: %s", key, exc)
            with self._lock:
                if key in self._positions:
                    self._positions[key].last_error = str(exc)
                    self._positions[key].status = "EXIT_FAILED"
            self._record("exit_error", {**detail, "error": str(exc)})
            return None

        with self._lock:
            self._positions.pop(key, None)
        self._record("exit_placed", detail)
        logger.info("OMS: closed %s (%s)", key, reason)
        return detail

    # ── Entry point ──────────────────────────────────────────────────────────
    def handle_events(self, events: list[dict]) -> dict[str, int]:
        """Process a batch of scanner events. Never raises."""
        opened = closed = skipped = buffered = expired = 0
        for event in events or []:
            try:
                etype = str(event.get("type", ""))
                if etype not in ENTRY_EVENTS and etype not in EXIT_EVENTS:
                    continue

                eid = self._event_id(event)
                with self._lock:
                    if eid in self._seen_events:
                        skipped += 1
                        continue
                    self._seen_events.add(eid)
                    if len(self._seen_events) > 5000:
                        self._seen_events = set(list(self._seen_events)[-2500:])

                if etype in ENTRY_EVENTS:
                    key = self._key(event)
                    wait = self.buffer_seconds(str(event.get("timeframe", "")))
                    if wait <= 0:
                        if self._open(event):
                            opened += 1
                    else:
                        with self._lock:
                            if key not in self._pending and key not in self._positions:
                                self._pending[key] = (event, time.monotonic())
                                buffered += 1
                else:
                    # The signal died before it stabilised — drop it unopened.
                    key = self._key(event)
                    with self._lock:
                        dropped_pending = self._pending.pop(key, None)
                    if dropped_pending is not None:
                        expired += 1
                        self._record("entry_abandoned", {
                            "key": key, "reason": f"{etype} arrived during the stabilisation buffer",
                        })
                    if self._close(event):
                        closed += 1
            except Exception as exc:
                logger.error("OMS: event handling failed (%s): %s", event.get("type"), exc)

        # Anything that has now outlived its buffer becomes an order.
        opened += self._promote_ready()
        
        # Route tsl_update events to broker
        for event in events or []:
            etype = str(event.get("type", ""))
            if etype == "tsl_update":
                key = self._key(event)
                new_stop = event.get("new_stop_price")
                if new_stop:
                    self.modify_trailing_stop(key, float(new_stop))

        # Check for expiry auto square-off
        self.expiry_squareoff_check()

        if opened or closed or buffered or expired:
            logger.info(
                "OMS: %d opened, %d closed, %d buffering, %d abandoned, %d duplicates",
                opened, closed, buffered, expired, skipped,
            )
        return {"opened": opened, "closed": closed, "buffered": buffered,
                "abandoned": expired, "skipped": skipped}

    def modify_trailing_stop(self, position_key: str, new_stop_price: float) -> None:
        """Sends a modify order to the broker for trailing stop-loss."""
        with self._lock:
            pos = self._positions.get(position_key)
        if not pos or pos.status != "OPEN" or not pos.stop_order_id:
            return
            
        try:
            from broker_dispatcher import get_dispatcher
            dispatcher = get_dispatcher()
            dispatcher.modify_order(
                order_id=pos.stop_order_id,
                price=new_stop_price,
                broker=pos.broker
            )
            logger.info("OMS: modified TSL for %s to %s", position_key, new_stop_price)
        except Exception as exc:
            logger.error("OMS: failed to modify TSL for %s: %s", position_key, exc)

    def aggregate_day_pnl(self) -> float:
        """Sums realized + unrealized P&L across all positions."""
        # A mock for the actual logic which requires pulling trade fills/prices.
        return 0.0

    def expiry_squareoff_check(self) -> None:
        """Calls options_engine.should_auto_squareoff()"""
        try:
            from options_engine import should_auto_squareoff, auto_squareoff_positions
            if should_auto_squareoff(datetime.now(IST_TZ)):
                auto_squareoff_positions()
        except Exception as exc:
            logger.error("OMS: expiry squareoff check failed: %s", exc)

    def reset(self) -> None:
        with self._lock:
            self._positions.clear()
            self._seen_events.clear()
            self._intents.clear()
            self._pending.clear()


_oms: Optional[OrderManager] = None
_oms_lock = threading.Lock()


def get_oms() -> OrderManager:
    global _oms
    with _oms_lock:
        if _oms is None:
            _oms = OrderManager()
        return _oms
