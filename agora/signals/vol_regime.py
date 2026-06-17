"""
Vol Regime Classifier — XGBoost model trained on structured market features.

Replaces the LLM regime classifier (uncalibrated confidence scores) with
a proper ML model that produces calibrated probabilities via Platt scaling.

Features:
  - iv_rank_real:    ATM IV rank from options chain (0-100)
  - vix_vix3m_ratio: VIX / VIX3M — term structure shape
  - hv10_hv30_ratio: short-term vs medium-term realized vol ratio
  - spy_rsi_14:      SPY 14-day RSI
  - iv_vs_hv:        ATM IV minus 21d HV (premium indicator)

Labels (4 regimes):
  0 = low_volatility   (IV rank < 30, VIX < 15, calm trending)
  1 = normal           (IV rank 30-60, typical)
  2 = high_volatility  (IV rank > 60, stressed but not panic)
  3 = crisis           (VIX > 30, backwardation, panic)

The model trains on historical data and is retrained weekly.
Until enough data is collected, falls back to rule-based classification.
"""

from __future__ import annotations

import json
import logging
import pickle
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_PATH  = Path(".agora/models/vol_regime.pkl")
_SCALER_PATH = Path(".agora/models/vol_regime_scaler.pkl")
_DATA_PATH   = Path(".agora/models/vol_regime_data.json")

REGIME_LABELS = {0: "low_volatility", 1: "normal", 2: "high_volatility", 3: "crisis"}
REGIME_REVERSE = {v: k for k, v in REGIME_LABELS.items()}


