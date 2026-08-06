import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import pandas as pd
import numpy as np
from apex_python_scanner import rsi

# Create test data
np.random.seed(42)
dates = pd.date_range(start="2026-01-01", periods=200, freq="15min", tz="Asia/Kolkata")
base_price = 20000
prices = base_price + np.cumsum(np.random.normal(0, 10, 200))
data = pd.Series(prices, index=dates)

# Compute RSI
rsi14 = rsi(data, 14)

# Print min and max
print(f"RSI min: {rsi14.min()}")
print(f"RSI max: {rsi14.max()}")
print("\nFirst 20 RSI values:")
print(rsi14.head(20))
