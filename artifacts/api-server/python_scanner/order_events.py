"""Broker order-update intake (webhook / postback) and the order state store.

Before this module the system had ZERO webhook integration — a grep for
webhook / postback / order_update across the package returned nothing. Order
state was polled at most once per 60-second scan cycle from a single-worker
executor, and lost entirely on restart. Between a stop filling and the poller
noticing, the sibling target leg stayed live: a multi-minute window in which the
account could be flipped to an unintended opposite position.

Two intake paths, one store:

  * ``POST /api/webhooks/upstox``  — Upstox postback (configure the URL in the
    Upstox developer console). Upstox posts order updates as JSON.
  * ``POST /api/webhooks/angel``   — Angel SmartAPI order-update push.

Both are normalised into ``OrderEvent`` and applied to a durable store, so the
OMS can answer "what actually happened to order X" without polling, and can
recover that answer across a restart.

Security: these endpoints accept broker traffic from outside the loopback
interface, so they are authenticated with a shared secret
(``APEX_WEBHOOK_SECRET``) supplied either as a query parameter or the
``X-Apex-Signature`` header. Without the secret configured the endpoints refuse
every request rather than accepting unauthenticated order state — an attacker
who can forge fills can drive the OMS into closing or reversing real positions.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST_TZ = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
STORE_PATH = HERE / "order_events.json"

# Normalised order states. Every broker vocabulary maps into these.
FILLED = "FILLED"
PARTIAL = "PARTIAL"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
OPEN = "OPEN"
UNKNOWN = "UNKNOWN"

TERMINAL = {FILLED, REJECTED, CANCELLED}

# Angel returns `orderstatus`; Upstox returns `status`. Neither vocabulary
# matches the other, and the old _order_is_filled read only `status`/
# `order_status`, so Angel orders never reconciled at all.
_STATUS_MAP = {
    "complete": FILLED, "completed": FILLED, "filled": FILLED, "traded": FILLED,
    "partially filled": PARTIAL, "partial": PARTIAL,
    "rejected": REJECTED, "cancelled": CANCELLED, "canceled": CANCELLED,
    "open": OPEN, "pending": OPEN, "trigger pending": OPEN,
    "open pending": OPEN, "validation pending": OPEN, "put order req received": OPEN,
    "modify pending": OPEN, "after market order req received": OPEN,
}


def normalise_status(raw: Any) -> str:
    return _STATUS_MAP.get(str(raw or "").strip().lower(), UNKNOWN)


@dataclass
class OrderEvent:
    order_id: str
    broker: str
    status: str
    symbol: str = ""
    filled_quantity: int = 0
    quantity: int = 0
    average_price: float = 0.0
    message: str = ""
    received_at: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_complete_fill(self) -> bool:
        """Fully filled — NOT merely 'status says complete'.

        _order_is_filled ignored filled_quantity vs quantity entirely, so
        {'status':'complete','filled_quantity':5,'quantity':50} read as filled
        and the protective legs were sized for the full 50.
        """
        if self.status != FILLED:
            return False
        if self.quantity > 0 and self.filled_quantity > 0:
            return self.filled_quantity >= self.quantity
        return True


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_upstox(payload: dict) -> Optional[OrderEvent]:
    """Normalise an Upstox postback body."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    order_id = str(data.get("order_id") or data.get("orderId") or "").strip()
    if not order_id:
        return None
    return OrderEvent(
        order_id=order_id,
        broker="upstox",
        status=normalise_status(data.get("status") or data.get("order_status")),
        symbol=str(data.get("trading_symbol") or data.get("tradingsymbol") or ""),
        filled_quantity=_as_int(data.get("filled_quantity")),
        quantity=_as_int(data.get("quantity")),
        average_price=_as_float(data.get("average_price")),
        message=str(data.get("status_message") or data.get("message") or ""),
        raw=payload,
    )


