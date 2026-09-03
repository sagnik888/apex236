"""NSE regular-session status in Asia/Kolkata, including verified 2026 trading holidays."""
from datetime import datetime, time, date
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PREOPEN_START = time(9, 0)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# NSE F&O trading holidays for calendar year 2026 (NSE/FAOP/71777, 12-Dec-2025).
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
    date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
}
NSE_HOLIDAYS = NSE_HOLIDAYS_2026


def _ist(dt: datetime | None = None) -> datetime:
    dt = dt or datetime.now(IST)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def is_trading_day(dt: datetime | None = None) -> bool:
    dt = _ist(dt)
    return dt.weekday() < 5 and dt.date() not in NSE_HOLIDAYS


def is_trading_session(dt: datetime | None = None) -> bool:
    dt = _ist(dt)
    return is_trading_day(dt) and MARKET_OPEN <= dt.time() <= MARKET_CLOSE


def market_status(dt: datetime | None = None) -> str:
    dt = _ist(dt)
    if dt.weekday() >= 5:
        return "CLOSED"
    if dt.date() in NSE_HOLIDAYS:
        return "HOLIDAY"
    if PREOPEN_START <= dt.time() < MARKET_OPEN:
        return "PRE_OPEN"
    if MARKET_OPEN <= dt.time() <= MARKET_CLOSE:
        return "LIVE"
    return "CLOSED"
