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

logger = logging.getLogger(__name__)

NSE_HOLIDAYS: dict[int, set[date]] = {
    2026: {
        date(2026, 1, 15),   # Maharashtra municipal elections
        date(2026, 1, 26),   # Republic Day
        date(2026, 3, 3),    # Holi
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
MUHURAT_SESSIONS: dict[int, date] = {2026: date(2026, 11, 8)}

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


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def calendar_covers(year: int) -> bool:
    return year in NSE_HOLIDAYS
