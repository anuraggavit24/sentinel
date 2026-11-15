import random

import pytest

from app.risk.behaviour import ENROL_MIN, BehaviourInvalid, IsolationForestModel, evaluate, validate
from app.risk.rules import Signals, score


def person(seed: int, **shift):
    """A synthetic typist with a personal rhythm and natural day-to-day noise."""
    rnd = random.Random(seed)
    base = {"dwellMeanMs": rnd.uniform(80, 140), "flightMeanMs": rnd.uniform(120, 260),
            "correctionRate": rnd.uniform(0.02, 0.1), "fillTimeMs": rnd.uniform(9000, 20000),
            "dwellStdMs": rnd.uniform(12, 30), "flightStdMs": rnd.uniform(40, 110),
            "idleBeforeSubmitMs": rnd.uniform(500, 1500), "pointerSpeedPxMs": rnd.uniform(0.6, 1.4)}
    base.update(shift)

    def sample(r=rnd):
        return {
            "keystrokes": 24,
            "dwellMeanMs": r.gauss(base["dwellMeanMs"], 6),
            "dwellStdMs": abs(r.gauss(base["dwellStdMs"], 3)),
            "flightMeanMs": r.gauss(base["flightMeanMs"], 15),
            "flightStdMs": abs(r.gauss(base["flightStdMs"], 8)),
            "correctionRate": min(1, max(0, r.gauss(base["correctionRate"], 0.015))),
            "fillTimeMs": max(300, r.gauss(base["fillTimeMs"], 1500)),
            "idleBeforeSubmitMs": max(0, r.gauss(base["idleBeforeSubmitMs"], 200)),
            "pointerSpeedPxMs": max(0, r.gauss(base["pointerSpeedPxMs"], 0.08)),
            "pasted": False,
        }

    return sample


def clean(samples):
    return [validate(s) for s in samples]


def test_validation_rejects_out_of_range_and_non_numbers():
    s = person(1)()
    assert validate(s)["dwellMeanMs"] == s["dwellMeanMs"]
    for bad in [{**s, "dwellMeanMs": 5000}, {**s, "flightMeanMs": float("nan")}, {**s, "fillTimeMs": "9000"},
                {**s, "correctionRate": True}]:
        with pytest.raises(BehaviourInvalid):
            validate(bad)


def test_too_little_typing_gives_no_behaviour_sample():
    assert validate({**person(1)(), "keystrokes": 3}) is None
    assert validate(None) is None


def test_learning_until_enough_clean_samples():
    alice = person(1)
    r = evaluate(clean([alice() for _ in range(ENROL_MIN - 1)]), validate(alice()))
    assert r.model == "learning" and r.anomaly is None


def test_robust_z_genuine_low_impostor_high():
    alice, mallory = person(1), person(2, dwellMeanMs=200, flightMeanMs=420, fillTimeMs=4000)
    train = clean([alice() for _ in range(10)])
    genuine = evaluate(train, validate(alice()))
    impostor = evaluate(train, validate(mallory()))
    assert genuine.model == "robust-z" and genuine.anomaly < 0.3
    assert impostor.anomaly > 0.5 and impostor.top_features


def test_robust_z_stays_the_model_with_long_history_and_uses_a_recent_window():
    alice = person(3)
    r = evaluate(clean([alice() for _ in range(80)]), validate(alice()))
    assert r.model == "robust-z" and r.samples == 80 and r.anomaly < 0.3


def test_isolation_forest_is_available_for_evaluation():
    alice, mallory = person(3), person(4, dwellMeanMs=210, flightMeanMs=450, pointerSpeedPxMs=3.0)
    model = IsolationForestModel(clean([alice() for _ in range(30)]))
    assert model.score(validate(mallory()))[0] > model.score(validate(alice()))[0]


def test_rule_uses_behaviour_only_when_scored():
    base = dict(amount_paise=500_00, hour_ist=14, payee_type="UPI", payee_trusted=True)
    assert score(Signals(**base, behaviour_anomaly=None)).reasons == []
    assert score(Signals(**base, behaviour_anomaly=0.2)).reasons == []
    mid = score(Signals(**base, behaviour_anomaly=0.35, behaviour_features=["pause between keys"]))
    high = score(Signals(**base, behaviour_anomaly=0.6))
    assert [r.code for r in mid.reasons] == ["BEHAVIOUR_ANOMALY"] and mid.reasons[0].points == 10
    assert [r.code for r in high.reasons] == ["BEHAVIOUR_ANOMALY_HIGH"] and high.reasons[0].points == 20
    assert high.customer_reasons()[0]["code"] == "BEHAVIOUR_UNUSUAL"



def test_form_context_does_not_count_as_identity():
    """Same person, different form: slower form, long pause before submit, no mouse. Not flagged."""
    alice = person(1)
    train = clean([alice() for _ in range(8)])
    other_form = {**alice(), "fillTimeMs": 45000, "idleBeforeSubmitMs": 9000, "pointerSpeedPxMs": 0}
    assert evaluate(train, validate(other_form)).anomaly < 0.3
