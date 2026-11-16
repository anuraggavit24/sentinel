"""Measure the behaviour model on REAL volunteers.

    curl -s -H "x-demo-operator-token: $TOKEN" http://localhost:8000/research/export > study.json
    python api/scripts/eval_study.py study.json

Protocol (fixed before looking at any data):
- Profile: the first 5 samples of a volunteer's first session (the same as guided enrolment).
- Same-sitting test: the rest of session 1. Optimistic: same hour, same mood, same warm-up.
- Later-session test: every sample from sessions 2-3 (hours or days later). This is the honest number.
- Impostor test: every sample of every OTHER volunteer, scored against this volunteer's profile. These are
  zero-effort impostors (someone typing normally), not someone deliberately imitating the victim.
- Rates are averaged per person, so a volunteer who typed more does not count more. 95% intervals come
  from resampling volunteers, because one person's attempts are not independent of each other.
- The thresholds are the ones in production (0.3 and 0.5). They were chosen on synthetic data and are
  NOT re-tuned here: tuning on the same data you report on flatters the result.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.risk.behaviour import ENROL_MIN, RobustZModel  # noqa: E402
from app.risk.metrics import bootstrap_ci, eer, rate_at  # noqa: E402

THRESHOLDS = (0.3, 0.5)
MIN_PARTICIPANTS = 5     # below this nothing is reported
QUOTABLE = 20            # below this the numbers are a pilot
STRUGGLING = 0.25        # a person with more than 25% of their own later attempts flagged at 0.5


class InsufficientData(Exception):
    pass


def score_people(data: dict) -> list[dict]:
    people = []
    for p in data["participants"]:
        sessions = sorted(p["sessions"], key=lambda s: s["session"])
        if not sessions or len(sessions[0]["samples"]) < ENROL_MIN:
            continue  # not enough to build a profile
        people.append({"pid": p["pid"], "keyboard": p.get("keyboard", "?"), "sessions": sessions})
    for person in people:
        s1 = person["sessions"][0]["samples"]
        model = RobustZModel(s1[:ENROL_MIN])
        person["same"] = [model.score(x)[0] for x in s1[ENROL_MIN:]]
        person["later"] = [model.score(x)[0] for s in person["sessions"][1:] for x in s["samples"]]
        person["impostor"] = [
            model.score(x)[0]
            for other in people if other is not person
            for s in other["sessions"] for x in s["samples"]
        ]
    return people


def _mean(values: list[float]) -> float:
    values = [v for v in values if not math.isnan(v)]
    return statistics.fmean(values) if values else float("nan")


def false_flag(kind: str, t: float):
    return lambda ps: _mean([rate_at(t, p[kind]) for p in ps if p[kind]])


def miss(t: float):
    return lambda ps: _mean([1 - rate_at(t, p["impostor"]) for p in ps if p["impostor"]])


def pooled_eer(kind: str):
    def stat(ps):
        g = [x for p in ps for x in p[kind]]
        i = [x for p in ps for x in p["impostor"]]
        return eer(g, i)[0] if g and i else float("nan")
    return stat


def _ci(people, stat, seed):
    est, lo, hi = bootstrap_ci(people, stat, seed=seed)
    if math.isnan(est):
        return None
    if math.isnan(lo) or math.isnan(hi):  # a resample happened to have no data of this kind
        lo, hi = float("nan"), float("nan")
    return {"value": est, "low": lo, "high": hi}


def evaluate(data: dict, seed: int = 0) -> dict:
    people = score_people(data)
    if len(people) < MIN_PARTICIPANTS:
        raise InsufficientData(
            f"{len(people)} volunteers with at least {ENROL_MIN} samples in their first session; "
            f"need {MIN_PARTICIPANTS} for a pilot and {QUOTABLE}+ for numbers worth quoting."
        )
    with_later = [p for p in people if p["later"]]
    result = {
        "participants": len(people),
        "withLaterSession": len(with_later),
        "keyboards": {k: sum(p["keyboard"] == k for p in people) for k in sorted({p["keyboard"] for p in people})},
        "attempts": {
            "same": sum(len(p["same"]) for p in people),
            "later": sum(len(p["later"]) for p in people),
            "impostor": sum(len(p["impostor"]) for p in people),
        },
        "pilot": len(people) < QUOTABLE,
        "rates": {},
    }
    for t in THRESHOLDS:
        result["rates"][str(t)] = {
            "falseFlagSame": _ci(people, false_flag("same", t), seed),
            "falseFlagLater": _ci(with_later, false_flag("later", t), seed) if with_later else None,
            "miss": _ci(people, miss(t), seed),
        }
    kind = "later" if with_later else "same"
    result["eer"] = {"genuine": kind, **(_ci(with_later or people, pooled_eer(kind), seed) or {})}
    result["struggling"] = sorted(
        p["pid"] for p in with_later if rate_at(0.5, p["later"]) > STRUGGLING
    )
    return result


def _fmt(ci) -> str:
    if ci is None:
        return "no data"
    if math.isnan(ci["low"]):
        return f"{ci['value']:6.1%}  (interval unavailable)"
    return f"{ci['value']:6.1%}  (95% CI {ci['low']:.1%} to {ci['high']:.1%})"


def report(r: dict) -> str:
    lines = [
        f"Volunteer study: {r['participants']} people ({r['withLaterSession']} came back for a later session); "
        f"keyboards {r['keyboards']}",
        f"Attempts scored: {r['attempts']['same']} same-sitting, {r['attempts']['later']} later-session, "
        f"{r['attempts']['impostor']} other-person",
        "",
    ]
    for t, rates in r["rates"].items():
        lines += [
            f"Threshold {t} (production rule: {'+10' if t == '0.3' else '+20'} points)",
            f"  own attempts flagged, same sitting    {_fmt(rates['falseFlagSame'])}",
            f"  own attempts flagged, later session   {_fmt(rates['falseFlagLater'])}",
            f"  other people NOT flagged (missed)     {_fmt(rates['miss'])}",
            "",
        ]
    e = r["eer"]
    if "value" in e:
        lines.append(f"Equal error rate ({e['genuine']} attempts vs other people): {_fmt(e)}")
    if r["struggling"]:
        lines.append(f"Model fails these people (> {STRUGGLING:.0%} of their later attempts flagged at 0.5): "
                     f"{', '.join(r['struggling'])}")
    lines.append("")
    if r["pilot"]:
        lines.append(f"PILOT: fewer than {QUOTABLE} volunteers. Report the intervals, not just the percentages.")
    lines.append("Impostors here type normally; nobody tried to imitate the victim. A deliberate mimic would be missed more often.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    data = json.loads(Path(argv[1]).read_text())
    if data.get("format") != "sentinel-typing-study/1":
        print("Not a Sentinel study export (format field missing or different).")
        return 2
    try:
        print(report(evaluate(data)))
    except InsufficientData as exc:
        print(f"Not enough data: {exc}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
