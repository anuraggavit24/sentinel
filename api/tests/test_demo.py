"""Demo bank network: one-click logins, transfers checked against the ledger as the sending bank, reports
filed as each victim's own bank, payments recorded with private details, per-bank ledger views, tamper check.

The ledger here is a stateful fake of the gateway + chaincode (same rules: 2 banks -> CONFIRMED, only the
two banks in a payment see its details). The real chaincode rules are tested in chaincode/, the gateway's
routing in ledger-gateway/, and the whole thing live with scripts/demo_smoke.sh.
"""
import hashlib
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")
PAYMENT_HASH = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "payment-hash.json").read_text())
ACTIVE = ("PENDING", "CONFIRMED", "DISPUTED")


def canonical(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class FakeNetwork:
    """Gateway + chaincode, per organisation."""

    def __init__(self):
        self.down = False
        self.reports: dict[str, dict] = {}       # idHash -> report
        self.payments: dict[str, dict] = {}      # paymentId -> {public, details}
        self.calls: list[tuple[str, str, dict]] = []
        self.block = 10

    def _tx(self) -> tuple[str, int]:
        self.block += 1
        return uuid.uuid4().hex + uuid.uuid4().hex, self.block

    def _weighted(self, r: dict) -> dict:
        banks = len({r["reporterOrg"], *r["confirmations"]})
        return {"reportId": r["reportId"], "idHash": r["idHash"], "status": r["status"], "source": "CUSTOMER_COMPLAINT",
                "severity": "HIGH", "reporterOrg": r["reporterOrg"], "nonSandboxBanks": banks,
                "weight": 1 if r["status"] == "CONFIRMED" else 0.25, "hardRuleEligible": r["status"] == "CONFIRMED"}

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=req)
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        org = body.get("org") or req.url.params.get("org")
        token = req.headers.get("x-ledger-token")
        self.calls.append((path, org, body))
        if path == "/query-payee":
            rs = [self._weighted(r) for h in body["idHashes"] if (r := self.reports.get(h))]
            return httpx.Response(200, json={"result": {"idHashes": body["idHashes"], "maxWeight": max([r["weight"] for r in rs], default=0),
                                                        "hardRuleEligible": any(r["hardRuleEligible"] for r in rs), "reports": rs}})
        if path in ("/report-mule", "/confirm-report", "/record-payment") and token != "write-token-0987654321":
            return httpx.Response(403, json={"error": "this token can only read"})
        if path == "/report-mule":
            r = self.reports.get(body["idHash"])
            if r and r["status"] in ACTIVE:
                return httpx.Response(422, json={"error": f"Active report {r['reportId']} already exists", "kind": "chaincode"})
            tx, block = self._tx()
            self.reports[body["idHash"]] = {"reportId": tx, "idHash": body["idHash"], "reporterOrg": org, "confirmations": [], "status": "PENDING"}
            return httpx.Response(200, json={"result": {"reportId": tx, "status": "PENDING"}, "txId": tx, "blockNumber": block})
        if path == "/confirm-report":
            r = next(x for x in self.reports.values() if x["reportId"] == body["reportId"])
            if r["reporterOrg"] == org:
                return httpx.Response(422, json={"error": "A bank cannot confirm its own report"})
            if org in r["confirmations"]:
                return httpx.Response(422, json={"error": f"{org} has already confirmed report {r['reportId']}"})
            r["confirmations"].append(org)
            if len({r["reporterOrg"], *r["confirmations"]}) >= 2:
                r["status"] = "CONFIRMED"
            tx, block = self._tx()
            return httpx.Response(200, json={"result": {"reportId": r["reportId"], "status": r["status"]}, "txId": tx, "blockNumber": block})
        if path == "/record-payment":
            d = body["payment"]
            if d["fromBank"] != org:
                return httpx.Response(400, json={"error": "payment.fromBank must be the sending bank"})
            if d["paymentId"] in self.payments:
                return httpx.Response(422, json={"error": "Payment is already recorded"})
            tx, block = self._tx()
            pub = {"paymentId": d["paymentId"], "fromBank": d["fromBank"], "toBank": d["toBank"],
                   "collection": "pay_" + "_".join(sorted([d["fromBank"][:-3], d["toBank"][:-3]])),
                   "dataHash": hashlib.sha256(canonical(d)).hexdigest(), "txId": tx}
            self.payments[d["paymentId"]] = {"public": pub, "details": d}
            return httpx.Response(200, json={"result": pub, "txId": tx, "blockNumber": block})
        if path.startswith("/payment/"):
            p = self.payments.get(path.rsplit("/", 1)[1])
            if not p:
                return httpx.Response(422, json={"error": "does not exist"})
            member = org in (p["public"]["fromBank"], p["public"]["toBank"])
            return httpx.Response(200, json={"result": {
                "viewer": org, "access": "member" if member else "hash-only", "public": p["public"],
                "details": p["details"] if member else None, "fabricHash": p["public"]["dataHash"], "fabricHashMatches": True,
                "note": "Details match the public hash" if member else "Private: hash only"}})
        if path == "/chain/blocks":
            chain = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "chain-blocks.json").read_text())
            return httpx.Response(200, json={"info": chain["info"], "blocks": chain["blocks"][: int(req.url.params.get("count", 6))]})
        if path == "/identities":
            return httpx.Response(200, json={"orgs": [{"msp": "BankAMSP", "client": {"subject": "User1@banka.example.com"}}]})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture()
