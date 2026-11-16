"""Slice 2c: payments scored with the consortium ledger. The sidecar is faked at the HTTP layer; its
answers are the chaincode's real output (contracts/queryPayee.json, kept honest by the chaincode's own
contract test). Needs DATABASE_URL for the payment tests; the validator tests run without it."""
import copy
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from .test_payments import OPERATOR, client, codes, pay, set_signals  # noqa: F401  (client is a fixture)

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "queryPayee.json").read_text())
needs_db = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")
SHOP = {"upi": "shop@okicici"}


class FakeGateway:
    """Answers /query-payee with a chosen contract case, re-addressed to the hashes actually asked for."""

    def __init__(self):
        self.case = "clear"
        self.calls = []
        self.fail = None          # None | "timeout" | "500" | "garbage"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "msp": "BankAMSP", "latencyMs": 4})
        body = json.loads(request.content)
        self.calls.append({"hashes": body["idHashes"], "token": request.headers.get("x-ledger-token"),
                           "path": request.url.path})
        if self.fail == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.fail == "500":
            return httpx.Response(502, json={"error": "peer unreachable", "kind": "unavailable"})
        answer = copy.deepcopy(CONTRACT[self.case]["answer"])
        original = CONTRACT[self.case]["idHashes"]
        mapping = dict(zip(original, body["idHashes"]))  # the payee's real hash takes the fixture's place
        answer["idHashes"] = body["idHashes"]
        for r in answer["reports"]:
            r["idHash"] = mapping.get(r["idHash"], r["idHash"])
        if self.fail == "garbage":
            answer["maxWeight"] = 7
        return httpx.Response(200, json={"result": answer, "latencyMs": 3})


@pytest.fixture()
def gw(monkeypatch):
    from app import ledger, settings

    fake = FakeGateway()
    monkeypatch.setattr(settings, "LEDGER_URL", "http://ledger-gateway:8100")
    monkeypatch.setattr(settings, "LEDGER_TOKEN", "test-ledger-token-123456")
    monkeypatch.setattr(ledger, "_transport", lambda: httpx.MockTransport(fake.handler))
    monkeypatch.setattr(ledger, "breaker", ledger._Breaker())
    return fake


def analyst(r):
    return r.json()["analyst"]


def consortium(r):
    return analyst(r)["signals"]["consortium"]


# ---------------------------------------------------------------- scoring with the ledger
@needs_db
def test_ledger_not_configured_means_disabled_and_no_points(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "LEDGER_URL", "")
    r = pay(client, payee=SHOP)
    assert consortium(r) == {"status": "disabled"}
    assert not any(c.startswith("CONSORTIUM") for c in codes(r))


@needs_db
def test_clear_payee_and_the_request_sent_to_the_sidecar(client, gw):
    from app import settings
    from app.risk.normalize import normalise

    r = pay(client, payee=SHOP)
    assert consortium(r)["status"] == "clear"
    expected = normalise("UPI", SHOP).id_hash(settings.PAYEE_HMAC_KEY)
    assert gw.calls[-1] == {"hashes": [expected], "token": "test-ledger-token-123456", "path": "/query-payee"}


@needs_db
@pytest.mark.parametrize("case, code, points", [
    ("pendingOneBank", "CONSORTIUM_REPORTED", 10),
    ("disputedAfterConfirmed", "CONSORTIUM_REPORTED", 20),
    ("confirmed", "CONSORTIUM_CONFIRMED", 50),
])
def test_report_weights_become_points(client, gw, case, code, points):
    gw.case = case
    r = pay(client, payee=SHOP)
    got = {x["code"]: x["points"] for x in analyst(r)["reasons"]}
    assert got.get(code) == points
    assert consortium(r)["status"] == "hit"


@needs_db
@pytest.mark.parametrize("case", ["sandboxOnly", "retracted"])
def test_reports_that_weigh_nothing_add_nothing(client, gw, case):
    """A bank's own honeypot capture alone cannot get an innocent payee flagged across the consortium."""
    gw.case = case
    r = pay(client, payee=SHOP)
    assert consortium(r)["status"] == "hit"
    assert not any(c.startswith("CONSORTIUM") for c in codes(r))


