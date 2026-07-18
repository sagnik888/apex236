"""Yahoo Finance data provider for Nifty 50 stocks (15-min delayed)."""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

IST = "Asia/Kolkata"

# Yahoo Finance fetch config per logical timeframe
_FETCH_CONFIG: dict[str, dict] = {
    "15m": {"interval": "15m", "period": "60d"},   # max yfinance allows for 15m
    "1h":  {"interval": "1h",  "period": "2y"},
    "4h":  {"interval": "1h",  "period": "2y"},   # resample 1h → 4h
    "1d":  {"interval": "1d",  "period": "2y"},
}

# NSE-aligned 4h origin — ensures buckets start at 09:15 IST every day
_4H_ORIGIN = pd.Timestamp("2000-01-03 09:15:00", tz=IST)

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_BATCH_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 300

def prefetch_all_ohlcv(symbols: list[str], timeframes: list[str]) -> None:
    """Pre-fetch all data using batch yfinance download to prevent rate limits."""
    now = time.time()
    for tf in timeframes:
        cfg = _FETCH_CONFIG.get(tf)
        if not cfg: continue
        
        try:
            logger.info(f"Batch downloading {len(symbols)} symbols for {tf}")
            # Download in chunks of 50 to avoid rate limits and large URI
            chunk_size = 50
            df_bulk = pd.DataFrame()
            for i in range(0, len(symbols), chunk_size):
                chunk = symbols[i:i+chunk_size]
                logger.info(f"Downloading chunk {i//chunk_size + 1} for {tf} ({len(chunk)} symbols)")
                df_chunk = yf.download(
                    tickers=" ".join(chunk),
                    interval=cfg["interval"],
                    period=cfg["period"],
                    group_by="ticker",
                    threads=True,
                    auto_adjust=True,
                    progress=False
                )
                if df_chunk is not None and not df_chunk.empty:
                    if df_bulk.empty:
                        df_bulk = df_chunk
                    else:
                        df_bulk = pd.concat([df_bulk, df_chunk], axis=1)
                time.sleep(1) # rate limit backoff
        except Exception as exc:
            logger.warning(f"Bulk download failed for {tf}: {exc}")
            continue

        if df_bulk is None or df_bulk.empty:
            continue

        # fast_info must still be fetched per-ticker, but we can parallelize it safely 
        # or skip it during batch if we want to avoid 100 requests. 
        # Actually, let's just cache the bulk data here and let fetch_ohlcv handle LTP injection
        
        # Ensure we have a MultiIndex
        if not isinstance(df_bulk.columns, pd.MultiIndex):
            # Fallback if only 1 symbol was passed (though here we pass many)
            sym = symbols[0]
            _BATCH_CACHE[(sym, tf)] = (now, df_bulk.copy())
            continue

        for sym in symbols:
            try:
                # Extract the single ticker's dataframe
                if sym in df_bulk.columns.levels[0]:
                    sym_df = df_bulk[sym].dropna(how="all").copy()
                    if not sym_df.empty:
                        _BATCH_CACHE[(sym, tf)] = (now, sym_df)
            except Exception:
                pass


def fetch_ohlcv(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV data for a symbol at the requested logical timeframe with batch caching."""
    cfg = _FETCH_CONFIG.get(timeframe)
    if not cfg:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")

    now = time.time()
    cache_key = (symbol, timeframe)
    
    # Try batch cache first
    raw = None
    if cache_key in _BATCH_CACHE:
        cached_time, cached_df = _BATCH_CACHE[cache_key]
        if now - cached_time < _CACHE_TTL:
            raw = cached_df.copy()

    # Fallback to single fetch if not in cache
    if raw is None or raw.empty:
        try:
            ticker = yf.Ticker(symbol)
            raw = ticker.history(
                interval=cfg["interval"],
                period=cfg["period"],
                auto_adjust=True,
            )
        except Exception as exc:
            logger.warning(f"yfinance fetch failed for {symbol}/{timeframe}: {exc}")
            return None

    if raw is None or raw.empty:
        return None

    try:
        last_price = ticker.fast_info.get("last_price")
        if not last_price:
            last_price = ticker.fast_info.get("lastPrice")
            
        if last_price and last_price > 0:
            raw.iloc[-1, raw.columns.get_loc("Close")] = last_price
            if last_price > raw.iloc[-1, raw.columns.get_loc("High")]:
                raw.iloc[-1, raw.columns.get_loc("High")] = last_price
            if last_price < raw.iloc[-1, raw.columns.get_loc("Low")]:
                raw.iloc[-1, raw.columns.get_loc("Low")] = last_price
    except Exception as exc:
        logger.debug(f"Could not fetch real-time LTP for {symbol}: {exc}")

    df = raw.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })

    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    if "volume" not in df.columns:
        df["volume"] = 0.0

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)

    df = df.dropna(subset=["open", "high", "low", "close"])

    mask_bad = (
        (df["high"] < df["low"]) |
        (df["close"] <= 0) |
        (df["open"] <= 0)
    )
    if mask_bad.any():
        df = df[~mask_bad]

    if timeframe == "4h":
        df = _resample_4h(df)

    if df.empty or len(df) < 50:
        return None

    _BATCH_CACHE[cache_key] = (now, df)
    return df.copy()


def _resample_4h(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample 1h bars to NSE-aligned 4h bars.

    NSE trades 09:15–15:30. Using origin=09:15 IST creates buckets at:
      09:15–13:15  (full 4h bar, the main trading session bar)
      13:15–15:30  (partial bar — kept, contains closing action)
    Bars outside these windows (overnight) are empty and dropped automatically.
    """
    rs = df.resample(
        "4h",
        origin=_4H_ORIGIN,
        closed="left",
        label="left",
    ).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    })
    # Drop bars with no data (overnight/weekend empty buckets)
    rs = rs.dropna(subset=["open", "high", "low", "close"])
    # Keep only bars that start inside or near market hours (09:00–16:00 IST)
    rs = rs[rs.index.hour.isin(range(9, 16))]
    return rs
