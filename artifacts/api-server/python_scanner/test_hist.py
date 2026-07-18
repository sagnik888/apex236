import yfinance as yf
ticker = yf.Ticker("RELIANCE.NS")
hist = ticker.history(period="5d", interval="15m")
print(hist.tail(5))
