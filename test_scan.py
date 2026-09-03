import sys; sys.path.append('artifacts/api-server/python_scanner')
from data_provider import prefetch_all_ohlcv, fetch_ohlcv
from scanner_engine import _scan_preloaded
from nifty50 import NIFTY236_SYMBOLS

symbols = NIFTY236_SYMBOLS
prefetch_all_ohlcv(symbols, ['15m', '1h'])

for tf in ['15m', '1h']:
    max_bull = 0
    signals = 0
    for sym in symbols:
        df = fetch_ohlcv(sym, tf)
        if df is not None and not df.empty:
            res = _scan_preloaded(sym, tf, df)
            if res:
                bull = res.latest.get('bull_score', 0)
                if bull > max_bull: max_bull = bull
                if res.latest.get('signal') or res.latest.get('state') in ('ACTIVE', 'PENDING'):
                    signals += 1
    print(f"TF: {tf}, Max Bull: {max_bull}, Total Signals/Active: {signals}")
