"""Train and validate the rebuilt score; compare it against the legacy score.
Uses Walk-Forward Validation instead of single train/test split.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit

from apex_python_scanner import ApexConfig, ApexScanner
from scoring_model import ApexScoreModel, auc, ic_significance, rank_ic
from simulation_engine import simulate

HERE = Path(__file__).resolve().parent
CACHE = HERE / "angel_cache"
MODEL_PATH = HERE / "apex_score_model.json"
COST = 0.182
TP, SL = 2.00, 1.00

FEATURES = [
    "legacy_score", "adx", "di_spread", "rsi_dir", "rvol", "atr_exp", "atr_pct",
    "body_pos_dir", "body_atr", "macd_hist_n", "dist_ema21", "dist_ema50",
    "dist_ema200", "dist_vwap", "hour", "is_long", "aligned_trend",
    "strong_pattern", "is_breakout", "is_breakdown", "is_trend_setup",
    "regime_1h_trend", "regime_1d_trend", "score_gap",
    "cmf", "rs_proxy", "vwap_zscore",
]

def load_cached(interval: str, limit: int):
    frames = {}
    for path in sorted(CACHE.glob(f"*__{interval}.pkl")):
        try:
            f = pd.read_pickle(path)
            if isinstance(f, pd.DataFrame) and len(f) > 400:
                frames[path.name.split("__")[0]] = f
        except Exception: continue
        if len(frames) >= limit: break
    return frames

def build_dataset(limit: int) -> pd.DataFrame:
    universe = load_cached("15m", limit)
    cfg = ApexConfig(min_score=60.0, conflict_margin=12.0, use_htf=True, entry_delay_bars=1, realistic_fills=True, use_session=True, enforce_market_hours=True, keep_full_history=True, max_input_bars=0, strict_ohlcv=False, min_history_bars=210, fixed_sl_pct=0.5, force_fixed_sl=True, fixed_tp_pct=1.0, exit_at_t1=True, use_trail=False)
    scanner = ApexScanner(cfg)
    rows = []
    for symbol, frame in universe.items():
        try: res = scanner.run_symbol(symbol, frame, asset_type="stock")
        except Exception: continue
        f = res.frame
        o, h, l, c = (f[x].to_numpy() for x in ("open", "high", "low", "close"))
        sig = f["signal"].to_numpy(); setup = f["setup"].to_numpy()
        get = lambda n: f[n].to_numpy() if n in f.columns else np.full(len(f), np.nan)
        score_c, bull, bear = get("signal_score"), get("bull_score"), get("bear_score")
        cmf, rs_proxy, vwap_zscore = get("cmf"), get("rs_proxy"), get("vwap_zscore")
        adx, dip, dim = get("adx"), get("di_plus"), get("di_minus")
        rsi, rvol, atr_exp = get("rsi"), get("relative_volume"), get("atr_expansion")
        body_pos, body_abs, atr = get("body_position"), get("body_abs"), get("atr")
        macd_hist = get("macd_hist")
        ema21, ema50, ema200, vwap = get("ema21"), get("ema50"), get("ema200"), get("vwap")
        reg1h, reg1d = get("regime_1h"), get("regime_1d")
        sb = f["pattern_strong_bull"].to_numpy() if "pattern_strong_bull" in f else np.zeros(len(f), bool)
        sr = f["pattern_strong_bear"].to_numpy() if "pattern_strong_bear" in f else np.zeros(len(f), bool)
        bull_tr, bear_tr = get("bull_trend"), get("bear_trend")
        hours = (f.index.hour + f.index.minute / 60.0).to_numpy()

        # Train on actual signals PLUS a 10% sample of all bars to learn noise
        signal_indices = np.flatnonzero(pd.Series(sig).isin(["BUY", "SELL"]).to_numpy())
        sample_indices = np.random.choice(len(f), size=len(f)//10, replace=False)
        all_indices = np.unique(np.concatenate([signal_indices, sample_indices]))
        
        for i in all_indices:
            i = int(i)
            # Skip first 100 bars (burn-in)
            if i < 100: continue
            
            bars_to_eod = 200
            h_val = hours[i]
            if h_val < 15.5:
                bars_to_eod = int(max(1, (15.5 - h_val) * 4)) - 1
                if bars_to_eod <= 0: continue
            
            # Determine direction to test: if it's a real signal, test that direction.
            # If it's a random sample, randomly pick long or short.
            actual_sig = sig[i]
            if actual_sig == "BUY":
                dirs_to_test = [True]
            elif actual_sig == "SELL":
                dirs_to_test = [False]
            else:
                dirs_to_test = [np.random.random() > 0.5]
                
            for is_long in dirs_to_test:
                sim_res = simulate(o, h, l, c, i, is_long, TP, SL, max_bars=bars_to_eod, deduct_costs=False)
                if sim_res is None: continue
                out, _ = sim_res
            d = 1.0 if is_long else -1.0
            px = c[i]
            a = atr[i] if np.isfinite(atr[i]) and atr[i] > 0 else np.nan
            def reg_dir(code):
                if not np.isfinite(code): return 0.0
                base = int(code) % 10
                if base in (1, 3): return 1.0 * d
                if base in (2, 4): return -1.0 * d
                return 0.0
            rows.append({
                "ts": f.index[i], "symbol": symbol, "net": out - COST,
                "legacy_score": score_c[i],
                "adx": adx[i],
                "di_spread": (dip[i] - dim[i]) * d,
                "rsi_dir": rsi[i] if is_long else 100 - rsi[i],
                "rvol": rvol[i],
                "atr_exp": atr_exp[i],
                "atr_pct": a / px * 100 if (np.isfinite(a) and px) else np.nan,
                "body_pos_dir": body_pos[i] if is_long else 1 - body_pos[i],
                "body_atr": body_abs[i] / a if (np.isfinite(a) and a != 0) else np.nan,
                "macd_hist_n": macd_hist[i] / a * d if (np.isfinite(a) and a != 0) else np.nan,
                "dist_ema21": (px - ema21[i]) / px * 100 * d if px else np.nan,
                "dist_ema50": (px - ema50[i]) / px * 100 * d if px else np.nan,
                "dist_ema200": (px - ema200[i]) / px * 100 * d if px else np.nan,
                "dist_vwap": (px - vwap[i]) / px * 100 * d if px else np.nan,
                "hour": hours[i],
                "is_long": 1.0 if is_long else 0.0,
                "aligned_trend": 1.0 if (bull_tr[i] if is_long else bear_tr[i]) else 0.0,
                "strong_pattern": 1.0 if (sb[i] if is_long else sr[i]) else 0.0,
                "is_breakout": 1.0 if setup[i] == "Breakout" else 0.0,
                "is_breakdown": 1.0 if setup[i] == "Breakdown" else 0.0,
                "is_trend_setup": 1.0 if setup[i] == "Trend" else 0.0,
                "regime_1h_trend": reg_dir(reg1h[i]),
                "regime_1d_trend": reg_dir(reg1d[i]),
                "score_gap": (bull[i] - bear[i]) * d,
                "cmf": cmf[i] * d,
                "rs_proxy": rs_proxy[i] * d,
                "vwap_zscore": vwap_zscore[i] * d,
            })
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)

def decile_table(scores, net, title):
    print(f"\n  {title}")
    print(f"    {'decile':>7s} {'n':>6s} {'win%':>7s} {'expectancy':>12s}")
    for k in range(10):
        lo, hi = k * 10, (k + 1) * 10
        mask = (scores >= lo) & (scores <= hi if k == 9 else scores < hi)
        if mask.sum() < 10: continue
        sub = net[mask]
        print(f"    {k+1:7d} {int(mask.sum()):6d} {(sub>0).mean()*100:6.1f}% {sub.mean():+11.4f}%")

def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    df = build_dataset(limit)
    df = df.replace([np.inf, -np.inf], np.nan)
    print(f"Dataset {len(df)} signals | geometry {TP}%/{SL}% | cost {COST}%")

    X = df[FEATURES].to_numpy(dtype=float)
    y = df["net"].to_numpy(dtype=float)

    # Walk-forward validation
    tscv = TimeSeriesSplit(n_splits=5)
    oof_preds = np.full(len(df), np.nan)
    oof_probs = np.full(len(df), np.nan)
    
    print("\nRunning Walk-Forward Validation (5 folds)...")
    for i, (train_index, test_index) in enumerate(tscv.split(X)):
        X_tr, y_tr = X[train_index], y[train_index]
        X_te, y_te = X[test_index], y[test_index]
        
        model = ApexScoreModel().fit(X_tr, y_tr, FEATURES)
        oof_preds[test_index] = model.expected_return(X_te)
        oof_probs[test_index] = model.win_probability(X_te)
        
        ic = rank_ic(oof_preds[test_index], y_te)
        print(f"  Fold {i+1}: train={len(train_index)}, test={len(test_index)}, rank IC={ic:+.4f}")

    # Evaluate full OOF
    valid_idx = ~np.isnan(oof_preds)
    final_preds = oof_preds[valid_idx]
    final_probs = oof_probs[valid_idx]
    final_y = y[valid_idx]
    
    final_ic = rank_ic(final_preds, final_y)
    final_auc = auc(final_probs, (final_y > 0).astype(float))
    print(f"\n{'='*94}\nOUT-OF-SAMPLE (Walk-Forward Test)\n{'='*94}")
    print(f"  REBUILT score: rank IC={final_ic:+.4f} (t={ic_significance(final_ic, len(final_y)):+.2f}) AUC={final_auc:.4f}")

    # Train final model on ALL data
    final_model = ApexScoreModel().fit(X, y, FEATURES)
    final_model.calibrate(X, y, bins=5)
    final_model.save(MODEL_PATH)
    print(f"\n  Final Model saved to {MODEL_PATH.name}")

    informative = bool(abs(ic_significance(final_ic, len(final_y))) > 1.96 and final_auc > 0.515)
    print(f"\n{'='*94}\nVERDICT\n{'='*94}")
    print(f"  Rebuilt score informative out-of-sample: {'YES' if informative else 'NO'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
