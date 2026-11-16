"""Error-rate maths shared by the synthetic evaluation, the volunteer study and their tests.

Vocabulary (the behaviour model flags a sample when its anomaly score is at or above a threshold):
- false flag rate: share of a person's OWN attempts that get flagged (annoyance; biometrics "FRR").
- miss rate: share of OTHER people's attempts that are NOT flagged (risk; biometrics "FAR").
- EER: the threshold where the two are equal, and the rate there. One number to compare models.
"""
from __future__ import annotations

import math
import random
from typing import Callable, Sequence

import numpy as np


def eer(genuine: Sequence[float], impostor: Sequence[float]) -> tuple[float, float]:
    """(equal error rate, threshold where it occurs)."""
    if len(genuine) == 0 or len(impostor) == 0:
        raise ValueError("need genuine and impostor scores")
    g, i = np.sort(np.asarray(genuine, float)), np.sort(np.asarray(impostor, float))
    t = np.unique(np.concatenate([g, i]))          # every possible threshold, ascending
    frr = 1 - np.searchsorted(g, t, "left") / len(g)  # own attempts at or above t
    far = np.searchsorted(i, t, "left") / len(i)      # other people's attempts below t
    k = int(np.argmin(np.abs(frr - far)))             # first minimum, same tie-break as a plain loop
    return float((frr[k] + far[k]) / 2), float(t[k])


def rate_at(threshold: float, scores: Sequence[float]) -> float:
    """Share of scores at or above the threshold (flagged)."""
    return sum(s >= threshold for s in scores) / len(scores) if scores else float("nan")


def bootstrap_ci(
    people: Sequence, stat: Callable[[Sequence], float], iterations: int = 2000, seed: int = 0, level: float = 0.95
) -> tuple[float, float, float]:
    """(estimate, low, high). Resamples PEOPLE, not samples: one person's attempts are correlated, so
    resampling attempts would give a falsely narrow interval."""
    est = stat(people)
    rnd = random.Random(seed)
    draws = (stat([rnd.choice(people) for _ in people]) for _ in range(iterations))
    vals = sorted(v for v in draws if not math.isnan(v))  # a resample can lack data of one kind
    if not vals:
        return est, float("nan"), float("nan")
    lo = vals[int((1 - level) / 2 * len(vals))]
    hi = vals[min(len(vals) - 1, int((1 + level) / 2 * len(vals)))]
    return est, lo, hi
