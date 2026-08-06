import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import logging, time, os
logging.basicConfig(level=logging.WARNING)
os.environ.setdefault("APEX_SCAN_WORKERS", "3")

def main():
    from scanner_engine import ScannerEngine, SCAN_SYMBOLS
    print("symbols under test:", len(SCAN_SYMBOLS))
    eng = ScannerEngine()

    t0 = time.monotonic(); eng.run_all_scans(["15m"]); t_15m = time.monotonic() - t0
    t0 = time.monotonic(); eng.run_all_scans(["1h", "4h", "1d"]); t_htf = time.monotonic() - t0

    stats = eng.get_stats()
    print(f"\n15m-only cycle: {t_15m:.1f}s | HTF cycle: {t_htf:.1f}s")
    print("scan_errors:", stats["scan_errors"], "| active:", stats["active_trades"], "| pending:", stats["pending_signals"])
    print("breadth:", stats["market_breadth"])
    dh = stats["data_health"]
    print("data_health: angel_active=%s feed_connected=%s pkt_age=%.2fs yahoo_syms=%s" % (
        dh["angel_active"], dh["feed"]["connected"], dh["feed"]["last_packet_age_s"] or -1, dh["source_by_symbol_yahoo"]))

    sigs = eng.get_signals()
    print("signals:", sigs["total_signals"], "| sample:", [
        {k: s[k] for k in ("symbol","timeframe","direction","state","entry_price","sl1","tp1")} for s in sigs["signals"][:3]])
    print("active trades payload:", eng.get_active_trades()["total"])

    an = eng.get_analytics("30d")
    s = an["summary"]
    print("analytics:", s["overall_win_rate_pct"], "WR |", s["overall_profit_factor"], "PF |",
          s["overall_sharpe_ratio"], "sharpe | trades:", s["total_historical_trades"], "| basis:", s["data_basis"])
    print("15m tf stats:", {k: an["timeframe_breakdown"]["15m"][k] for k in ("num_trades","win_rate_pct","profit_factor","sharpe_ratio","insufficient_data")})

    res = eng._results["15m"].get("RELIANCE")
    if res is not None:
        print("RELIANCE 15m last bar:", res.frame.index[-1], "| close:", round(float(res.frame['close'].iloc[-1]), 2))

    import sqlite3
    con = sqlite3.connect("apex_trading.db")
    print("signal_states rows:", con.execute("select count(*) from signal_states").fetchone()[0])
    print("trades rows:", con.execute("select count(*) from trades").fetchone()[0])
    eng.shutdown()

if __name__ == "__main__":
    main()
