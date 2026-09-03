from typing import Optional
from pydantic import BaseModel


class ScanRow(BaseModel):
    symbol: str
    name: str
    lot_size: int
    last_price: float
    score: float
    label: str
    pct_change_1d: Optional[float] = None
    pct_change_2d: Optional[float] = None
    pct_change_3d: Optional[float] = None
    pct_change_4d: Optional[float] = None
    pct_change_7d: Optional[float] = None
    pct_change_14d: Optional[float] = None
    pct_change_30d: Optional[float] = None
    pct_change_60d: Optional[float] = None
    rvol: Optional[float] = None
    adx: Optional[float] = None
    data_source: str      # "upstox_live" | "yahoo_finance"
    freshness: str        # "LIVE" | "DELAYED" | "OFFLINE"
    last_updated: str


class ScanResponse(BaseModel):
    timeframe: str
    market_status: str
    bullish: list[ScanRow]
    bearish: list[ScanRow]
    neutral_count: int
    generated_at: str
