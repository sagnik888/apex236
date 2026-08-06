"""NSE trading calendar shared by the scanner engine and data provider.

Holiday source: NSE equity-segment trading holiday circular for 2026
(verified 2026-07-20 against public holiday calendars). The previous inline
list contained wrong dates (Holi, Ram Navami, both Diwali sessions) and was
missing ten weekday holidays.

NOTE: Muhurat trading (Diwali Laxmi Pujan) is a special ~1h evening session;
this module intentionally reports that date as a holiday for the regular
09:15–15:30 cash session. Support for the Muhurat window would need the
exchange's session-time circular each year.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

NSE_HOLIDAYS: dict[int, set[date]] = {
    2024: {
        date(2024, 1, 26),   # Republic Day
        date(2024, 3, 8),    # Mahashivratri
        date(2024, 3, 25),   # Holi
        date(2024, 3, 29),   # Good Friday
        date(2024, 4, 11),   # Id-Ul-Fitr
        date(2024, 4, 17),   # Shri Ram Navmi
        date(2024, 5, 1),    # Maharashtra Day
        date(2024, 6, 17),   # Bakri Id
        date(2024, 7, 17),   # Moharram
        date(2024, 8, 15),   # Independence Day
        date(2024, 10, 2),   # Mahatma Gandhi Jayanti
        date(2024, 11, 1),   # Diwali Laxmi Pujan
        date(2024, 11, 15),  # Gurunanak Jayanti
        date(2024, 12, 25),  # Christmas
    },
    2025: {
        date(2025, 2, 26),   # Mahashivratri
        date(2025, 3, 14),   # Holi
        date(2025, 3, 31),   # Id-Ul-Fitr
        date(2025, 4, 10),   # Shri Mahavir Jayanti
        date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
        date(2025, 4, 18),   # Good Friday
        date(2025, 5, 1),    # Maharashtra Day
        date(2025, 8, 15),   # Independence Day
        date(2025, 8, 27),   # Ganesh Chaturthi
        date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra
        date(2025, 10, 21),  # Diwali Laxmi Pujan
        date(2025, 10, 22),  # Diwali Balipratipada
        date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev
        date(2025, 12, 25),  # Christmas
    },
    2026: {
        date(2026, 1, 15),   # Maharashtra municipal elections
        date(2026, 1, 26),   # Republic Day
        date(2026, 3, 3),    # Holi
        date(2026, 3, 20),   # Id-Ul-Fitr
        date(2026, 3, 26),   # Shri Ram Navami
        date(2026, 3, 31),   # Shri Mahavir Jayanti
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 14),   # Dr. Ambedkar Jayanti
        date(2026, 5, 1),    # Maharashtra Day
        date(2026, 5, 28),   # Bakri Id
        date(2026, 6, 26),   # Muharram
        date(2026, 9, 14),   # Ganesh Chaturthi
        date(2026, 10, 2),   # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 10),  # Diwali Balipratipada
        date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
        date(2026, 12, 25),  # Christmas
    },
}

# Special sessions (informational): regular session closed, evening Muhurat only.
MUHURAT_SESSIONS: dict[int, date] = {
    2024: date(2024, 11, 1),
    2025: date(2025, 10, 21),
    2026: date(2026, 11, 8)
}

# Real full cash sessions the exchange holds on a day the weekday/holiday rule
# would otherwise reject — budget Sundays and the like. These ARE tradeable and
# their candles are genuine.
SPECIAL_SESSIONS: set[date] = {
    date(2026, 2, 1),    # Union Budget presented on a Saturday; live cash session
}

# MOCK / disaster-recovery sessions. The exchange really does run these and the
# brokers really do return candles for them, but the prices are synthetic — they
# are a systems test, not a market. They MUST be excluded from ingestion.
#
# Ingesting them is not cosmetic: the 2026-08-01 mock printed RELIANCE between
# 1270 and 1569 against a real 2026-07-31 close of 1305. That single bar inflated
# ATR(14) on the 1h series by a median 4.47x across 234 of 236 symbols (max
# 15.7x), which pushed the raw ATR stop from ~1.28% to ~5.93% of price and pinned
# every subsequent intraday trade against the 1.5% stop clamp.
MOCK_SESSIONS: set[date] = {
    date(2026, 7, 25),
    date(2026, 8, 1),
}

_warned_years: set[int] = set()


def holidays_for(year: int) -> set[date]:
    holidays = NSE_HOLIDAYS.get(year)
    if holidays is None:
        if year not in _warned_years:
            _warned_years.add(year)
            logger.error(
                "NSE holiday calendar has no data for %s — update market_calendar.py! "
                "Falling back to weekday-only session detection.", year,
            )
        return set()
    return holidays


def is_holiday(d: date) -> bool:
    return d in holidays_for(d.year)


def is_mock_session(d: date) -> bool:
    """True for exchange mock / disaster-recovery sessions.

    These produce real broker candles at synthetic prices. Never ingest them.
    """
    return d in MOCK_SESSIONS


def is_trading_day(d: date) -> bool:
    """True when the regular cash session runs on this date.

    CAUTION: for a year the calendar does not cover, holidays_for() returns an
    empty set and this degrades to a weekday check — every exchange holiday in
    that year is reported as a normal trading day. Call calendar_covers() at
    startup and surface a degraded state rather than relying on this silently.
    """
    if is_mock_session(d):
        return False
    if d in SPECIAL_SESSIONS:
        return True
    return d.weekday() < 5 and not is_holiday(d)


def assert_calendar_current(year: int) -> Optional[str]:
    """Return a human-readable warning when `year` has no holiday data.

    Returns None when the calendar covers the year. Intended for startup checks
    and /api/healthz so the operator learns the calendar has lapsed BEFORE the
    engine starts treating exchange holidays as open sessions.
    """
    if calendar_covers(year):
        return None
    return (
        f"NSE holiday calendar has no data for {year}. is_trading_day() is degraded to a "
        f"weekday-only check, so every {year} exchange holiday will be reported as OPEN. "
        f"Update NSE_HOLIDAYS in market_calendar.py from the exchange circular."
    )


def is_ingestable(d: date) -> bool:
    """True when candles dated `d` may be merged into the price store.

    Deliberately separate from is_trading_day so the two questions — "should the
    scanner be polling right now?" and "is this bar real price history?" — can
    never drift apart again.
    """
    return is_trading_day(d)


def calendar_covers(year: int) -> bool:
    return year in NSE_HOLIDAYS