class VolRegimeClassifier:
    """
    Hybrid regime classifier:
      - XGBoost with Platt scaling when trained model exists
      - Rule-based fallback during cold start (< 60 days of data)
    """

    def __init__(self) -> None:
        self._model = None
        self._scaler = None
        self._load_model()

    def _load_model(self) -> None:
        try:
            if _MODEL_PATH.exists() and _SCALER_PATH.exists():
                # noqa rationale: the project's OWN locally-trained XGBoost model/scaler artifacts,
                # never untrusted/remote input — pickle is the model's native serialization format.
                with open(_MODEL_PATH, "rb") as f:
                    self._model = pickle.load(f)  # noqa: S301
                with open(_SCALER_PATH, "rb") as f:
                    self._scaler = pickle.load(f)  # noqa: S301
                logger.info("Vol regime XGBoost model loaded from %s", _MODEL_PATH)
        except Exception as exc:
            logger.warning("Could not load vol regime model: %s — using rule-based fallback", exc)

    def classify(
        self,
        iv_rank: float | None,
        vix: float | None,
        vix3m: float | None,
        hv10: float | None,
        hv30: float | None,
        spy_rsi: float | None,
        atm_iv: float | None,
        hv21: float | None,
    ) -> dict[str, Any]:
        """
        Classify current vol regime.

        Returns dict with:
          regime: str
          confidence: float (calibrated probability, 0-1)
          probabilities: dict of all regime probs
          method: "xgboost" | "rules"
          features_used: dict of input features
        """
        features = {
            "iv_rank":        iv_rank,
            "vix":            vix,
            "vix_vix3m_ratio": (vix / vix3m) if (vix and vix3m and vix3m > 0) else None,
            "hv10_hv30_ratio": (hv10 / hv30) if (hv10 and hv30 and hv30 > 0) else None,
            "spy_rsi":        spy_rsi,
            "iv_vs_hv":       (atm_iv - hv21) if (atm_iv and hv21) else None,
        }

        # Store data point for future training
        self._append_data(features)

        if self._model is not None and self._scaler is not None:
            return self._classify_xgboost(features)
        return self._classify_rules(features)

    def _classify_xgboost(self, features: dict) -> dict:
        try:
            import numpy as np
            feature_order = ["iv_rank", "vix_vix3m_ratio", "hv10_hv30_ratio", "spy_rsi", "iv_vs_hv"]
            x = np.array([[features.get(f) or 0.0 for f in feature_order]], dtype=float)
            x_scaled = self._scaler.transform(x)
            proba = self._model.predict_proba(x_scaled)[0]
            regime_idx = int(proba.argmax())
            return {
                "regime":        REGIME_LABELS[regime_idx],
                "confidence":    round(float(proba[regime_idx]), 3),
                "probabilities": {REGIME_LABELS[i]: round(float(p), 3) for i, p in enumerate(proba)},
                "method":        "xgboost",
                "features_used": features,
            }
        except Exception as exc:
            logger.warning("XGBoost classify failed: %s — falling back to rules", exc)
            return self._classify_rules(features)

    def _classify_rules(self, features: dict) -> dict:
        """Rule-based fallback using VIX term structure and IV rank."""
        iv_rank = features.get("iv_rank") or 50.0
        vix     = features.get("vix") or 20.0
        ratio   = features.get("vix_vix3m_ratio") or 1.0
        rsi     = features.get("spy_rsi") or 50.0

        if vix > 30 or ratio > 1.15:
            regime, confidence = "crisis", 0.80
        elif iv_rank > 65 or vix > 22:
            regime, confidence = "high_volatility", 0.70
        elif iv_rank < 25 and vix < 14:
            regime, confidence = "low_volatility", 0.70
        else:
            regime, confidence = "normal", 0.60

        # Adjust confidence down if signals conflict
        if regime == "low_volatility" and rsi < 40:
            confidence = max(0.40, confidence - 0.20)  # low IV but bearish RSI — uncertain
        if regime == "crisis" and ratio < 1.0:
            confidence = max(0.50, confidence - 0.15)  # high VIX but term structure normal

        return {
            "regime":        regime,
            "confidence":    confidence,
            "probabilities": {regime: confidence},
            "method":        "rules",
            "features_used": features,
        }

    def _append_data(self, features: dict) -> None:
        """Accumulate daily feature vectors for future XGBoost training."""
        _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: list = []
        if _DATA_PATH.exists():
            try:
                data = json.loads(_DATA_PATH.read_text())
            except Exception:
                data = []
        data.append({"date": date.today().isoformat(), **features})
        data = data[-504:]  # 2 years
        _DATA_PATH.write_text(json.dumps(data))

    def train(self, labeled_data: list[dict]) -> dict:
        """
        Train XGBoost on labeled historical data.

        labeled_data: list of dicts, each with feature keys + 'regime' label.
        Returns training metrics.

        Call this weekly once enough labeled data exists (≥ 200 days).
        """
        try:
            import numpy as np
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.preprocessing import StandardScaler
            from xgboost import XGBClassifier

            feature_order = ["iv_rank", "vix_vix3m_ratio", "hv10_hv30_ratio", "spy_rsi", "iv_vs_hv"]

            X, y = [], []
            for row in labeled_data:
                x_row = [row.get(f) or 0.0 for f in feature_order]
                label = REGIME_REVERSE.get(row.get("regime", "normal"), 1)
                X.append(x_row)
                y.append(label)

            X_arr = np.array(X, dtype=float)
            y_arr = np.array(y, dtype=int)

            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X_arr)

            base_model = XGBClassifier(
                n_estimators=200,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                use_label_encoder=False,
                eval_metric="mlogloss",
                random_state=42,
            )
            # Platt scaling for calibrated probabilities
            model = CalibratedClassifierCV(base_model, method="sigmoid", cv=5)
            model.fit(X_scaled, y_arr)

            _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_MODEL_PATH, "wb") as f:
                pickle.dump(model, f)
            with open(_SCALER_PATH, "wb") as f:
                pickle.dump(scaler, f)

            self._model  = model
            self._scaler = scaler

            # Accuracy on training set (for monitoring — not OOS)
            preds = model.predict(X_scaled)
            accuracy = float((preds == y_arr).mean())
            logger.info("Vol regime model trained: n=%d, train_accuracy=%.2f", len(y), accuracy)
            return {"n_samples": len(y), "train_accuracy": round(accuracy, 3)}

        except ImportError:
            logger.error("xgboost / sklearn not installed — cannot train. Run: pip install xgboost scikit-learn")
            return {"error": "missing dependencies"}
        except Exception as exc:
            logger.error("Training failed: %s", exc)
            return {"error": str(exc)}