def net(monkeypatch):
    from fastapi.testclient import TestClient

    from app import db, demo, main, settings

    fake = FakeNetwork()
    monkeypatch.setattr(settings, "DEMO_LOGIN", True)
    monkeypatch.setattr(settings, "LEDGER_URL", "http://ledger-gateway:8100")
    monkeypatch.setattr(settings, "LEDGER_TOKEN", "read-token-1234567890")
    monkeypatch.setattr(settings, "LEDGER_WRITE_TOKEN", "write-token-0987654321")
    monkeypatch.setattr(demo, "_transport", lambda: httpx.MockTransport(fake.handler))
    with TestClient(main.app) as first:
        first.get("/demo/accounts")
        with db.connect() as conn:  # every test starts a fresh story round
            conn.execute("DELETE FROM demo_transfers")
            conn.execute("UPDATE demo_meta SET round = round + 1")
            conn.execute("UPDATE demo_accounts SET balance_paise = %s WHERE kind = 'customer'", (demo.OPENING_PAISE,))
            conn.execute("UPDATE demo_accounts SET upi = %s, balance_paise = 0 WHERE kind = 'scammer'",
                         (demo.scammer_upi(demo._round(conn)),))
        accounts = first.get("/demo/accounts").json()

        def login(upi: str) -> TestClient:
            c = TestClient(main.app)
            acct = next(x for b in accounts["banks"] for x in b["customers"] if x["upi"] == upi)
            assert c.post("/demo/login", json={"accountId": acct["id"]}).status_code == 200
            return c

        yield {"fake": fake, "login": login, "scammer": accounts["scammer"]["upi"], "accounts": accounts}


def send(c, to, amount, ack=False):
    return c.post("/demo/transfers", json={"toUpi": to, "amount": amount, "acknowledgeWarning": ack})


