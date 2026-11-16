"""Measure the behaviour models on SYNTHETIC users. Reproducible: python scripts/eval_behaviour.py

What this proves: the pipeline works and the two models rank genuine vs other-person samples as
expected on data with a known structure. What it does NOT prove: accuracy on real people. Real error
rates need real volunteers (README: volunteer collection). Never quote these numbers as real-world.

Each synthetic user has a personal rhythm (key hold time and its variation, pause between keys and its
variation, corrections) plus day-to-day noise. Only these rhythm features are scored (see behaviour.py). "Impostors" are other synthetic users typing on the same form.
"""
from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.risk.behaviour import IsolationForestModel, RobustZModel, validate  # noqa: E402
from app.risk.metrics import eer, rate_at  # noqa: E402


def make_user(rnd: random.Random):
    base = {
        "dwell": rnd.uniform(70, 160), "flight": rnd.uniform(100, 320), "corr": rnd.uniform(0.01, 0.15),
        "fill": rnd.uniform(6000, 25000), "idle": rnd.uniform(300, 2500), "speed": rnd.uniform(0.4, 1.8),
        "dstd": rnd.uniform(8, 40), "fstd": rnd.uniform(30, 140),
    }
    noise = rnd.uniform(0.8, 1.6)  # some people are more consistent than others

    def sample():
        return validate({
            "keystrokes": 24,
            "dwellMeanMs": max(20, rnd.gauss(base["dwell"], 8 * noise)),
            "dwellStdMs": abs(rnd.gauss(base["dstd"], 3 * noise)),
            "flightMeanMs": max(10, rnd.gauss(base["flight"], 22 * noise)),
            "flightStdMs": abs(rnd.gauss(base["fstd"], 10 * noise)),
            "correctionRate": min(1, max(0, rnd.gauss(base["corr"], 0.02 * noise))),
            "fillTimeMs": max(300, rnd.gauss(base["fill"], 2200 * noise)),
            "idleBeforeSubmitMs": max(0, rnd.gauss(base["idle"], 300 * noise)),
            "pointerSpeedPxMs": max(0, rnd.gauss(base["speed"], 0.12 * noise)),
        })

    return sample


def run(users: int = 60, tests: int = 20, seed: int = 7) -> None:
    rnd = random.Random(seed)
    people = [make_user(rnd) for _ in range(users)]
    print(f"Synthetic evaluation: {users} users, {tests} genuine + {tests} impostor attempts each, seed {seed}\n")
    print(f"{'model':<18}{'history':>8}{'EER':>7}{'at':>6}   {'anomaly >= 0.3':^25}   {'anomaly >= 0.5':^25}")
    print(f"{'':<18}{'':>8}{'':>7}{'':>6}   {'genuine':>11}{'impostor':>14}   {'genuine':>11}{'impostor':>14}")
    for model_cls, sizes in [(RobustZModel, [5, 10, 19, 40]), (IsolationForestModel, [20, 40])]:
        for n in sizes:
            gen, imp = [], []
            for i, p in enumerate(people):
                model = model_cls([p() for _ in range(n)])
                others = [q for j, q in enumerate(people) if j != i]
                for _ in range(tests):
                    gen.append(model.score(p())[0])
                    imp.append(model.score(rnd.choice(others)())[0])
            e, t = eer(gen, imp)
            print(f"{model_cls.name:<18}{n:>8}{e:>7.1%}{t:>6.2f}   {rate_at(0.3, gen):>11.1%}{rate_at(0.3, imp):>14.1%}"
                  f"   {rate_at(0.5, gen):>11.1%}{rate_at(0.5, imp):>14.1%}")
    print("\nThresholds 0.3 / 0.5 were chosen on a different seed (11) than this report (7).")
    print("SYNTHETIC DATA ONLY. Real-world error rates: scripts/eval_study.py on volunteer data.")


if __name__ == "__main__":
    run(users=int(sys.argv[1]) if len(sys.argv) > 1 else 60)
