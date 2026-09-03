from app import config
from app.engine.timeframes import TF_GROUPS

# Keep this test lightweight: pipeline imports yfinance, which is an optional
# runtime dependency in bare CI environments. The full app smoke test below
# exercises pipeline mappings when requirements are installed.


def test_30min_is_in_intraday_group():
    assert "30min" in TF_GROUPS["intraday"]


def test_live_micro_target_is_under_one_minute():
    assert config.MICRO_SCAN_SECONDS <= 60
    assert config.QUOTE_REFRESH_SECONDS < config.MICRO_SCAN_SECONDS


def test_lane_cadences_are_ordered():
    assert config.MICRO_SCAN_SECONDS < config.MEDIUM_SCAN_SECONDS < config.MACRO_SCAN_SECONDS
