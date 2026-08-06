"""Regression checks for NSE session boundaries and live cache behaviour."""
from datetime import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from data_provider import _cache_ttl_seconds
from scanner_engine import get_market_status, scan_interval_secs


IST = ZoneInfo("Asia/Kolkata")


def _at(hour: int, minute: int) -> datetime:
    # Monday, 20 July 2026 is a normal NSE trading day.
    return datetime(2026, 7, 20, hour, minute, tzinfo=IST)


def _weekend_at(hour: int, minute: int) -> datetime:
    # Sunday, 19 July 2026 is a weekend.
    return datetime(2026, 7, 19, hour, minute, tzinfo=IST)


class TestNseMarketSession(unittest.TestCase):
    def test_nse_live_window_is_0915_to_1530_ist(self) -> None:
        self.assertEqual(get_market_status(_at(9, 14))["session_status"], "PRE_OPEN")
        self.assertTrue(get_market_status(_at(9, 15))["market_open"])
        self.assertTrue(get_market_status(_at(15, 30))["market_open"])
        self.assertEqual(get_market_status(_at(15, 31))["session_status"], "CLOSED")

    def test_live_session_uses_one_minute_scheduler_and_fresh_cache(self) -> None:
        self.assertEqual(scan_interval_secs(_at(10, 0)), 60)
        self.assertLess(_cache_ttl_seconds(_at(10, 0)), scan_interval_secs(_at(10, 0)))
        self.assertEqual(_cache_ttl_seconds(_at(16, 0)), 300)
        
    def test_weekend_session_is_closed(self) -> None:
        self.assertEqual(get_market_status(_weekend_at(10, 0))["session_status"], "WEEKEND")
        self.assertFalse(get_market_status(_weekend_at(10, 0))["market_open"])
        self.assertEqual(_cache_ttl_seconds(_weekend_at(10, 0)), 300)
        
    @patch("scanner_engine.is_holiday")
    @patch("market_calendar.is_holiday")
    def test_holiday_session_is_closed(self, mock_mc_is_holiday, mock_se_is_holiday) -> None:
        mock_mc_is_holiday.return_value = True
        mock_se_is_holiday.return_value = True
        self.assertEqual(get_market_status(_at(10, 0))["session_status"], "HOLIDAY")
        self.assertFalse(get_market_status(_at(10, 0))["market_open"])
        self.assertEqual(_cache_ttl_seconds(_at(10, 0)), 300)


if __name__ == "__main__":
    unittest.main()
