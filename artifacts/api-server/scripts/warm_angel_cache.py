import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

"""One-off warm-up: bootstrap Angel candle history for the full universe."""
import logging, time
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
import data_provider as dp
from nifty50 import NIFTY236_SYMBOLS

t0 = time.monotonic()
dp.prefetch_all_ohlcv(NIFTY236_SYMBOLS, ["15m", "1h"])   # blocks on 15m bootstrap
dp._BOOTSTRAP_EVENTS["1h"].wait(timeout=1800)             # wait for 1h too
print(f"WARM DONE in {time.monotonic()-t0:.0f}s | health={dp.get_data_health()}")
