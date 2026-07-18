import json
import math
import unittest

import numpy as np
import pandas as pd

from apex_python_scanner import (
    IST,
    ActiveTrade,
    ApexConfig,
    ApexScanner,
    InstrumentProfile,
    PerformanceStats,
    SwingForecast,
    SymbolResult,
    build_feature_frame,
    calculate_atm,
    calculate_stops,
    detect_instrument,
    dynamic_min_score,
    normalize_ohlcv,
    previous_closed_mtf,
    result_to_json,
    rma,
    self_test,
    synthetic_ohlcv,
)


class ApexScannerV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = synthetic_ohlcv(rows=1800, seed=11)
        cls.config = ApexConfig(
            use_session=False,
            min_score=58.0,
            conflict_margin=12.0,
            entry_delay_bars=1,
            allow_entry_on_last_bar=True,
        )
        cls.scanner = ApexScanner(cls.config)
        cls.result = cls.scanner.run_symbol("NIFTY50", cls.data, asset_type="index")

    def test_built_in_self_test(self):
        self.assertEqual(self_test()["status"], "PASS")

    def test_wilder_rma_uses_sma_seed(self):
        source = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        actual = rma(source, 3)
        expected = pd.Series([math.nan, math.nan, 2.0, 8.0 / 3.0, 31.0 / 9.0])
        np.testing.assert_allclose(actual.to_numpy(), expected.to_numpy(), equal_nan=True, rtol=1e-12)

    def test_full_pattern_inventory(self):
        df = normalize_ohlcv(self.data, self.config)
        features = build_feature_frame(df, self.config)
        raw_pattern_columns = [
            c for c in features.columns
            if c.startswith("pattern_")
            and c not in {
                "pattern_valid_bull", "pattern_valid_bear",
                "pattern_fake_bull", "pattern_fake_bear",
                "pattern_strong_bull", "pattern_strong_bear",
                "pattern_count",
            }
        ]
        self.assertEqual(len(raw_pattern_columns), 39)

    def test_default_execution_is_next_bar(self):
        signal_positions = np.flatnonzero(self.result.frame["signal"].isin(["BUY", "SELL"]).to_numpy())
        entry_positions = set(np.flatnonzero(self.result.frame["entry_event"].to_numpy()))
        self.assertGreater(len(signal_positions), 0)
        for pos in signal_positions:
            self.assertNotIn(pos, entry_positions)
            if pos + 1 < len(self.result.frame):
                self.assertIn(pos + 1, entry_positions)

    def test_last_bar_entry_flag_is_effective(self):
        signal_positions = np.flatnonzero(self.result.frame["signal"].isin(["BUY", "SELL"]).to_numpy())
        first_signal = int(signal_positions[0])
        prefix = self.data.iloc[: first_signal + 2]
        cfg = ApexConfig(
            use_session=False,
            min_score=58.0,
            conflict_margin=12.0,
            entry_delay_bars=1,
            allow_entry_on_last_bar=False,
        )
        result = ApexScanner(cfg).run_symbol("NIFTY50", prefix, asset_type="index")
        self.assertFalse(bool(result.frame["entry_event"].iloc[-1]))
        self.assertIsNotNone(result.pending_order)
        self.assertIsNone(result.active_trade)
        self.assertIsNotNone(result.order_intent())

    def test_open_timestamp_mtf_mapping(self):
        idx = pd.date_range("2026-01-02 09:15", periods=31, freq="1min", tz=IST)
        values = np.arange(31.0) + 100.0
        frame = pd.DataFrame(
            {"open": values, "high": values + 0.2, "low": values - 0.2, "close": values, "volume": 1.0},
            index=idx,
        )
        mapped = previous_closed_mtf(frame, "15min", lambda x: x["close"], timestamps_are_bar_close=False)
        self.assertTrue(math.isnan(mapped.loc[pd.Timestamp("2026-01-02 09:29", tz=IST)]))
        self.assertEqual(mapped.loc[pd.Timestamp("2026-01-02 09:30", tz=IST)], 114.0)

    def test_close_timestamp_mtf_mapping(self):
        idx = pd.date_range("2026-01-02 09:16", periods=31, freq="1min", tz=IST)
        values = np.arange(31.0) + 100.0
        frame = pd.DataFrame(
            {"open": values, "high": values + 0.2, "low": values - 0.2, "close": values, "volume": 1.0},
            index=idx,
        )
        mapped = previous_closed_mtf(frame, "15min", lambda x: x["close"], timestamps_are_bar_close=True)
        self.assertTrue(math.isnan(mapped.loc[pd.Timestamp("2026-01-02 09:30", tz=IST)]))
        self.assertEqual(mapped.loc[pd.Timestamp("2026-01-02 09:31", tz=IST)], 114.0)

    def test_daily_mtf_uses_previous_trading_session(self):
        index = []
        values = []
        for date, price in (("2026-01-01", 100), ("2026-01-02", 200), ("2026-01-05", 300)):
            index.extend([
                pd.Timestamp(f"{date} 09:15", tz=IST),
                pd.Timestamp(f"{date} 15:29", tz=IST),
            ])
            values.extend([float(price), float(price + 1)])
        frame = pd.DataFrame(
            {
                "open": values,
                "high": np.asarray(values) + 1,
                "low": np.asarray(values) - 1,
                "close": values,
                "volume": 100.0,
            },
            index=index,
        )
        mapped = previous_closed_mtf(frame, "1D", lambda x: x["close"])
        self.assertEqual(mapped.loc[pd.Timestamp("2026-01-02 09:15", tz=IST)], 101.0)
        self.assertEqual(mapped.loc[pd.Timestamp("2026-01-05 09:15", tz=IST)], 201.0)

    def test_instrument_aliases_and_half_up_strike(self):
        self.assertEqual(detect_instrument("NIFTYBANK", asset_type="index").name, "Bank Nifty")
        self.assertEqual(detect_instrument("NIFTY FIN SERVICE", asset_type="index").name, "FinNifty")
        self.assertEqual(calculate_atm(22525.0, 50.0), 22550.0)

    def test_gap_stop_is_always_protective(self):
        row = pd.Series({"high_volatility": True, "is_trending": True, "low_volatility": False, "atr": 10.0})
        long_sl1, long_sl2, _ = calculate_stops(True, 80.0, 100.0, 110.0, row, ApexConfig(use_session=False))
        short_sl1, short_sl2, _ = calculate_stops(False, 120.0, 90.0, 100.0, row, ApexConfig(use_session=False))
        self.assertLess(long_sl2, long_sl1)
        self.assertLess(long_sl1, 80.0)
        self.assertGreater(short_sl2, short_sl1)
        self.assertGreater(short_sl1, 120.0)

    def test_nse_session_not_forced_on_commodity(self):
        idx = pd.date_range("2026-01-02 18:00", periods=300, freq="1min", tz=IST)
        price = 7000 + np.linspace(0, 50, len(idx))
        frame = pd.DataFrame(
            {"open": price, "high": price + 2, "low": price - 2, "close": price + 0.5, "volume": 1000},
            index=idx,
        )
        cfg = ApexConfig(use_session=True, enforce_market_hours=True)
        commodity = build_feature_frame(frame, cfg, InstrumentProfile("Commodity", False, 50.0))
        stock = build_feature_frame(frame, cfg, InstrumentProfile("Stock", False, 50.0))
        self.assertTrue(commodity["session_ok"].all())
        self.assertFalse(stock["session_ok"].any())

    def test_dynamic_score_never_exceeds_score_scale(self):
        self.assertEqual(dynamic_min_score(90.0, 3), 100.0)
        self.assertEqual(dynamic_min_score(70.0, 2), 82.0)

    def test_invalid_nonpositive_prices_are_rejected(self):
        bad = self.data.iloc[:10].copy()
        bad.iloc[3, bad.columns.get_loc("low")] = 0.0
        with self.assertRaises(ValueError):
            normalize_ohlcv(bad, ApexConfig(strict_ohlcv=True))

    def test_bearish_option_intent_is_buy_pe(self):
        latest = {
            "signal": "SELL",
            "timestamp": pd.Timestamp("2026-01-02 10:00", tz=IST),
            "signal_score": 81.0,
            "setup": "Breakdown",
            "close": 50000.0,
            "planned_sl1": 50100.0,
            "planned_tp1": 49800.0,
            "planned_tp2": 49700.0,
            "planned_tp3": 49600.0,
            "option_type": "PE",
            "option_strike": 50000.0,
        }
        result = SymbolResult(
            symbol="BANKNIFTY",
            instrument=InstrumentProfile("Bank Nifty", True, 100.0),
            frame=pd.DataFrame(),
            trades=[],
            active_trade=None,
            pending_order=None,
            forecast=SwingForecast(),
            stats=PerformanceStats(),
            latest=latest,
        )
        intent = result.order_intent()
        self.assertIsNotNone(intent)
        self.assertEqual(intent.side, "BUY")
        self.assertEqual(intent.underlying_action, "SHORT")
        self.assertEqual(intent.execution_instrument, "OPTION_GUIDANCE")

    def test_json_serialization_has_no_nan_tokens(self):
        payload = result_to_json(self.result)
        decoded = json.loads(payload)
        self.assertEqual(decoded["symbol"], "NIFTY50")
        self.assertNotIn("NaN", payload)
        self.assertNotIn("Infinity", payload)

    def test_historical_prefix_invariance(self):
        prefix = self.data.iloc[:1400]
        prefix_result = self.scanner.run_symbol("NIFTY50", prefix, asset_type="index")
        columns = ["bull_score", "bear_score", "ema9_15m_prev", "regime_1d"]
        left = prefix_result.frame.iloc[:-30][columns]
        right = self.result.frame.loc[left.index, columns]
        np.testing.assert_allclose(left.to_numpy(), right.to_numpy(), equal_nan=True, rtol=1e-10, atol=1e-10)

    def test_multi_symbol_isolation(self):
        scaled = self.data.copy()
        scaled[["open", "high", "low", "close"]] *= 2.0
        board, results, errors = self.scanner.scan_many({"NIFTY50": self.data, "BANKNIFTY": scaled})
        self.assertFalse(errors)
        self.assertEqual(len(board), 2)
        self.assertEqual(set(results), {"NIFTY50", "BANKNIFTY"})

    def test_optional_live_window_caps_computation(self):
        cfg = ApexConfig(
            use_session=False,
            min_score=58.0,
            conflict_margin=12.0,
            max_input_bars=600,
        )
        result = ApexScanner(cfg).run_symbol("NIFTY50", self.data, asset_type="index")
        self.assertEqual(len(result.frame), 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