@needs_db
def test_confirmed_mule_is_held_even_from_a_trusted_payee(client, gw, monkeypatch):
    """Score alone could drop below the step-up line one day; a confirmed mule is still never auto-paid."""
    from app.risk import rules

    monkeypatch.setattr(rules, "CONSORTIUM_CONFIRMED_POINTS", 5)  # pretend the points were tuned down
    gw.case = "confirmed"
    r = pay(client, amount="100", payee=SHOP)
    assert r.json()["status"] == "held" and r.json()["band"] == "step_up"
    assert analyst(r)["score"] < rules.STEP_UP_AT


@needs_db
def test_customer_sees_one_wording_for_every_kind_of_report(client, gw):
    seen = set()
    for case in ("pendingOneBank", "confirmed", "disputedAfterConfirmed"):
        gw.case = case
        reasons = pay(client, payee=SHOP).json()["reasons"]
        mine = [x for x in reasons if x["group"] == "CONSORTIUM"]
        assert len(mine) == 1
        seen.add((mine[0]["code"], mine[0]["text"]))
    assert seen == {("PAYEE_REPORTED", "Banks have received fraud reports about this payee")}


@needs_db
def test_analyst_sees_the_reports(client, gw):
    gw.case = "confirmed"
    c = consortium(pay(client, payee=SHOP))
    assert c["hardRule"] is True and c["maxWeight"] == 1
    rep = c["reports"][0]
    assert rep["status"] == "CONFIRMED" and rep["reporterOrg"] == "BankAMSP" and rep["nonSandboxBanks"] == 2


@needs_db
def test_key_rotation_asks_for_both_hashes(client, gw, monkeypatch):
    from app import settings
    from app.risk.normalize import normalise

    monkeypatch.setattr(settings, "PAYEE_HMAC_KEY_PREVIOUS", "consortium-demo-key-v0")
    gw.case = "keyRotationOldHashHit"
    r = pay(client, payee=SHOP)
    p = normalise("UPI", SHOP)
    assert gw.calls[-1]["hashes"] == [p.id_hash(settings.PAYEE_HMAC_KEY), p.id_hash("consortium-demo-key-v0")]
    assert "CONSORTIUM_REPORTED" in codes(r)  # the report was filed under the old key


# ---------------------------------------------------------------- fail-open
@needs_db
@pytest.mark.parametrize("fail", ["timeout", "500", "garbage"])
def test_ledger_failure_is_fail_open_and_recorded(client, gw, fail):
    gw.case, gw.fail = "confirmed", fail
    r = pay(client, payee=SHOP)
    assert r.status_code == 200
    c = consortium(r)
    assert c["status"] == "unavailable" and c["error"]
    assert not any(x.startswith("CONSORTIUM") for x in codes(r))  # garbage never becomes points


@needs_db
def test_a_really_slow_ledger_costs_at_most_the_timeout(client, monkeypatch):
    """Real sockets, real clock: a sidecar that hangs for 3 s delays the payment by ~0.3 s, not 3 s."""
    from app import ledger, settings

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            time.sleep(3)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "LEDGER_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(settings, "LEDGER_TOKEN", "test-ledger-token-123456")
    monkeypatch.setattr(settings, "LEDGER_TIMEOUT_MS", 300)
    monkeypatch.setattr(ledger, "breaker", ledger._Breaker())
    t0 = time.monotonic()
    r = pay(client, payee=SHOP)
    took = time.monotonic() - t0
    srv.shutdown()
    assert consortium(r)["status"] == "unavailable"
    assert took < 1.5, f"payment took {took:.2f}s"


