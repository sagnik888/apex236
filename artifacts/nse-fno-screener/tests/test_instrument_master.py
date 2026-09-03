from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

from app.instrument_master import _enrich_authoritative_universe, load_bundled


def test_authoritative_master_has_237_symbols_and_matches_uploaded_reference():
    master = load_bundled()
    assert len(master) == 237
    assert len({r["symbol"] for r in master}) == 237
    assert master[0]["symbol"] == "360ONE"
    assert master[-1]["symbol"] == "WAAREE"


def test_upstox_enrichment_can_never_shrink_237_to_210():
    master = load_bundled()
    expiry_ms = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp() * 1000)
    records = []
    # Simulate the old failure: only 210 symbols happen to match the BOD parse.
    for row in master[:210]:
        sym = row["symbol"]
        records.append({
            "segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": sym,
            "instrument_key": f"NSE_EQ|{sym}", "name": row["name"],
        })
        records.append({
            "segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": sym,
            "instrument_key": f"NSE_FO|{sym}", "expiry": expiry_ms, "lot_size": row["lot_size"],
        })
    enriched, stats = _enrich_authoritative_universe(master, records)
    assert len(enriched) == 237
    assert [r["symbol"] for r in enriched] == [r["symbol"] for r in master]
    assert stats["equity_key_matches"] == 210
    assert stats["future_contract_matches"] == 210
    assert len(stats["unmatched_symbols"]) == 27
