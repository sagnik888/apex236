import sys
sys.path.append("artifacts/api-server/python_scanner")
from scanner_engine import engine
from settings_store import get_settings

print("Active trades in memory:")
for tf, syms in engine._results.items():
    for sym, res in syms.items():
        if res.latest.get("state") == "ACTIVE":
            print(f"{sym}/{tf}: option_ltp={res.latest.get('option_ltp')}, option_symbol={res.latest.get('option_symbol')}")
            print(f"   from active object: option_ltp={res.active_trade.option_ltp if res.active_trade else None}")
