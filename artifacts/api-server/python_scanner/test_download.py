import yfinance as yf
import pandas as pd

symbols = ["RELIANCE.NS", "TCS.NS"]
df = yf.download(tickers=" ".join(symbols), interval="1d", period="1mo", group_by="ticker", threads=True, auto_adjust=True)
print(df.columns)
if isinstance(df.columns, pd.MultiIndex):
    print("MultiIndex")
    print(df["RELIANCE.NS"].head())
else:
    print("Single Index")
    print(df.head())
