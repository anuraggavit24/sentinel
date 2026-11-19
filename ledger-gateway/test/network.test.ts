/** The demo bank network: acting as each bank, payments with private data, public identities. Fake ledgers. */
import assert from 'node:assert/strict';
import * as path from 'node:path';
import { AddressInfo } from 'node:net';
import { after, before, test } from 'node:test';
import { loadConfig } from '../src/config';
import { loadIdentities } from '../src/identities';
import { LedgerClient, LedgerError, SubmitOptions, Submitted, WriteFunction } from '../src/ledger';
import { makeServer, validatePayment } from '../src/server';
import { FIXTURES } from './chainfixture';

const READ = 'read-token-1234567890';
const WRITE = 'write-token-0987654321';
const PID = '3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b';

class OrgLedger implements LedgerClient {
  calls: { fn: string; args: string[]; opts?: SubmitOptions }[] = [];
  constructor(readonly mspId: string) {}
  async queryPayee(h: string[]): Promise<string> {
    this.calls.push({ fn: 'queryPayee', args: h });
    return JSON.stringify({ idHashes: h, maxWeight: 0, hardRuleEligible: false, reports: [], askedAs: this.mspId });
  }
  async ping(): Promise<string> {
    return JSON.stringify({ msp: this.mspId });
  }
  async submit(): Promise<string> {
    throw new Error('use submitTx');
  }
  async submitTx(fn: WriteFunction, args: string[], opts?: SubmitOptions): Promise<Submitted> {
    this.calls.push({ fn, args, opts });
    if (args[0] === 'f'.repeat(64)) throw new LedgerError('Active report already exists', 'chaincode');
    return { result: JSON.stringify({ ok: true, as: this.mspId }), txId: 'e'.repeat(64), blockNumber: 12 };
  }
  async getPayment(id: string): Promise<string> {
    this.calls.push({ fn: 'getPayment', args: [id] });
    return JSON.stringify({ viewer: this.mspId, access: this.mspId === 'BankBMSP' ? 'hash-only' : 'member' });
  }
  async chainInfo(): Promise<Uint8Array> {
    throw new LedgerError('none', 'unavailable');
  }
  async blockByNumber(): Promise<Uint8Array> {
    throw new LedgerError('none', 'unavailable');
  }
  async blockByTxId(): Promise<Uint8Array> {
    throw new LedgerError('none', 'unavailable');
  }
  close(): void {}
}

