"""Yahoo Finance delayed fallback with bulk fetch + bounded missing-symbol retry."""
from __future__ import annotations

import asyncio
import pandas as pd
import yfinance as yf

from app.data_sources.base import MarketDataSource

YAHOO_INTERVAL_MAP = {"5min": "5m", "15min": "15m", "1h": "60m", "1d": "1d"}
RETRY_MISSING_LIMIT = 60
RETRY_BATCH_SIZE = 30


class YahooFinanceSource(MarketDataSource):
    name = "yahoo_finance"

    def freshness_label(self) -> str:
        return "DELAYED"

    @staticmethod
    def _download_many(tickers: list[str], yf_interval: str, period: str):
        if not tickers:
            return pd.DataFrame()
        return yf.download(
            tickers=tickers, period=period, interval=yf_interval, progress=False,
            auto_adjust=True, group_by="ticker", threads=True, timeout=20,
        )

    @staticmethod
    def _extract(raw: pd.DataFrame, symbol_to_ticker: dict[str, str], lookback_bars: int) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        if raw is None or raw.empty:
            return out
        for symbol, ticker in symbol_to_ticker.items():
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker in raw.columns.get_level_values(0):
                        df = raw[ticker].copy()
                    elif ticker in raw.columns.get_level_values(-1):
                        df = raw.xs(ticker, axis=1, level=-1).copy()
                    else:
                        continue
                else:
                    if len(symbol_to_ticker) != 1:
                        continue
                    df = raw.copy()
                df.columns = [str(c).lower() for c in df.columns]
                needed = ["open", "high", "low", "close", "volume"]
                if not all(c in df.columns for c in needed):
                    continue
                df = df[needed].dropna(subset=["close"]).tail(lookback_bars)
                if df.empty:
                    continue
                idx = pd.DatetimeIndex(df.index)
                if idx.tz is None:
                    idx = idx.tz_localize("Asia/Kolkata")
                else:
                    idx = idx.tz_convert("Asia/Kolkata")
                df.index = idx
                df.index.name = "timestamp"
                out[symbol] = df.sort_index()
            except Exception:
                continue
        return out

    @staticmethod
    def _sync_download_with_retry(symbol_to_ticker: dict[str, str], yf_interval: str, period: str, lookback_bars: int):
        # Fast path: one bulk request. yfinance occasionally omits a subset of tickers
        # from a large multi-ticker response even when the rest succeeds.
        raw = YahooFinanceSource._download_many(list(symbol_to_ticker.values()), yf_interval, period)
        out = YahooFinanceSource._extract(raw, symbol_to_ticker, lookback_bars)

        missing = [s for s in symbol_to_ticker if s not in out]
        # Retry only a bounded omission set. If most symbols are missing, that is a
        # provider/network outage and blasting more requests would make it worse.
        if 0 < len(missing) <= RETRY_MISSING_LIMIT:
            for i in range(0, len(missing), RETRY_BATCH_SIZE):
                chunk_syms = missing[i:i + RETRY_BATCH_SIZE]
                chunk = {s: symbol_to_ticker[s] for s in chunk_syms}
                retry_raw = YahooFinanceSource._download_many(list(chunk.values()), yf_interval, period)
                out.update(YahooFinanceSource._extract(retry_raw, chunk, lookback_bars))
        return out

    async def get_many_bars(self, symbol_to_ticker: dict[str, str], interval: str, lookback_bars: int = 500) -> dict[str, pd.DataFrame]:
        # Try fetching from Apex backend first!
        import json
        import asyncio
        from urllib.request import Request, urlopen
        
        symbols_str = ",".join(symbol_to_ticker.keys())
        url = f"http://127.0.0.1:8080/api/historical_bulk/{interval}?symbols={symbols_str}"
        try:
            req = Request(url, headers={"User-Agent": "NSE-FNO-Screener/4.0"})
            
            def _fetch():
                with urlopen(req, timeout=180) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            
            payload = await asyncio.to_thread(_fetch)
            
            data = payload.get("data", {})
            if data:
                out = {}
                for sym, rows in data.items():
                    if rows:
                        df = pd.DataFrame(rows)
                        time_col = df.columns[0]
                        df[time_col] = pd.to_datetime(df[time_col], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
                        df.set_index(time_col, inplace=True)
                        out[sym] = df.tail(lookback_bars)
                # Only fallback to Yahoo if we got absolutely nothing (e.g. Apex failed completely)
                if out:
                    return out
        except Exception as e:
            import logging
            logging.getLogger("yahoo_finance").warning(f"Failed to fetch {interval} from Apex: {e}. Falling back to Yahoo.")

        yf_interval = YAHOO_INTERVAL_MAP.get(interval)
        if yf_interval is None:
            raise ValueError(f"Unsupported Yahoo base interval: {interval}")
        period = "60d" if yf_interval != "1d" else "2y"
        return await asyncio.to_thread(
            self._sync_download_with_retry, symbol_to_ticker, yf_interval, period, lookback_bars
        )

    async def get_bars(self, symbol: str, interval: str, lookback_bars: int) -> pd.DataFrame:
        result = await self.get_many_bars({symbol: symbol}, interval, lookback_bars)
        return result.get(symbol, pd.DataFrame())
