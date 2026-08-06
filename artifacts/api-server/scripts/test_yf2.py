import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import yfinance as yf
ticker = yf.Ticker("RELIANCE.NS")
try:
    print(ticker.fast_info.get("lastPrice"))
except Exception as e:
    print("Error:", type(e), e)
