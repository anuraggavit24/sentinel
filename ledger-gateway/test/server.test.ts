/** HTTP layer with a fake ledger: auth, validation, error mapping. */
import assert from 'node:assert/strict';
import { AddressInfo } from 'node:net';
import { after, before, test } from 'node:test';
import { LedgerClient, LedgerError } from '../src/ledger';
import { makeServer, validateHashes } from '../src/server';
import { loadConfig } from '../src/config';

const TOKEN = 'test-ledger-token-123456';
const H1 = 'a'.repeat(64);
const H2 = 'b'.repeat(64);

class FakeLedger implements LedgerClient {
  calls: string[][] = [];
  mode: 'ok' | 'timeout' | 'unavailable' | 'chaincode' | 'config' | 'raw' = 'ok';
  async queryPayee(h: string[]): Promise<string> {
    this.calls.push(h);
    if (this.mode === 'raw') throw new Error('EACCES: permission denied, open /crypto/key');
    if (this.mode !== 'ok') throw new LedgerError(`fake ${this.mode}`, this.mode);
    return JSON.stringify({ idHashes: h, maxWeight: 0, hardRuleEligible: false, reports: [] });
  }
  async ping(): Promise<string> {
    if (this.mode === 'raw') throw new Error('raw failure');
    if (this.mode !== 'ok') throw new LedgerError(`fake ${this.mode}`, this.mode);
    return JSON.stringify({ msp: 'BankAMSP', txId: 'x', txTime: 'now' });
  }
  async submit(): Promise<string> {
    throw new Error('writes are disabled in this server');
  }
  close(): void {}
}

const ledger = new FakeLedger();
const server = makeServer(ledger, TOKEN);
let base = '';
before(async () => {
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());

const post = (body: unknown, token = TOKEN) =>
  fetch(`${base}/query-payee`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-ledger-token': token },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });

test('every request needs the token', async () => {
  assert.equal((await fetch(`${base}/health`)).status, 401);
  assert.equal((await post({ idHashes: [H1] }, 'wrong-token-wrong-token')).status, 401);
  assert.equal((await post({ idHashes: [H1] }, TOKEN.slice(0, -1))).status, 401);
});

test('query-payee passes valid hashes through and returns the chaincode result', async () => {
  ledger.mode = 'ok';
  const r = await post({ idHashes: [H1, H2] });
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.deepEqual(body.result.idHashes, [H1, H2]);
  assert.equal(typeof body.latencyMs, 'number');
  assert.deepEqual(ledger.calls.at(-1), [H1, H2]);
});

test('bad input is refused before Fabric sees it', async () => {
  const before = ledger.calls.length;
  for (const bad of [{}, { idHashes: [] }, { idHashes: [H1, H2, H1] }, { idHashes: ['A'.repeat(64)] },
    { idHashes: [H1, H1] }, { idHashes: [123] }, { idHashes: 'x' }]) {
    assert.equal((await post(bad)).status, 400, JSON.stringify(bad));
  }
  assert.equal((await post('not json')).status, 400);
  assert.equal(ledger.calls.length, before);
});

test('oversized bodies are cut off', async () => {
  const r = await post({ idHashes: [H1], pad: 'x'.repeat(10_000) }).catch(() => null);
  assert.ok(r === null || r.status === 413);
});

test('only the two routes exist', async () => {
  const r = await fetch(`${base}/invoke`, { method: 'POST', headers: { 'x-ledger-token': TOKEN }, body: '{}' });
  assert.equal(r.status, 404);
  assert.equal((await fetch(`${base}/query-payee`, { headers: { 'x-ledger-token': TOKEN } })).status, 404);
});

test('ledger failures map to distinct statuses so the API can tell them apart', async () => {
  const expected = { timeout: 504, unavailable: 502, chaincode: 422, config: 503 } as const;
  for (const [mode, status] of Object.entries(expected)) {
    ledger.mode = mode as FakeLedger['mode'];
    const r = await post({ idHashes: [H1] });
    assert.equal(r.status, status, mode);
    assert.equal((await r.json()).kind, mode);
  }
  ledger.mode = 'ok';
});

test('health pings the chaincode', async () => {
  ledger.mode = 'ok';
  const r = await fetch(`${base}/health`, { headers: { 'x-ledger-token': TOKEN } });
  assert.equal(r.status, 200);
  assert.equal((await r.json()).msp, 'BankAMSP');
  ledger.mode = 'config';
  assert.equal((await fetch(`${base}/health`, { headers: { 'x-ledger-token': TOKEN } })).status, 503);
  ledger.mode = 'ok';
});

test('the service refuses to start without a real token', () => {
  assert.throws(() => loadConfig({}), /LEDGER_TOKEN/);
  assert.throws(() => loadConfig({ LEDGER_TOKEN: 'short' }), /LEDGER_TOKEN/);
  assert.equal(loadConfig({ LEDGER_TOKEN: TOKEN }).peerHostAlias, 'peer0.banka.example.com');
});

test('validateHashes is exported for reuse and strict', () => {
  assert.deepEqual(validateHashes({ idHashes: [H1] }), [H1]);
  assert.throws(() => validateHashes(null));
});

test('an unexpected error keeps its message and is logged', async () => {
  const logged: string[] = [];
  const orig = console.error;
  console.error = (m: string) => logged.push(m);
  ledger.mode = 'raw';
  const r = await post({ idHashes: [H1] });
  console.error = orig;
  ledger.mode = 'ok';
  const body = await r.json();
  assert.equal(r.status, 502);
  assert.match(body.error, /EACCES: permission denied/);   // not just "ledger call failed"
  assert.ok(logged.some((l) => l.includes('queryPayee failed') && l.includes('EACCES')));
});

test('write routes do not exist when no write token is configured', async () => {
  const r = await fetch(`${base}/report-mule`, { method: 'POST', headers: { 'x-ledger-token': TOKEN }, body: '{}' });
  assert.equal(r.status, 404);
});
