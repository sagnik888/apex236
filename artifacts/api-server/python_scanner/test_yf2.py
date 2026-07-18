import yfinance as yf
ticker = yf.Ticker("RELIANCE.NS")
try:
    print(ticker.fast_info.get("lastPrice"))
except Exception as e:
    print("Error:", type(e), e)