const A = new OrgLedger('BankAMSP');
const B = new OrgLedger('BankBMSP');
const C = new OrgLedger('BankCMSP');
const REG = new OrgLedger('RegulatorMSP');
const orgs = new Map<string, LedgerClient>([['BankAMSP', A], ['BankBMSP', B], ['BankCMSP', C], ['RegulatorMSP', REG]]);
const server = makeServer(A, { read: READ, write: WRITE }, { orgs, identities: async () => [{ msp: 'BankAMSP', ca: null, client: null, peer: null, privateKey: 'keystore' }] });
let base = '';
before(async () => {
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());

const post = (p: string, body: unknown, token = WRITE) =>
  fetch(base + p, { method: 'POST', headers: { 'content-type': 'application/json', 'x-ledger-token': token }, body: JSON.stringify(body) });
const get = (p: string, token = READ) => fetch(base + p, { headers: { 'x-ledger-token': token } });

function payment(over: Record<string, unknown> = {}): Record<string, unknown> {
  return { paymentId: PID, fromBank: 'BankAMSP', toBank: 'BankCMSP', payer: 'asha@banka', payerName: 'Asha Rao',
    payee: 'quick.refund@bankc', amountPaise: 2_000_000, verdict: 'ALLOWED', createdAt: '2026-11-19T10:00:00.000Z',
    salt: 'a'.repeat(64), ...over };
}

test('each write is signed by the bank named in "org", with that bank\'s own client', async () => {
  const r = await post('/report-mule', { org: 'BankBMSP', idHash: 'a'.repeat(64), idType: 'UPI', evidenceHash: 'b'.repeat(64),
    source: 'CUSTOMER_COMPLAINT', severity: 'HIGH' });
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.equal(body.result.as, 'BankBMSP');
  assert.equal(body.txId, 'e'.repeat(64));
  assert.equal(body.blockNumber, 12);
  assert.equal(B.calls.at(-1)!.fn, 'reportMule');
  assert.equal(A.calls.filter((c) => c.fn === 'reportMule').length, 0);
});

test('without "org" the default identity is used, as before', async () => {
  const r = await post('/confirm-report', { reportId: 'c'.repeat(64), evidenceHash: 'd'.repeat(64), source: 'ANALYST' });
  assert.equal((await r.json()).result.as, 'BankAMSP');
});

test('an unknown org is refused before Fabric is called', async () => {
  const r = await post('/query-payee', { org: 'BankZMSP', idHashes: ['a'.repeat(64)] }, READ);
  assert.equal(r.status, 400);
  assert.match((await r.json()).error, /org must be one of BankAMSP, BankBMSP, BankCMSP, RegulatorMSP/);
});

test('lookups can be made as any bank (read token is enough)', async () => {
  const r = await post('/query-payee', { org: 'BankCMSP', idHashes: ['a'.repeat(64)] }, READ);
  assert.equal((await r.json()).result.askedAs, 'BankCMSP');
});

test('recordPayment: details go as transient data, endorsed by the two banks in the payment', async () => {
  const r = await post('/record-payment', { org: 'BankAMSP', payment: payment() });
  assert.equal(r.status, 200);
  const call = A.calls.at(-1)!;
  assert.equal(call.fn, 'recordPayment');
  assert.deepEqual(call.args, [PID], 'only the id is a transaction argument (and so in the block)');
  const sent = JSON.parse(Buffer.from(call.opts!.transient!.payment).toString());
  assert.equal(sent.payee, 'quick.refund@bankc');
  assert.deepEqual(call.opts!.endorsingOrganizations, ['BankAMSP', 'BankCMSP']);
});

test('recordPayment: a bank cannot record a payment sent by another bank', async () => {
  const r = await post('/record-payment', { org: 'BankBMSP', payment: payment() });
  assert.equal(r.status, 400);
  assert.match((await r.json()).error, /fromBank must be the sending bank \(BankBMSP\)/);
});

test('recordPayment needs the write token', async () => {
  assert.equal((await post('/record-payment', { org: 'BankAMSP', payment: payment() }, READ)).status, 403);
});

test('one-bank payments are endorsed by that bank plus another (2-of-3 policy)', () => {
  const p = validatePayment({ payment: payment({ fromBank: 'BankBMSP', toBank: 'BankBMSP' }) }, 'BankBMSP');
  assert.deepEqual(p.endorsers, ['BankBMSP', 'BankAMSP']);
});

test('a payment is read as the bank asking: the answer differs per bank', async () => {
  const asC = await (await get(`/payment/${PID}?org=BankCMSP`)).json();
  const asB = await (await get(`/payment/${PID}?org=BankBMSP`)).json();
  assert.equal(asC.result.access, 'member');
  assert.equal(asB.result.access, 'hash-only');
  assert.equal(C.calls.at(-1)!.fn, 'getPayment');
  assert.equal((await get('/payment/not-a-uuid?org=BankCMSP')).status, 400);
});

test('/identities lists public material', async () => {
  const r = await (await get('/identities')).json();
  assert.equal(r.orgs[0].msp, 'BankAMSP');
});

test('ORGS builds one identity per organisation from Fablo\'s layout and Fablo\'s ports', () => {
  const cfg = loadConfig({ LEDGER_TOKEN: READ, ORGS: 'BankA,BankB,BankC,Regulator' });
  assert.deepEqual(cfg.orgs!.map((o) => [o.mspId, o.peerEndpoint, o.peerHostAlias]), [
    ['BankAMSP', 'host.docker.internal:7041', 'peer0.banka.example.com'],
    ['BankBMSP', 'host.docker.internal:7061', 'peer0.bankb.example.com'],
    ['BankCMSP', 'host.docker.internal:7081', 'peer0.bankc.example.com'],
    ['RegulatorMSP', 'host.docker.internal:7101', 'peer0.regulator.example.com'],
  ]);
  assert.equal(cfg.orgs![1].mspDir, '/crypto/bankb/users/User1@bankb.example.com/msp');
  assert.equal(cfg.orgs![0].orgDir, '/crypto/banka');
  const moved = loadConfig({ LEDGER_TOKEN: READ, ORGS: 'BankA,BankB', PEER_ENDPOINT_BANKB: 'peer0.bankb.example.com:7061' });
  assert.equal(moved.orgs![1].peerEndpoint, 'peer0.bankb.example.com:7061');
  assert.throws(() => loadConfig({ LEDGER_TOKEN: READ, ORGS: 'BankA,BankA' }), /twice/);
  assert.deepEqual(loadConfig({ LEDGER_TOKEN: READ }).orgs, []);
});

test('identities: certificates and public keys from an MSP layout, issuance checked, no private key in the answer', async () => {
  // Lay the test PKI out like Fablo does for one organisation.
  const fs = await import('node:fs');
  const os = await import('node:os');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'org-'));
  const pki = path.join(FIXTURES, 'pki', 'bankb');
  const put = (rel: string, from: string) => {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    fs.copyFileSync(path.join(pki, from), path.join(dir, rel));
  };
  put('msp/cacerts/ca.bankb.example.com-cert.pem', 'ca.crt');
  put('users/User1@bankb.example.com/msp/signcerts/cert.pem', 'user.crt');
  put('users/User1@bankb.example.com/msp/keystore/priv-key.pem', 'user.key');
  put('peers/peer0.bankb.example.com/msp/signcerts/peer0.bankb.example.com-cert.pem', 'peer.crt');
  const cfg = loadConfig({ LEDGER_TOKEN: READ, ORGS: 'BankB', ORG_DIR_BANKB: dir });
  const [id] = await loadIdentities(cfg.orgs!);
  fs.rmSync(dir, { recursive: true, force: true });
  assert.equal(id.msp, 'BankBMSP');
  assert.equal(id.client?.subject, 'User1@bankb.example.com');
  assert.equal(id.peer?.subject, 'peer0.bankb.example.com');
  assert.equal(id.ca?.subject, 'ca.bankb.example.com');
  assert.equal(id.client?.issuedByOrgCA, true);
  assert.equal(id.client?.keyType, 'EC P-256');
  assert.match(id.client?.publicKeyPem ?? '', /BEGIN PUBLIC KEY/);
  assert.ok(!JSON.stringify(id).includes('PRIVATE KEY'), 'no private key material, ever');
});
