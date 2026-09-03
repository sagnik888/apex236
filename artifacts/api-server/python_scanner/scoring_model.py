"""Calibrated signal scoring — rebuilt with XGBoost."""
from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd
import xgboost as xgb

def rank_ic(preds: np.ndarray, actuals: np.ndarray) -> float:
    if len(preds) < 3: return 0.0
    pr = pd.Series(preds).rank().values.astype(float)
    ar = pd.Series(actuals).rank().values.astype(float)
    pr -= pr.mean(); ar -= ar.mean()
    denom = np.sqrt((pr**2).sum() * (ar**2).sum())
    return float((pr * ar).sum() / denom) if denom > 0 else 0.0

def auc(pred: np.ndarray, label: np.ndarray) -> float:
    pos, neg = label > 0.5, label <= 0.5
    n_pos, n_neg = int(pos.sum()), int(neg.sum())
    if n_pos == 0 or n_neg == 0: return 0.5
    ranks = pd.Series(pred).rank().values
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))

def ic_significance(ic: float, n: int) -> float:
    if n < 4 or abs(ic) >= 1.0: return 0.0
    return float(ic * math.sqrt((n - 2) / max(1e-12, 1 - ic * ic)))

@dataclass
class ScoreCalibration:
    bands: list[tuple[float, float]] = field(default_factory=list)
    win_rate: list[float] = field(default_factory=list)
    expectancy: list[float] = field(default_factory=list)
    count: list[int] = field(default_factory=list)

    def describe(self, score: float) -> dict:
        for i, (lo, hi) in enumerate(self.bands):
            if lo <= score <= hi:
                return {
                    "band": f"{lo:.0f}-{hi:.0f}",
                    "historical_win_rate_pct": round(self.win_rate[i], 1),
                    "historical_expectancy_pct": round(self.expectancy[i], 4),
                    "sample": self.count[i],
                }
        return {}

@dataclass
class ApexScoreModel:
    feature_names: list[str] = field(default_factory=list)
    xgb_reg: Optional[xgb.XGBRegressor] = None
    xgb_clf: Optional[xgb.XGBClassifier] = None
    score_knots: Optional[np.ndarray] = None
    calibration: ScoreCalibration = field(default_factory=ScoreCalibration)
    trained_on: int = 0
    diagnostics: dict = field(default_factory=dict)

    def fit(self, X: np.ndarray, net_return: np.ndarray, feature_names: Sequence[str], alpha: float = 25.0) -> "ApexScoreModel":
        self.feature_names = list(feature_names)
        self.xgb_reg = xgb.XGBRegressor(n_estimators=50, max_depth=2, learning_rate=0.05, subsample=0.7, colsample_bytree=0.5, reg_alpha=10.0, reg_lambda=10.0, min_child_weight=30, random_state=42)
        self.xgb_reg.fit(X, net_return)
        self.xgb_clf = xgb.XGBClassifier(n_estimators=50, max_depth=2, learning_rate=0.05, subsample=0.7, colsample_bytree=0.5, reg_alpha=10.0, reg_lambda=10.0, min_child_weight=30, random_state=42)
        self.xgb_clf.fit(X, (net_return > 0).astype(int))
        raw = self.xgb_reg.predict(X)
        self.score_knots = np.percentile(raw, np.linspace(0, 100, 101))
        self.trained_on = len(X)
        return self

    def expected_return(self, X: np.ndarray) -> np.ndarray:
        return self.xgb_reg.predict(X)

    def win_probability(self, X: np.ndarray) -> np.ndarray:
        return self.xgb_clf.predict_proba(X)[:, 1]

    def score(self, X: np.ndarray) -> np.ndarray:
        raw = self.expected_return(X)
        return np.clip(np.interp(raw, self.score_knots, np.linspace(0, 100, 101)), 0, 100)

    def calibrate(self, X: np.ndarray, net_return: np.ndarray, bins: int = 5) -> ScoreCalibration:
        scores = self.score(X)
        edges = np.linspace(0, 100, bins + 1)
        cal = ScoreCalibration()
        for i in range(bins):
            lo, hi = edges[i], edges[i + 1]
            mask = (scores >= lo) & (scores <= hi if i == bins - 1 else scores < hi)
            if mask.sum() == 0: continue
            sub = net_return[mask]
            cal.bands.append((float(lo), float(hi)))
            cal.win_rate.append(float((sub > 0).mean() * 100))
            cal.expectancy.append(float(sub.mean()))
            cal.count.append(int(mask.sum()))
        self.calibration = cal
        return cal

    def evaluate(self, X: np.ndarray, net_return: np.ndarray, label: str = "") -> dict:
        pred = self.expected_return(X)
        ic = rank_ic(pred, net_return)
        area = auc(self.win_probability(X), (net_return > 0).astype(float))
        scores = self.score(X)
        deciles = []
        for k in range(10):
            mask = (scores >= k * 10) & (scores < (k + 1) * 10 if k < 9 else scores <= 100)
            if mask.sum() >= 10:
                deciles.append({"decile": k + 1, "n": int(mask.sum()), "win_rate": float((net_return[mask] > 0).mean() * 100), "expectancy": float(net_return[mask].mean())})
        top = [d for d in deciles if d["decile"] >= 9]
        bottom = [d for d in deciles if d["decile"] <= 2]
        lift = float(np.mean([d["expectancy"] for d in top]) - np.mean([d["expectancy"] for d in bottom])) if top and bottom else 0.0
        report = {
            "label": label, "n": int(len(net_return)), "rank_ic": round(ic, 4), "ic_t_stat": round(ic_significance(ic, len(net_return)), 2),
            "auc": round(area, 4), "top_minus_bottom_expectancy": round(lift, 4), "deciles": deciles,
            "informative": bool(abs(ic_significance(ic, len(net_return))) > 2.5 and area > 0.53)
        }
        self.diagnostics[label or "eval"] = report
        return report

    def save(self, path: Path) -> None:
        def _c(arr): return np.nan_to_num(arr, nan=0.0).tolist() if arr is not None else []
        with open(path.with_suffix('.pkl'), 'wb') as f: pickle.dump((self.xgb_reg, self.xgb_clf), f)
        path.write_text(json.dumps({
            "feature_names": self.feature_names, "score_knots": _c(self.score_knots), "trained_on": self.trained_on,
            "calibration": {"bands": self.calibration.bands, "win_rate": self.calibration.win_rate, "expectancy": self.calibration.expectancy, "count": self.calibration.count},
            "diagnostics": self.diagnostics
        }, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "ApexScoreModel":
        blob = json.loads(path.read_text(encoding="utf-8"))
        model = cls(feature_names=blob["feature_names"], score_knots=np.array(blob["score_knots"]), trained_on=blob.get("trained_on", 0), diagnostics=blob.get("diagnostics", {}))
        with open(path.with_suffix('.pkl'), 'rb') as f: model.xgb_reg, model.xgb_clf = pickle.load(f)
        cal = blob.get("calibration", {})
        model.calibration = ScoreCalibration(bands=[tuple(b) for b in cal.get("bands", [])], win_rate=cal.get("win_rate", []), expectancy=cal.get("expectancy", []), count=cal.get("count", []))
        return model

    def weight_table(self) -> list[tuple[str, float, float]]:
        if not self.xgb_reg: return []
        importances = self.xgb_reg.feature_importances_
        rows = [(name, float(importances[i]), 0.0) for i, name in enumerate(self.feature_names)]
        rows.sort(key=lambda r: -abs(r[1]))
        return rows
