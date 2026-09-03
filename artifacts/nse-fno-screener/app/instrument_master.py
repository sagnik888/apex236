"""Maintain the user's authoritative 237-symbol NSE F&O universe.

The bundled `data/instruments.json` is the universe contract.  The Upstox
BOD master is used to ENRICH those rows with current NSE_EQ / nearest-future
instrument keys and lot sizes; it must never silently shrink the user's list.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from app import config

log = logging.getLogger("instrument_master")
NSE_BOD_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CACHE_PATH = Path("data/instruments_live.json")
META_PATH = Path("data/instruments_live.meta.json")


def _read_json(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_bundled() -> list[dict]:
    try:
        req = Request("http://127.0.0.1:8080/api/symbols")
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        symbols = data.get("symbols", [])
        if symbols:
            return [{"symbol": s, "name": s, "yahoo_ticker": f"{s}.NS"} for s in symbols]
    except Exception as e:
        log.warning(f"Failed to fetch symbols from Apex: {e}. Falling back to bundled.")
    return _read_json(Path(config.INSTRUMENTS_PATH))


def _download_bod(timeout: int = 25) -> list[dict]:
    req = Request(NSE_BOD_URL, headers={"User-Agent": "NSE-FNO-Screener/4.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(gzip.decompress(raw).decode("utf-8"))


def _nearest_futures(records: list[dict]) -> dict[str, dict]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    nearest: dict[str, dict] = {}
    for r in records:
        if r.get("segment") != "NSE_FO" or r.get("instrument_type") != "FUT":
            continue
        expiry = r.get("expiry") or 0
        try:
            expiry = int(expiry)
        except (TypeError, ValueError):
            continue
        if expiry < now_ms:
            continue
        sym = (r.get("underlying_symbol") or r.get("trading_symbol") or "").strip().upper()
        if not sym:
            continue
        if sym not in nearest or expiry < int(nearest[sym].get("expiry") or 10**30):
            nearest[sym] = r
    return nearest


def _equities(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in records:
        if r.get("segment") == "NSE_EQ" and r.get("instrument_type") == "EQ":
            sym = (r.get("trading_symbol") or "").strip().upper()
            if sym:
                out[sym] = r
    return out


def _enrich_authoritative_universe(master: list[dict], records: list[dict]) -> tuple[list[dict], dict]:
    """Preserve every master symbol and enrich matches from the Upstox BOD.

    Older builds generated the universe *from* the BOD futures set.  That
    turned a temporary/mapping miss into a deleted stock.  Here a BOD miss is
    metadata only: the symbol remains available and Yahoo/history fallback can
    still score it.
    """
    equities = _equities(records)
    futures = _nearest_futures(records)
    enriched: list[dict] = []
    matched_eq = matched_fut = 0
    unmatched: list[str] = []

    for src in master:
        row = dict(src)
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym:
            continue
        row["symbol"] = sym
        row.setdefault("yahoo_ticker", f"{sym}.NS")

        eq = equities.get(sym)
        fut = futures.get(sym)
        if eq:
            matched_eq += 1
            row["upstox_instrument_key"] = eq.get("instrument_key")
            row["name"] = row.get("name") or eq.get("short_name") or eq.get("name") or sym
        else:
            row["upstox_instrument_key"] = row.get("upstox_instrument_key")

        if fut:
            matched_fut += 1
            row["future_instrument_key"] = fut.get("instrument_key")
            row["future_expiry"] = fut.get("expiry")
            try:
                live_lot = int(fut.get("lot_size") or 0)
            except (TypeError, ValueError):
                live_lot = 0
            if live_lot:
                row["lot_size"] = live_lot
        else:
            row.setdefault("future_instrument_key", None)
            row.setdefault("future_expiry", None)

        row["master_match"] = bool(eq)
        row["fno_contract_match"] = bool(fut)
        if not eq or not fut:
            unmatched.append(sym)
        enriched.append(row)

    stats = {
        "authoritative_count": len(enriched),
        "equity_key_matches": matched_eq,
        "future_contract_matches": matched_fut,
        "unmatched_count": len(unmatched),
        "unmatched_symbols": unmatched,
    }
    return enriched, stats


def _cache_is_authoritative(cache: list[dict], master: list[dict]) -> bool:
    return [r.get("symbol") for r in cache] == [r.get("symbol") for r in master]


def sync_instruments(force: bool = False) -> tuple[list[dict], dict]:
    """Refresh enrichment at most once/day without ever reducing master size."""
    today = datetime.now().astimezone().date().isoformat()
    master = load_bundled()

    if not force and CACHE_PATH.exists() and META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            cached = _read_json(CACHE_PATH)
            if meta.get("date") == today and _cache_is_authoritative(cached, master):
                return cached, meta
        except Exception:
            pass

    try:
        universe, stats = _enrich_authoritative_universe(master, _download_bod())
        if len(universe) != len(master):
            raise RuntimeError(f"enrichment changed master size {len(master)} -> {len(universe)}")
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(universe, indent=2), encoding="utf-8")
        meta = {
            "date": today,
            "source": "authoritative_237+upstox_bod_enrichment",
            "count": len(universe),
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
            **stats,
        }
        META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return universe, meta
    except Exception as exc:
        log.exception("Instrument master enrichment failed")
        # A prior cache is acceptable only if it contains the exact master
        # symbol sequence.  Never resurrect the old 210-row replacement cache.
        if CACHE_PATH.exists():
            try:
                cached = _read_json(CACHE_PATH)
                if _cache_is_authoritative(cached, master):
                    return cached, {
                        "date": today, "source": "cached_authoritative_enrichment",
                        "count": len(cached), "error": str(exc),
                        "authoritative_count": len(master),
                    }
            except Exception:
                pass
        return master, {
            "date": today,
            "source": "bundled_authoritative_fallback",
            "count": len(master),
            "authoritative_count": len(master),
            "error": str(exc),
        }
