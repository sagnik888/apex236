import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import yfinance as yf
ticker = yf.Ticker("RELIANCE.NS")
hist = ticker.history(period="5d", interval="15m")
print(hist.tail(5))
