import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import yfinance as yf

ticker = yf.Ticker("RELIANCE.NS")
hist = ticker.history(period="1d", interval="15m")
print("Latest history close:", hist['Close'].iloc[-1] if not hist.empty else "No history")

print("fast_info last_price:", ticker.fast_info['lastPrice'])
try:
    print("info currentPrice:", ticker.info.get('currentPrice'))
except Exception as e:
    print("info currentPrice error:", e)