@needs_db
def test_breaker_stops_waiting_on_a_dead_ledger_and_audits_once(client, gw, monkeypatch):
    from app import audit, ledger

    gw.fail = "timeout"
    before = len(audit.pending_events("LEDGER_DEGRADED"))
    for _ in range(ledger.FAILURES_TO_OPEN):
        pay(client, payee=SHOP)
    calls = len(gw.calls)
    r = pay(client, payee=SHOP)  # breaker open: the sidecar is not called at all
    assert len(gw.calls) == calls
    assert "skipped" in consortium(r)["error"]
    assert len(audit.pending_events("LEDGER_DEGRADED")) == before + 1

    # After the cool-down the ledger is tried again, and one success closes the breaker.
    gw.fail = None
    monkeypatch.setattr(ledger.breaker, "open_until", 0.0)
    assert consortium(pay(client, payee=SHOP))["status"] == "clear"
    assert ledger.breaker.state() == {"open": False, "secondsUntilRetry": 0, "consecutiveFailures": 0}


@needs_db
def test_ledger_status_is_operator_only(client, gw):
    assert client.get("/ledger/status").status_code == 401
    s = client.get("/ledger/status", headers=OPERATOR).json()
    assert s["configured"] is True and s["breaker"]["open"] is False
    assert s["reachable"] is True and s["gateway"]["msp"] == "BankAMSP"


@needs_db
def test_decision_audit_records_the_ledger_outcome(client, gw):
    from app import audit

    gw.case = "pendingOneBank"
    pid = pay(client, payee=SHOP).json()["paymentId"]
    ev = [e for e in audit.pending_events("RISK_DECISION") if e["payload"]["paymentId"] == pid][0]
    assert ev["payload"]["consortium"] == "hit"


@needs_db
def test_operator_signals_and_ledger_combine(client, gw):
    """Coached customer paying a confirmed mule: held (step-up), never the sandbox, both reasons shown."""
    gw.case = "confirmed"
    set_signals(client, activeCall=True)
    r = pay(client, amount="15000", payee=SHOP)
    assert r.json()["band"] == "step_up"
    assert {"CONSORTIUM_CONFIRMED", "ACTIVE_CALL"} <= set(codes(r))


# ---------------------------------------------------------------- the validator, no database
@pytest.mark.parametrize("case", sorted(CONTRACT))
def test_every_real_chaincode_answer_passes_validation(case):
    from app.ledger import _validate

    sent = CONTRACT[case]["idHashes"]
    weight, hard, reports = _validate(CONTRACT[case]["answer"], sent)
    assert weight == CONTRACT[case]["answer"]["maxWeight"] and hard == CONTRACT[case]["answer"]["hardRuleEligible"]
    assert len(reports) == len(CONTRACT[case]["answer"]["reports"])


@pytest.mark.parametrize("mutate", [
    lambda a: a.update(maxWeight=1.5),
    lambda a: a.update(maxWeight=True),
    lambda a: a.update(hardRuleEligible="yes"),
    lambda a: a["reports"][0].update(status="GUILTY"),
    lambda a: a["reports"][0].update(weight=-1),
    lambda a: a["reports"][0].update(idHash="f" * 64),          # a report about someone we did not ask about
    lambda a: a.update(maxWeight=0.5),                         # summary disagrees with the reports
    lambda a: a.update(hardRuleEligible=False),                # claims no confirmed report while one exists
    lambda a: a.update(reports=[{}] * 51),
])
def test_validator_rejects_answers_that_do_not_add_up(mutate):
    from app.ledger import _validate

    answer = copy.deepcopy(CONTRACT["confirmed"]["answer"])
    mutate(answer)
    with pytest.raises(ValueError):
        _validate(answer, CONTRACT["confirmed"]["idHashes"])


def test_consortium_rules_without_a_database():
    from app.risk.rules import STEP_UP, Signals, score

    base = dict(amount_paise=100_00, hour_ist=14, payee_type="UPI", payee_trusted=True)
    assert score(Signals(**base)).score == 0
    d = score(Signals(**base, consortium_weight=0.125))  # disputed, was pending: 5 points
    assert [(r.code, r.points) for r in d.reasons] == [("CONSORTIUM_REPORTED", 5)]
    d = score(Signals(**base, consortium_weight=1.0, consortium_confirmed=True, consortium_banks=2))
    assert d.band == STEP_UP and d.reasons[0].code == "CONSORTIUM_CONFIRMED"
    assert d.customer_reasons() == [{"code": "PAYEE_REPORTED", "text": "Banks have received fraud reports about this payee",
                                     "group": "CONSORTIUM"}]
