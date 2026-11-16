"""Volunteer-study evaluation and the shared error-rate maths. No database needed."""
import importlib.util
import json
import math
import random
from pathlib import Path

import pytest

from app.risk.metrics import bootstrap_ci, eer, rate_at

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


study = load("eval_study")
synthetic = load("eval_behaviour")


def fake_export(people=8, sessions=(10, 10), seed=3, short_first=0):
    """Synthetic users dressed as a study export, to test the pipeline (not the model)."""
    rnd = random.Random(seed)
    out = []
    for i in range(people):
        make = synthetic.make_user(rnd)
        sizes = list(sessions)
        if i < short_first:
            sizes[0] = 4  # not enough for a profile
        out.append({
            "pid": f"p{i + 1:02d}", "keyboard": "laptop", "consentVersion": "v1",
            "sessions": [{"session": k + 1, "startHours": 24.0 * k,
                          "samples": [{"promptNo": j, **make()} for j in range(n)]} for k, n in enumerate(sizes)],
        })
    return {"format": "sentinel-typing-study/1", "participants": out}


def test_eer_perfect_separation_and_full_overlap():
    assert eer([0.1, 0.2], [0.8, 0.9])[0] == 0.0
    assert eer([0.5, 0.5], [0.5, 0.5])[0] == pytest.approx(0.5)


def test_rate_at_counts_flags_at_or_above_threshold():
    assert rate_at(0.5, [0.1, 0.5, 0.9, 0.2]) == 0.5
    assert math.isnan(rate_at(0.5, []))


def test_bootstrap_resamples_people_and_is_reproducible():
    people = [[0.0] * 50, [1.0] * 50]  # two very different people, many attempts each
    stat = lambda ps: sum(sum(p) / len(p) for p in ps) / len(ps)  # noqa: E731
    a, b = bootstrap_ci(people, stat, seed=1), bootstrap_ci(people, stat, seed=1)
    assert a == b
    est, lo, hi = a
    # Resampling PEOPLE keeps the interval wide (0 to 1): there are only two people. Resampling the
    # 100 attempts would have claimed false precision around 0.5.
    assert est == 0.5 and lo == 0.0 and hi == 1.0


def test_refuses_to_report_on_too_few_volunteers():
    with pytest.raises(study.InsufficientData):
        study.evaluate(fake_export(people=4))


def test_people_without_a_profile_are_excluded_not_counted():
    r = study.evaluate(fake_export(people=7, short_first=2))
    assert r["participants"] == 5


def test_protocol_splits_and_impostors_exclude_self():
    data = fake_export(people=6, sessions=(10, 10, 5))
    people = study.score_people(data)
    p = people[0]
    assert len(p["same"]) == 5             # session 1 minus the 5 profile samples
    assert len(p["later"]) == 15           # sessions 2 and 3
    assert len(p["impostor"]) == 5 * 25    # every sample of the 5 OTHER people, none of their own


def test_full_report_has_intervals_and_pilot_warning():
    r = study.evaluate(fake_export(people=10))
    assert r["pilot"] is True and r["withLaterSession"] == 10
    later = r["rates"]["0.5"]["falseFlagLater"]
    assert later["low"] <= later["value"] <= later["high"]
    assert 0 <= r["eer"]["value"] < 0.3   # synthetic users are separable; real ones may not be
    text = study.report(r)
    assert "PILOT" in text and "later session" in text and "imitate" in text


def test_main_rejects_files_that_are_not_study_exports(tmp_path, capsys):
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"participants": []}))
    assert study.main(["eval_study.py", str(f)]) == 2
    f.write_text(json.dumps(fake_export(people=3)))
    assert study.main(["eval_study.py", str(f)]) == 3
    assert "Not enough data" in capsys.readouterr().out


def test_fast_eer_matches_the_plain_loop():
    def slow(g, i):
        best = (2.0, 0.0, 0.5)
        for t in sorted(set(g + i)):
            frr = sum(x >= t for x in g) / len(g)
            far = sum(x < t for x in i) / len(i)
            if abs(frr - far) < best[0]:
                best = (abs(frr - far), (frr + far) / 2, t)
        return best[1], best[2]

    rnd = random.Random(5)
    for _ in range(30):
        g = [round(rnd.random() * 0.7, 2) for _ in range(rnd.randint(1, 40))]
        i = [round(0.2 + rnd.random() * 0.8, 2) for _ in range(rnd.randint(1, 60))]
        fast = eer(g, i)
        assert fast[0] == pytest.approx(slow(g, i)[0]) and fast[1] == pytest.approx(slow(g, i)[1])
