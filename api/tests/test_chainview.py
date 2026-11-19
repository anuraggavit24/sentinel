"""The Ledger page's API: real blocks (from the gateway contract fixture), tx lookup, analyst payee check."""
import copy
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")
CHAIN = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "chain-blocks.json").read_text())
QP = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "queryPayee.json").read_text())
OP = {"x-demo-operator-token": "test-operator-token"}


class FakeGateway:
    def __init__(self):
        self.down = False
        self.case = "confirmed"

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=req)
        path = req.url.path
        if path == "/chain/blocks":
            n = int(req.url.params.get("count", 8))
            return httpx.Response(200, json={"info": CHAIN["info"], "blocks": copy.deepcopy(CHAIN["blocks"][:n]), "latencyMs": 9})
        if path.startswith("/chain/tx/"):
            for b in CHAIN["blocks"]:
                if any(t["txId"] == path.rsplit("/", 1)[1] for t in b["txs"]):
                    return httpx.Response(200, json={"block": copy.deepcopy(b), "latencyMs": 5})
            return httpx.Response(422, json={"error": "no such transaction", "kind": "chaincode"})
        if path == "/query-payee":
            asked = json.loads(req.content)["idHashes"]
            ans = copy.deepcopy(QP[self.case]["answer"])
            ans["idHashes"] = asked
            for r in ans["reports"]:
                r["idHash"] = asked[0]
            return httpx.Response(200, json={"result": ans, "latencyMs": 7})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture()
def env(monkeypatch):
    from fastapi.testclient import TestClient

    from app import ledger, main, settings

    from .test_ops import register

    gw = FakeGateway()
    monkeypatch.setattr(settings, "LEDGER_URL", "http://ledger-gateway:8100")
    monkeypatch.setattr(settings, "LEDGER_TOKEN", "read-token-1234567890")
    monkeypatch.setattr(ledger, "_transport", lambda: httpx.MockTransport(gw.handler))
    monkeypatch.setattr(ledger, "breaker", ledger._Breaker())
    with TestClient(main.app) as customer:
        register(customer, "cust")
        analyst = TestClient(main.app)
        name, _ = register(analyst, "anal")
        analyst.post("/ops/analysts", json={"username": name}, headers=OP)
        yield {"gw": gw, "customer": customer, "analyst": analyst}


def test_chain_shows_linked_blocks_and_endorsers(env):
    r = env["customer"].get("/ledger/chain?count=4").json()
    assert r["info"]["height"] == 4
    b3, b2, b1, b0 = r["blocks"]
    assert b3["linkVerified"] is True and b2["linkVerified"] is True and b1["linkVerified"] is True and b0["linkVerified"] is None
    tx = b1["txs"][0]
    assert tx["fn"] == "reportMule" and tx["endorsers"] == ["BankAMSP", "BankBMSP"] and tx["valid"] is True
    assert [t["valid"] for t in b2["txs"]] == [True, False]
    # Signatures, checked by the gateway with each signer's public key and bank CA, reach the page.
    assert tx["signer"]["signatureValid"] is True and tx["signer"]["issuedByOrgCA"] is True
    assert [e["signatureValid"] for e in tx["endorsements"]] == [True, True]
    assert b3["txs"][0]["fn"] == "recordPayment" and b3["txs"][0]["privateCollections"] == ["pay_BankA_BankC"]
    assert r["network"]["endorsement"].startswith("OutOf(2")


def test_customers_do_not_see_transaction_arguments_analysts_do(env):
    cust = env["customer"].get("/ledger/chain?count=4").json()["blocks"][2]["txs"][0]
    anal = env["analyst"].get("/ledger/chain?count=4").json()["blocks"][2]["txs"][0]
    assert cust["args"] == [] and anal["args"][1] == "UPI"


def test_a_broken_link_is_reported(env, monkeypatch):
    tampered = copy.deepcopy(CHAIN)
    tampered["blocks"][0]["previousHash"] = "0" * 64
    monkeypatch.setitem(globals(), "CHAIN", tampered)
    assert env["customer"].get("/ledger/chain?count=3").json()["blocks"][0]["linkVerified"] is False


def test_our_own_filings_are_labelled(env):
    from app import db, ledger_outbox

    with db.connect() as conn, conn.transaction():
        oid = ledger_outbox.enqueue(conn, payment_id=None, id_hash="f" * 64, id_type="UPI", severity="HIGH",
                                    source="ANALYST", evidence={"t": str(uuid.uuid4())}, requested_by=None)
        conn.execute("UPDATE ledger_outbox SET status = 'reported', report_id = %s WHERE id = %s", (CHAIN["reportTx"], oid))
    tx = env["analyst"].get("/ledger/chain?count=4").json()["blocks"][2]["txs"][0]
    assert tx["ours"] == {"filing": "reported", "source": "ANALYST"}
    with db.connect() as conn:
        conn.execute("DELETE FROM ledger_outbox WHERE id = %s", (oid,))


def test_find_the_block_holding_a_report(env):
    r = env["customer"].get(f"/ledger/tx/{CHAIN['reportTx']}").json()
    assert r["block"]["number"] == 1 and r["tx"]["fn"] == "reportMule"
    assert env["customer"].get(f"/ledger/tx/{'0' * 64}").status_code == 404
    assert env["customer"].get("/ledger/tx/nothex").status_code == 400


def test_ledger_down_is_a_clear_503(env):
    env["gw"].down = True
    r = env["customer"].get("/ledger/chain")
    assert r.status_code == 503 and "unreachable" in r.json()["detail"]


def test_payee_check_walks_the_payment_path_for_analysts_only(env):
    from app import settings
    from app.risk.normalize import normalise

    body = {"payeeType": "UPI", "payee": {"upi": "Mule.Demo@OKAXIS"}}
    assert env["customer"].post("/ledger/lookup", json=body).status_code == 403
    r = env["analyst"].post("/ledger/lookup", json=body).json()
    steps = {s["step"]: s for s in r["steps"]}
    assert steps["Keyed hash"]["value"] == normalise("UPI", {"upi": "mule.demo@okaxis"}).id_hash(settings.PAYEE_HMAC_KEY)
    assert "mule.demo@okaxis" not in json.dumps(r)        # only the masked form is shown
    assert r["points"] == 50 and "always held" in steps["Effect on a payment"]["detail"]
    env["gw"].case = "pendingOneBank"
    assert env["analyst"].post("/ledger/lookup", json=body).json()["points"] == 10
    assert env["analyst"].post("/ledger/lookup", json={"payeeType": "UPI", "payee": {"upi": "nope"}}).status_code == 422
