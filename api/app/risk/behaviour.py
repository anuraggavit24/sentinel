"""Behaviour model (slice 2b): how this user types and moves the mouse on the payment form.

Privacy: the browser sends only timing statistics (how long keys are held, gaps between keys, how
often corrections are made, mouse speed). It never sends which keys were pressed or what was typed.

Model in use: robust z-score (median and MAD per feature, from 5 clean samples).
An Isolation Forest (scikit-learn) was also built and measured with `scripts/eval_behaviour.py`. On the
synthetic users it was WORSE (EER 10-14% vs 5-6%) and badly calibrated (flagged about half of genuine
attempts at 0.6), so it is not used. It stays here for re-evaluation on real volunteer data, where
feature interactions it can model may exist. See README for the numbers and their limits.

Only "clean" samples train the profile: payments that were allowed with no coaching or takeover
signals. Otherwise an attacker's own typing would gradually become "normal" for the victim.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

# name: (min, max, minimum spread, customer-facing description)
FEATURES: dict[str, tuple[float, float, float, str]] = {
    "dwellMeanMs": (20, 1000, 12, "how long keys are held"),
    "dwellStdMs": (0, 800, 6, "variation in how long keys are held"),
    "flightMeanMs": (10, 3000, 25, "pause between keys"),
    "flightStdMs": (0, 3000, 15, "variation in the pause between keys"),
    "correctionRate": (0, 1, 0.03, "how often you correct typing"),
    "fillTimeMs": (300, 600_000, 1500, "time taken to fill the form"),
    "idleBeforeSubmitMs": (0, 600_000, 800, "pause before pressing send"),
    "pointerSpeedPxMs": (0, 20, 0.15, "mouse speed"),
}
# Only rhythm features go into the model. Form time, the pause before submit and mouse speed depend on
# WHICH form is filled (fields, prompts, whether a mouse is used), not on WHO is typing: a profile built on
# the practice form misjudged the same person on the payment form (anomaly 0.98 for the owner). They are
# still validated and stored as context, but they are not identity.
MODEL_FEATURES = ["dwellMeanMs", "dwellStdMs", "flightMeanMs", "flightStdMs", "correctionRate"]
MIN_KEYSTROKES = 6
ENROL_MIN = 5          # clean samples before any behaviour score
PROFILE_WINDOW = 50    # most recent clean samples used for the profile (habits drift)


class BehaviourInvalid(ValueError):
    pass


def validate(raw: Optional[dict]) -> Optional[dict]:
    """Returns clean features, or None when nothing usable was sent. Out-of-range values are rejected
    rather than clamped, so a crafted payload cannot pull the profile towards extreme values."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise BehaviourInvalid("behaviour must be an object")
    keystrokes = raw.get("keystrokes")
    if not isinstance(keystrokes, int) or keystrokes < MIN_KEYSTROKES:
        return None  # too little typing to say anything (e.g. everything pasted)
    out: dict[str, float] = {}
    for name, (lo, hi, _, _) in FEATURES.items():
        v = raw.get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise BehaviourInvalid(f"{name} must be a finite number")
        if not lo <= v <= hi:
            raise BehaviourInvalid(f"{name} is outside {lo}..{hi}")
        out[name] = float(v)
    out["pasted"] = 1.0 if raw.get("pasted") is True else 0.0
    return out


def _matrix(samples: list[dict]) -> np.ndarray:
    return np.array([[s[n] for n in MODEL_FEATURES] for s in samples], dtype=float)


def _robust_scale(train: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    med = np.median(train, axis=0)
    mad = np.median(np.abs(train - med), axis=0) * 1.4826
    floor = np.array([FEATURES[n][2] for n in MODEL_FEATURES])
    return med, np.maximum(mad, floor)


@dataclass
class BehaviourResult:
    model: str                 # "learning" | "robust-z" | "isolation-forest"
    samples: int
    anomaly: Optional[float]   # 0 (typical for you) .. 1 (very unusual); None while learning
    top_features: list[str]

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "samples": self.samples,
            "anomaly": None if self.anomaly is None else round(self.anomaly, 3),
            "topFeatures": self.top_features,
        }


class RobustZModel:
    """Median/MAD per feature. Score = mean of the three largest robust z-scores, squashed to 0..1."""

    name = "robust-z"

    def __init__(self, train: list[dict]):
        self.med, self.scale = _robust_scale(_matrix(train))

    def score(self, sample: dict) -> tuple[float, list[str]]:
        z = np.abs((_matrix([sample])[0] - self.med) / self.scale)
        top_idx = np.argsort(-z)[:3]
        d = float(np.mean(z[top_idx]))  # one odd feature is normal; several at once is not
        anomaly = 1 / (1 + math.exp(-(d - 3.0)))  # about 3 robust SDs -> 0.5
        return anomaly, [MODEL_FEATURES[i] for i in top_idx if z[i] >= 2.5]


class IsolationForestModel:
    """scikit-learn Isolation Forest on robust-scaled features; anomaly is a percentile against the
    user's own history, so 0.9 means 'less typical than 90% of your clean samples'."""

    name = "isolation-forest"

    def __init__(self, train: list[dict], seed: int = 0):
        from sklearn.ensemble import IsolationForest

        X = _matrix(train)
        self.med, self.scale = _robust_scale(X)
        Xs = (X - self.med) / self.scale
        self.forest = IsolationForest(n_estimators=150, random_state=seed).fit(Xs)
        self.train_scores = self.forest.score_samples(Xs)

    def score(self, sample: dict) -> tuple[float, list[str]]:
        xs = (_matrix([sample])[0] - self.med) / self.scale
        s = float(self.forest.score_samples(xs.reshape(1, -1))[0])
        worse_than = float(np.mean(self.train_scores > s))
        anomaly = min(1.0, worse_than + max(0.0, (self.train_scores.min() - s) * 4))
        z = np.abs(xs)
        return anomaly, [MODEL_FEATURES[i] for i in np.argsort(-z)[:3] if z[i] >= 2.5]


def score_robust_z(train: list[dict], sample: dict) -> tuple[float, list[str]]:
    return RobustZModel(train).score(sample)


def score_iforest(train: list[dict], sample: dict, seed: int = 0) -> tuple[float, list[str]]:
    return IsolationForestModel(train, seed).score(sample)


def evaluate(train: list[dict], sample: Optional[dict]) -> BehaviourResult:
    n = len(train)
    if sample is None:
        return BehaviourResult("none", n, None, [])
    if n < ENROL_MIN:
        return BehaviourResult("learning", n, None, [])
    a, top = RobustZModel(train[-PROFILE_WINDOW:]).score(sample)
    return BehaviourResult("robust-z", n, a, top)


def describe(features: list[str]) -> str:
    return ", ".join(FEATURES[f][3] for f in features[:2])
