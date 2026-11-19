/** /chain routes over HTTP, with a fake ledger serving a real-protobuf chain. */
import assert from 'node:assert/strict';
import { AddressInfo } from 'node:net';
import { after, before, test } from 'node:test';
import { LedgerClient, LedgerError } from '../src/ledger';
import { makeServer } from '../src/server';
import { chain, testCAs } from './chainfixture';

const C = chain();
const TOKEN = 'read-token-1234567890';
class ChainLedger implements LedgerClient {
  async queryPayee(): Promise<string> { return '{}'; }
  async ping(): Promise<string> { return '{"msp":"BankAMSP"}'; }
  async submit(): Promise<string> { throw new Error('no'); }
  async chainInfo() { return C.info; }
  async blockByNumber(n: number) { return C.blocks[n]; }
  async blockByTxId(id: string) {
    if (id !== C.reportTx) throw new LedgerError('no such transaction', 'chaincode');
    return C.blocks[1];
  }
  close(): void {}
}
const server = makeServer(new ChainLedger(), TOKEN, { cas: async () => testCAs() });
let base = '';
before(async () => {
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => server.close());
const get = (p: string, token = TOKEN) => fetch(base + p, { headers: { 'x-ledger-token': token } });

test('latest blocks, newest first, limited', async () => {
  const r = await (await get('/chain/blocks?count=2')).json();
  assert.equal(r.info.height, 4);
  assert.deepEqual(r.blocks.map((b: { number: number }) => b.number), [3, 2]);
  assert.equal(r.blocks[0].previousHash, r.blocks[1].hash);
});

test('the block holding a transaction', async () => {
  const r = await (await get(`/chain/tx/${C.reportTx}`)).json();
  assert.equal(r.block.number, 1);
  assert.equal((await get(`/chain/tx/${'0'.repeat(64)}`)).status, 422);
  assert.equal((await get('/chain/tx/not-a-tx')).status, 404);
});

test('chain routes need the token', async () => {
  assert.equal((await get('/chain/blocks', 'wrong-wrong-wrong-wrong')).status, 401);
});

test('contracts/chain-blocks.json is what this gateway produces (the API tests use it)', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const saved = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../contracts/chain-blocks.json'), 'utf8'));
  const r = await (await get('/chain/blocks?count=4')).json();
  assert.deepEqual(r.blocks, saved.blocks);
  assert.deepEqual(r.info, saved.info);
});