def test_off_by_default(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, settings

    monkeypatch.setattr(settings, "DEMO_LOGIN", False)
    with TestClient(main.app) as c:
        assert c.get("/demo/accounts").status_code == 404
        assert c.post("/demo/login", json={"accountId": 1}).status_code == 404


def test_three_banks_with_customers_and_one_click_login(net):
    banks = net["accounts"]["banks"]
    assert [b["bank"] for b in banks] == ["BankAMSP", "BankBMSP", "BankCMSP"]
    assert all(len(b["customers"]) == 2 for b in banks)
    asha = net["login"]("asha@banka")
    me = asha.get("/demo/me").json()
    assert me["account"]["bank"] == "BankAMSP" and me["account"]["balance"] == 100000
    assert net["scammer"].endswith("@bankc")


def test_the_scammer_cannot_be_logged_into(net):
    from fastapi.testclient import TestClient

    from app import db, main

    with db.connect() as conn:
        sid = conn.execute("SELECT id FROM demo_accounts WHERE kind = 'scammer'").fetchone()["id"]
    with TestClient(main.app) as c:
        assert c.post("/demo/login", json={"accountId": sid}).status_code == 404


def test_the_full_story_report_confirm_block(net):
    fake, scam = net["fake"], net["scammer"]
    asha, bilal, chitra = net["login"]("asha@banka"), net["login"]("bilal@bankb"), net["login"]("chitra@bankc")

    # 1. Asha (Bank A) pays: the ledger is clean, money moves, the payment is recorded on the ledger.
    r = send(asha, scam, 20000).json()
    assert r["result"]["verdict"] == "ALLOWED" and r["account"]["balance"] == 80000
    assert r["transfers"][0]["ledgerStatus"] == "pending"   # recorded in the background, after the answer
    t = asha.get("/demo/me").json()["transfers"][0]
    assert t["ledgerStatus"] == "recorded" and len(t["ledgerTx"]) == 64
    lookup = next(c for c in fake.calls if c[0] == "/query-payee")
    assert lookup[1] == "BankAMSP", "the lookup is made as the sending bank"
    rec = next(c for c in fake.calls if c[0] == "/record-payment")
    assert rec[1] == "BankAMSP" and rec[2]["payment"]["payee"] == scam

    # 2. Asha reports it: Bank A files a new report, as Bank A.
    rep = asha.post(f"/demo/transfers/{t['id']}/report").json()["result"]
    assert rep["action"] == "reported" and rep["bank"] == "BankAMSP" and rep["statusAfter"] == "PENDING"
    assert fake.calls[-2][0] == "/report-mule" and fake.calls[-2][1] == "BankAMSP"

    # 3. Bilal (Bank B) pays the same payee: warned first (one bank reported it), then sends anyway.
    warn = send(bilal, scam, 15000)
    assert warn.status_code == 409 and warn.json()["detail"]["needsAck"] is True
    r = send(bilal, scam, 15000, ack=True).json()
    assert r["result"]["verdict"] == "WARNED"
    # He reports it too: Bank B confirms Bank A's report, as Bank B -> CONFIRMED (two banks).
    rep = bilal.post(f"/demo/transfers/{r['transfers'][0]['id']}/report").json()["result"]
    assert rep["action"] == "confirmed" and rep["bank"] == "BankBMSP" and rep["statusAfter"] == "CONFIRMED"
    confirm = next(c for c in reversed(fake.calls) if c[0] == "/confirm-report")
    assert confirm[1] == "BankBMSP" and confirm[2]["source"] == "CUSTOMER_COMPLAINT"

    # 4. Chitra (Bank C) tries: blocked. No money moves, nothing is recorded as a payment.
    before = len(fake.payments)
    r = send(chitra, scam, 5000).json()
    assert r["result"]["verdict"] == "BLOCKED"
    assert r["account"]["balance"] == 100000
    assert sorted(r["result"]["check"]["blockedBecause"]) == ["BankAMSP"]  # reporter; Bank B confirmed it
    assert len(fake.payments) == before


def test_the_same_bank_reporting_twice_does_not_confirm_its_own_report(net):
    scam = net["scammer"]
    asha, arjun = net["login"]("asha@banka"), net["login"]("arjun@banka")
    t1 = send(asha, scam, 1000).json()["transfers"][0]
    asha.post(f"/demo/transfers/{t1['id']}/report")
    t2 = send(arjun, scam, 1000, ack=True).json()["transfers"][0]
    rep = arjun.post(f"/demo/transfers/{t2['id']}/report").json()["result"]
    assert rep["action"] == "already_reported" and rep["statusAfter"] == "PENDING"


def test_report_rules(net):
    scam = net["scammer"]
    asha, bilal = net["login"]("asha@banka"), net["login"]("bilal@bankb")
    t = send(asha, scam, 1000).json()["transfers"][0]
    assert bilal.post(f"/demo/transfers/{t['id']}/report").status_code == 404   # not his transfer
    assert asha.post(f"/demo/transfers/{t['id']}/report").status_code == 200
    assert asha.post(f"/demo/transfers/{t['id']}/report").status_code == 409   # once


def test_receiver_sees_the_money_arrive(net):
    asha, bilal = net["login"]("asha@banka"), net["login"]("bilal@bankb")
    send(asha, "bilal@bankb", 2500)
    me = bilal.get("/demo/me").json()
    assert me["account"]["balance"] == 102500
    assert me["transfers"][0]["direction"] == "received" and me["transfers"][0]["counterparty"] == "asha@banka"


def test_transfer_validation(net):
    asha = net["login"]("asha@banka")
    assert send(asha, "someone@okhdfc", 10).status_code == 400          # not a demo bank
    assert send(asha, "asha@banka", 10).status_code == 400              # yourself
    assert send(asha, "bilal@bankb", 100001).status_code == 422         # over the limit
    assert send(asha, "bilal@bankb", 100000).status_code == 200
    assert send(asha, "bilal@bankb", 1).status_code == 400              # no balance left


def test_ledger_down_fails_open_and_the_record_can_be_retried(net):
    fake = net["fake"]
    asha = net["login"]("asha@banka")
    fake.down = True
    r = send(asha, "bilal@bankb", 100).json()
    assert r["result"]["verdict"] == "ALLOWED" and "unavailable" in r["result"]["reason"]
    t = asha.get("/demo/me").json()["transfers"][0]
    assert t["ledgerStatus"] == "failed" and "unreachable" in t["ledgerError"]
    fake.down = False
    asha.post(f"/demo/transfers/{t['id']}/retry-ledger")
    assert asha.get("/demo/me").json()["transfers"][0]["ledgerStatus"] == "recorded"


def test_each_bank_sees_only_what_its_peer_returns(net):
    scam = net["scammer"]
    asha = net["login"]("asha@banka")
    send(asha, scam, 20000)
    pid = asha.get("/demo/me").json()["transfers"][0]["id"]

    def row(viewer):
        v = asha.get(f"/demo/ledger?viewer={viewer}").json()
        return next(p for p in v["payments"] if p["paymentId"] == pid)

    a, c, b, reg = row("BankAMSP"), row("BankCMSP"), row("BankBMSP"), row("RegulatorMSP")
    assert a["access"] == "member" and a["details"]["amountPaise"] == 2000000
    assert c["access"] == "member" and c["details"]["payee"] == scam
    for outsider in (b, reg):
        assert outsider["access"] == "hash-only" and outsider["details"] is None
        assert "bankCopy" not in outsider                     # no database copy for a bank not in the payment
        assert "20000" not in json.dumps(outsider) and scam not in json.dumps(outsider)
    assert a["bankCopy"]["matchesChain"] is True
    # The query went to the gateway as each viewer.
    assert {c[1] for c in net["fake"].calls if c[0] == f"/payment/{pid}"} == {"BankAMSP", "BankBMSP", "BankCMSP", "RegulatorMSP"}
    assert asha.get("/demo/ledger?viewer=OrdererMSP").status_code == 400


def test_tampering_with_the_bank_database_copy_is_caught_by_the_chain(net):
    asha = net["login"]("asha@banka")
    send(asha, "bilal@bankb", 300)
    pid = asha.get("/demo/me").json()["transfers"][0]["id"]
    asha.post(f"/demo/transfers/{pid}/tamper")
    row = next(p for p in asha.get("/demo/ledger").json()["payments"] if p["paymentId"] == pid)
    assert row["bankCopy"]["tampered"] is True and row["bankCopy"]["amount"] == 3000
    assert row["bankCopy"]["matchesChain"] is False
    assert row["details"]["amountPaise"] == 30000            # the peer's private copy still matches the hash
    asha.post(f"/demo/transfers/{pid}/restore")
    row = next(p for p in asha.get("/demo/ledger").json()["payments"] if p["paymentId"] == pid)
    assert row["bankCopy"]["matchesChain"] is True


def test_api_hashes_payments_exactly_like_the_chaincode():
    from app.demo import canonical as api_canonical, sha256_hex

    assert api_canonical(PAYMENT_HASH["record"]).decode() == PAYMENT_HASH["canonical"]
    assert sha256_hex(api_canonical(PAYMENT_HASH["record"])) == PAYMENT_HASH["sha256"]


def test_reset_starts_a_new_round_with_a_new_scammer(net):
    asha = net["login"]("asha@banka")
    send(asha, net["scammer"], 100)
    out = asha.post("/demo/reset").json()
    assert out["account"]["balance"] == 100000 and out["transfers"] == []
    assert out["scammer"] != net["scammer"]


def test_demo_cookie_is_not_a_bank_session(net):
    asha = net["login"]("asha@banka")
    assert asha.get("/me").status_code == 401
    assert asha.get("/ledger/chain").status_code == 401


def test_keys_come_from_the_gateway(net):
    asha = net["login"]("asha@banka")
    assert asha.get("/demo/keys").json()["orgs"][0]["msp"] == "BankAMSP"


def test_demo_chain_strip_with_links_and_signatures(net, monkeypatch):
    from app import ledger

    monkeypatch.setattr(ledger, "_transport", lambda: httpx.MockTransport(net["fake"].handler))
    asha = net["login"]("asha@banka")
    r = asha.get("/demo/chain?count=4").json()
    assert [b["number"] for b in r["blocks"]] == [3, 2, 1, 0]
    assert r["blocks"][0]["linkVerified"] is True and r["blocks"][-1]["linkVerified"] is None
    tx = r["blocks"][0]["txs"][0]
    assert tx["fn"] == "recordPayment" and tx["privateCollections"] == ["pay_BankA_BankC"]
    assert all(e["signatureValid"] for e in tx["endorsements"])
