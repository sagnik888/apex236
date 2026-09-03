"""
Abstract interface both data sources implement, so the scoring engine
never needs to know whether a bar came from Upstox or Yahoo — it just
asks for bars and gets a DataFrame plus a freshness label.
"""
from abc import ABC, abstractmethod
import pandas as pd


class MarketDataSource(ABC):
    name: str

    @abstractmethod
    async def get_bars(self, symbol: str, interval: str, lookback_bars: int) -> pd.DataFrame:
        """Return a closed-bar OHLCV DataFrame, ascending, tz-aware index."""
        ...

    @abstractmethod
    def freshness_label(self) -> str:
        """
        'LIVE' | 'DELAYED' | 'OFFLINE' — surfaced directly in the UI
        badge next to every price. Never let the UI show a number
        without this label attached; a trader acting on stale data
        while believing it's live is exactly the failure mode this
        label exists to prevent.
        """
        ...