def parse_angel(payload: dict) -> Optional[OrderEvent]:
    """Normalise an Angel SmartAPI order-update body.

    Angel's status key is `orderstatus`, not `status` — the module's own code
    proves it (broker_angel fabricates {"orderstatus": "complete"} for paper).
    Both are accepted here so a schema change cannot silently disarm it.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    order_id = str(data.get("orderid") or data.get("order_id") or "").strip()
    if not order_id:
        return None
    return OrderEvent(
        order_id=order_id,
        broker="angel",
        status=normalise_status(
            data.get("orderstatus") or data.get("status") or data.get("order_status")
        ),
        symbol=str(data.get("tradingsymbol") or data.get("trading_symbol") or ""),
        filled_quantity=_as_int(data.get("filledshares") or data.get("filled_quantity")),
        quantity=_as_int(data.get("quantity") or data.get("orderqty")),
        average_price=_as_float(data.get("averageprice") or data.get("average_price")),
        message=str(data.get("text") or data.get("message") or ""),
        raw=payload,
    )


class OrderEventStore:
    """Durable last-known state per broker order id.

    Persisted to disk so a restart does not lose the fact that a stop already
    filled — the previous design kept order state only in memory.
    """

    def __init__(self, path: Path = STORE_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._events: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._events = json.loads(self._path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("Could not load order event store: %s", exc)
            self._events = {}

    def _flush(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._events, indent=2, default=str), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as exc:
            logger.warning("Could not persist order event store: %s", exc)

    def apply(self, event: OrderEvent) -> OrderEvent:
        """Record an event. A terminal state is never overwritten by a later
        non-terminal one — brokers can deliver out of order, and 'OPEN' arriving
        after 'FILLED' must not resurrect a closed order."""
        with self._lock:
            event.received_at = datetime.now(IST_TZ).isoformat()
            prior = self._events.get(event.order_id)
            if prior and prior.get("status") in TERMINAL and event.status not in TERMINAL:
                logger.info(
                    "Ignoring %s for %s: already terminal (%s)",
                    event.status, event.order_id, prior.get("status"),
                )
                return OrderEvent(**{k: v for k, v in prior.items() if k in OrderEvent.__dataclass_fields__})
            self._events[event.order_id] = asdict(event)
            self._flush()
            return event

    def get(self, order_id: str) -> Optional[OrderEvent]:
        with self._lock:
            row = self._events.get(str(order_id))
        if not row:
            return None
        return OrderEvent(**{k: v for k, v in row.items() if k in OrderEvent.__dataclass_fields__})

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = sorted(self._events.values(), key=lambda r: r.get("received_at") or "", reverse=True)
        return rows[:limit]

    def clear(self) -> None:
        with self._lock:
            self._events = {}
            self._flush()


_store: Optional[OrderEventStore] = None
_store_lock = threading.Lock()


def get_store() -> OrderEventStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = OrderEventStore()
        return _store


def webhook_secret_configured() -> bool:
    return bool(os.getenv("APEX_WEBHOOK_SECRET", "").strip())


def verify_webhook_secret(supplied: Optional[str]) -> bool:
    """Constant-time comparison against APEX_WEBHOOK_SECRET.

    Fails CLOSED when the secret is not configured: an unauthenticated order
    webhook lets anyone forge a fill, and a forged fill drives the OMS into
    cancelling or reversing a real position.
    """
    expected = os.getenv("APEX_WEBHOOK_SECRET", "").strip()
    if not expected:
        return False
    return hmac.compare_digest(str(supplied or "").strip(), expected)


def ingest(broker: str, payload: dict) -> Optional[OrderEvent]:
    """Parse and record a raw broker payload. Returns the stored event."""
    parser = {"upstox": parse_upstox, "angel": parse_angel}.get(broker.lower())
    if parser is None:
        logger.warning("Unknown webhook broker %r", broker)
        return None
    event = parser(payload or {})
    if event is None:
        logger.warning("Unparseable %s order payload (no order id)", broker)
        return None
    stored = get_store().apply(event)
    logger.info(
        "Order update [%s] %s -> %s (%s/%s filled @ %.2f)",
        stored.broker, stored.order_id, stored.status,
        stored.filled_quantity, stored.quantity, stored.average_price,
    )
    return stored
