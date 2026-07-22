"""Calibrated signal scoring — a rebuild of the hand-weighted APEX score.

Why a rebuild
-------------
The legacy score adds hand-picked constants (18 for MACD acceleration, 14 for
VWAP deviation, 15 for a strong pattern, ...) and clips the sum at 100. Its
components are strongly correlated — in any trend the EMA stack, MACD, ADX and
VWAP terms all fire together — so scores pile up against the cap. Measured on
cached history, 75% of signals landed in the 90-100 bucket and that bucket
performed no better than the 65-70 bucket. A number that does not separate
outcomes is not a ranking.

What this provides instead
--------------------------
* Features standardised with TRAIN-window statistics only (no leakage).
* Weights fitted from data by ridge regression (expected net return) and
  ridge logistic regression (probability of a profitable trade), rather than
  chosen by hand.
* Decorrelation is handled by the ridge penalty, which shrinks the weights of
  redundant, collinear inputs instead of letting them each contribute in full.
* A calibration table mapping the 0-100 output to REALISED win rate and
  expectancy, so "score 80" carries a stated, checkable meaning.
* Honest diagnostics: rank IC, AUC and decile lift, all reported out-of-sample.
  When the inputs carry no information these come out flat, and the model is
  expected to say so rather than manufacture confidence.

No sklearn dependency; the estimators are small and implemented directly.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Estimators
# ---------------------------------------------------------------------------

def ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    """Closed-form ridge regression. X must already include an intercept column.

    The intercept (column 0) is left unpenalised.
    """
    n_features = X.shape[1]
    penalty = np.eye(n_features) * alpha
    penalty[0, 0] = 0.0
    return np.linalg.solve(X.T @ X + penalty, X.T @ y)


def logistic_fit(
    X: np.ndarray,
    y: np.ndarray,
    alpha: float,
    iterations: int = 200,
    tol: float = 1e-8,
) -> np.ndarray:
    """L2-regularised logistic regression via Newton/IRLS.

    Falls back to the last stable iterate if the Hessian becomes singular.
    """
    n_features = X.shape[1]
    weights = np.zeros(n_features)
    penalty = np.eye(n_features) * alpha
    penalty[0, 0] = 0.0
    for _ in range(iterations):
        eta = np.clip(X @ weights, -30.0, 30.0)
        p = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(p * (1.0 - p), 1e-6, None)
        gradient = X.T @ (y - p) - penalty @ weights
        hessian = (X * w[:, None]).T @ X + penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        weights = weights + step
        if np.max(np.abs(step)) < tol:
            break
    return weights


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def rank_ic(pred: np.ndarray, actual: np.ndarray) -> float:
    """Spearman rank correlation between prediction and realised outcome."""
    if len(pred) < 3:
        return 0.0
    pr = np.argsort(np.argsort(pred)).astype(float)
    ar = np.argsort(np.argsort(actual)).astype(float)
    pr -= pr.mean(); ar -= ar.mean()
    denom = np.sqrt((pr @ pr) * (ar @ ar))
    return float(pr @ ar / denom) if denom > 0 else 0.0


def auc(pred: np.ndarray, label: np.ndarray) -> float:
    """Area under the ROC curve via the rank-sum identity."""
    pos, neg = label > 0.5, label <= 0.5
    n_pos, n_neg = int(pos.sum()), int(neg.sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5
    ranks = np.argsort(np.argsort(pred)).astype(float) + 1.0
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def ic_significance(ic: float, n: int) -> float:
    """t-statistic for a rank correlation."""
    if n < 4 or abs(ic) >= 1.0:
        return 0.0
    return float(ic * math.sqrt((n - 2) / max(1e-12, 1 - ic * ic)))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class ScoreCalibration:
    """Realised behaviour per score band, measured out-of-sample."""
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
    """Fitted, calibrated replacement for the hand-weighted score."""

    feature_names: list[str] = field(default_factory=list)
    mean: Optional[np.ndarray] = None
    std: Optional[np.ndarray] = None
    ridge_weights: Optional[np.ndarray] = None
    logit_weights: Optional[np.ndarray] = None
    score_knots: Optional[np.ndarray] = None      # percentile grid of raw EV
    calibration: ScoreCalibration = field(default_factory=ScoreCalibration)
    trained_on: int = 0
    diagnostics: dict = field(default_factory=dict)

    # -- internals ---------------------------------------------------------

    def _design(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.mean) / self.std
        Z = np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)
        Z = np.clip(Z, -4.0, 4.0)   # winsorise; single outliers must not dominate
        return np.column_stack([np.ones(len(Z)), Z])

    # -- fitting -----------------------------------------------------------

    def fit(
        self,
        X: np.ndarray,
        net_return: np.ndarray,
        feature_names: Sequence[str],
        alpha: float = 25.0,
    ) -> "ApexScoreModel":
        self.feature_names = list(feature_names)
        self.mean = np.nanmean(X, axis=0)
        std = np.nanstd(X, axis=0)
        self.std = np.where(std < 1e-9, 1.0, std)
        design = self._design(X)
        self.ridge_weights = ridge_fit(design, net_return, alpha)
        self.logit_weights = logistic_fit(design, (net_return > 0).astype(float), alpha)
        raw = design @ self.ridge_weights
        # Map raw expected value onto 0-100 by its own training distribution,
        # so the score is a percentile rank with a stable, readable spread
        # (the legacy score's saturation problem cannot recur by construction).
        self.score_knots = np.percentile(raw, np.linspace(0, 100, 101))
        self.trained_on = len(X)
        return self

    # -- inference ---------------------------------------------------------

    def expected_return(self, X: np.ndarray) -> np.ndarray:
        return self._design(X) @ self.ridge_weights

    def win_probability(self, X: np.ndarray) -> np.ndarray:
        eta = np.clip(self._design(X) @ self.logit_weights, -30.0, 30.0)
        return 1.0 / (1.0 + np.exp(-eta))

    def score(self, X: np.ndarray) -> np.ndarray:
        """0-100 percentile score. Monotonic in expected return by construction."""
        raw = self.expected_return(X)
        return np.clip(np.interp(raw, self.score_knots, np.linspace(0, 100, 101)), 0, 100)

    # -- calibration -------------------------------------------------------

    def calibrate(self, X: np.ndarray, net_return: np.ndarray, bins: int = 5) -> ScoreCalibration:
        scores = self.score(X)
        edges = np.linspace(0, 100, bins + 1)
        cal = ScoreCalibration()
        for i in range(bins):
            lo, hi = edges[i], edges[i + 1]
            mask = (scores >= lo) & (scores <= hi if i == bins - 1 else scores < hi)
            if mask.sum() == 0:
                continue
            sub = net_return[mask]
            cal.bands.append((float(lo), float(hi)))
            cal.win_rate.append(float((sub > 0).mean() * 100))
            cal.expectancy.append(float(sub.mean()))
            cal.count.append(int(mask.sum()))
        self.calibration = cal
        return cal

    def evaluate(self, X: np.ndarray, net_return: np.ndarray, label: str = "") -> dict:
        """Out-of-sample discrimination report."""
        pred = self.expected_return(X)
        ic = rank_ic(pred, net_return)
        area = auc(self.win_probability(X), (net_return > 0).astype(float))
        scores = self.score(X)
        deciles = []
        for k in range(10):
            mask = (scores >= k * 10) & (scores < (k + 1) * 10 if k < 9 else scores <= 100)
            if mask.sum() >= 10:
                deciles.append({
                    "decile": k + 1,
                    "n": int(mask.sum()),
                    "win_rate": float((net_return[mask] > 0).mean() * 100),
                    "expectancy": float(net_return[mask].mean()),
                })
        top = [d for d in deciles if d["decile"] >= 9]
        bottom = [d for d in deciles if d["decile"] <= 2]
        lift = (
            float(np.mean([d["expectancy"] for d in top]) - np.mean([d["expectancy"] for d in bottom]))
            if top and bottom else 0.0
        )
        report = {
            "label": label,
            "n": int(len(net_return)),
            "rank_ic": round(ic, 4),
            "ic_t_stat": round(ic_significance(ic, len(net_return)), 2),
            "auc": round(area, 4),
            "top_minus_bottom_expectancy": round(lift, 4),
            "deciles": deciles,
            "informative": bool(abs(ic_significance(ic, len(net_return))) > 2.5 and area > 0.53),
        }
        self.diagnostics[label or "eval"] = report
        return report

    # -- persistence -------------------------------------------------------

    def save(self, path: Path) -> None:
        def _clean_array(arr: Optional[np.ndarray]) -> list:
            if arr is None:
                return []
            cleaned = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
            return cleaned.tolist()

        path.write_text(json.dumps({
            "feature_names": self.feature_names,
            "mean": _clean_array(self.mean),
            "std": _clean_array(self.std),
            "ridge_weights": _clean_array(self.ridge_weights),
            "logit_weights": _clean_array(self.logit_weights),
            "score_knots": _clean_array(self.score_knots),
            "trained_on": self.trained_on,
            "calibration": {
                "bands": self.calibration.bands,
                "win_rate": self.calibration.win_rate,
                "expectancy": self.calibration.expectancy,
                "count": self.calibration.count,
            },
            "diagnostics": self.diagnostics,
        }, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "ApexScoreModel":
        blob = json.loads(path.read_text(encoding="utf-8"))
        model = cls(
            feature_names=blob["feature_names"],
            mean=np.array(blob["mean"]),
            std=np.array(blob["std"]),
            ridge_weights=np.array(blob["ridge_weights"]),
            logit_weights=np.array(blob["logit_weights"]),
            score_knots=np.array(blob["score_knots"]),
            trained_on=blob.get("trained_on", 0),
            diagnostics=blob.get("diagnostics", {}),
        )
        cal = blob.get("calibration", {})
        model.calibration = ScoreCalibration(
            bands=[tuple(b) for b in cal.get("bands", [])],
            win_rate=cal.get("win_rate", []),
            expectancy=cal.get("expectancy", []),
            count=cal.get("count", []),
        )
        return model

    def weight_table(self) -> list[tuple[str, float, float]]:
        """(feature, ridge weight, logistic weight) sorted by absolute impact."""
        rows = [
            (name, float(self.ridge_weights[i + 1]), float(self.logit_weights[i + 1]))
            for i, name in enumerate(self.feature_names)
        ]
        rows.sort(key=lambda r: -abs(r[1]))
        return rows
