/** Write routes (Module 3): separate token, strict validation, error mapping. Fake ledger. */
import assert from 'node:assert/strict';
import { AddressInfo } from 'node:net';
import { after, before, test } from 'node:test';
import { loadConfig } from '../src/config';
import { LedgerClient, LedgerError, WriteFunction } from '../src/ledger';
import { makeServer } from '../src/server';

const READ = 'read-token-1234567890';
const WRITE = 'write-token-0987654321';
const H = 'a'.repeat(64);
const E = 'b'.repeat(64);
const TX = 'c'.repeat(64);

class FakeLedger implements LedgerClient {
  writes: { fn: WriteFunction; args: string[] }[] = [];
  fail: LedgerError | null = null;
  async queryPayee(h: string[]): Promise<string> {
    return JSON.stringify({ idHashes: h, maxWeight: 0, hardRuleEligible: false, reports: [] });
  }
  async ping(): Promise<string> {
    return JSON.stringify({ msp: 'BankAMSP' });
  }
  async submit(fn: WriteFunction, args: string[]): Promise<string> {
    this.writes.push({ fn, args });
    if (this.fail) throw this.fail;
    return JSON.stringify({ reportId: TX, status: 'PENDING', reporterOrg: 'BankAMSP' });
  }
  async chainInfo(): Promise<Uint8Array> {
    throw new LedgerError('no chain in this fake', 'unavailable');
  }
  async blockByNumber(): Promise<Uint8Array> {
    throw new LedgerError('no chain in this fake', 'unavailable');
  }
  async blockByTxId(): Promise<Uint8Array> {
    throw new LedgerError('no chain in this fake', 'unavailable');
  }
  close(): void {}
}

const ledger = new FakeLedger();
const server = makeServer(ledger, { read: READ, write: WRITE });
let base = '';
before(async () => {
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());

const post = (path: string, body: unknown, token = WRITE) =>
  fetch(`${base}${path}`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-ledger-token': token },
    body: JSON.stringify(body) });
const report = { idHash: H, idType: 'UPI', evidenceHash: E, source: 'ANALYST', severity: 'HIGH' };

test('the read token cannot write; no token cannot do anything', async () => {
  assert.equal((await post('/report-mule', report, READ)).status, 403);
  assert.equal((await post('/confirm-report', { reportId: TX, evidenceHash: E, source: 'ANALYST' }, READ)).status, 403);
  assert.equal((await post('/report-mule', report, 'nope-nope-nope-nope')).status, 401);
  assert.equal(ledger.writes.length, 0);
});

test('the write token can also read', async () => {
  assert.equal((await post('/query-payee', { idHashes: [H] })).status, 200);
  const h = await fetch(`${base}/health`, { headers: { 'x-ledger-token': WRITE } });
  assert.equal((await h.json()).writes, true);
});

test('reportMule passes the five arguments in chaincode order', async () => {
  ledger.fail = null;
  const r = await post('/report-mule', report);
  assert.equal(r.status, 200);
  assert.equal((await r.json()).result.reportId, TX);
  assert.deepEqual(ledger.writes.at(-1), { fn: 'reportMule', args: [H, 'UPI', E, 'ANALYST', 'HIGH'] });
});

test('confirmReport passes reportId, evidence, source', async () => {
  const r = await post('/confirm-report', { reportId: TX, evidenceHash: E, source: 'CUSTOMER_COMPLAINT' });
  assert.equal(r.status, 200);
  assert.deepEqual(ledger.writes.at(-1), { fn: 'confirmReport', args: [TX, E, 'CUSTOMER_COMPLAINT'] });
});

test('bad write requests are refused before Fabric sees them', async () => {
  const before = ledger.writes.length;
  for (const bad of [
    { ...report, idHash: 'XYZ' }, { ...report, idType: 'EMAIL' }, { ...report, source: 'RUMOUR' },
    { ...report, severity: 'EXTREME' }, { ...report, evidenceHash: undefined }, null,
  ]) {
    assert.equal((await post('/report-mule', bad)).status, 400, JSON.stringify(bad));
  }
  assert.equal((await post('/confirm-report', { reportId: 'short', evidenceHash: E, source: 'ANALYST' })).status, 400);
  assert.equal(ledger.writes.length, before);
});

test('a chaincode refusal is 422 with its message; a commit failure is 502 kind commit', async () => {
  ledger.fail = new LedgerError('Active report abc already exists for this identifier', 'chaincode');
  let r = await post('/report-mule', report);
  assert.equal(r.status, 422);
  assert.match((await r.json()).error, /already exists/);
  ledger.fail = new LedgerError('transaction x was not valid (validation code 11)', 'commit');
  r = await post('/report-mule', report);
  assert.equal(r.status, 502);
  assert.equal((await r.json()).kind, 'commit');
  ledger.fail = null;
});

test('the write token must be separate and strong', () => {
  assert.throws(() => loadConfig({ LEDGER_TOKEN: READ, LEDGER_WRITE_TOKEN: READ }), /differ/);
  assert.throws(() => loadConfig({ LEDGER_TOKEN: READ, LEDGER_WRITE_TOKEN: 'short' }), /16/);
  assert.equal(loadConfig({ LEDGER_TOKEN: READ }).writeToken, '');
  assert.equal(loadConfig({ LEDGER_TOKEN: READ, LEDGER_WRITE_TOKEN: WRITE }).writeToken, WRITE);
});
