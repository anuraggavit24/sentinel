/**
 * Contract test between this chaincode and the risk API (slice 2c). It produces queryPayee answers for
 * the cases the API scores differently and compares them with contracts/queryPayee.json, the same file
 * the API's tests feed through its strict validator. If the chaincode's output changes shape, this test
 * fails here before the API silently starts treating every answer as "unavailable".
 *   Regenerate after a deliberate change: WRITE_CONTRACT=1 npm test
 */
import { expect } from 'chai';
import { createHash } from 'crypto';
import { Context } from 'fabric-contract-api';
import * as fs from 'fs';
import * as path from 'path';
import { FraudIntelContract } from '../src/fraudIntelContract';
import { FakeStub } from './fakeStub';

const h = (s: string): string => createHash('sha256').update(s).digest('hex');
const FILE = path.resolve(__dirname, '../../../contracts/queryPayee.json');

class Net {
  stub = new FakeStub();
  msp = 'BankAMSP';
  ctx = { stub: this.stub, clientIdentity: { getMSPID: () => this.msp } } as unknown as Context;
  cc = new FraudIntelContract();
  async as(msp: string, txId: string, fn: (c: FraudIntelContract, ctx: Context) => Promise<string>): Promise<any> {
    this.msp = msp;
    this.stub.nextTx(txId);
    return JSON.parse(await fn(this.cc, this.ctx));
  }
}

async function build(): Promise<Record<string, { idHashes: string[]; answer: unknown }>> {
  const n = new Net();
  const ids = {
    clear: h('clear'), pending: h('pending'), sandboxOnly: h('sandbox'), confirmed: h('confirmed'),
    disputed: h('disputed'), retracted: h('retracted'), rotatedOld: h('rotated-old'), rotatedNew: h('rotated-new'),
  };
  const file = (msp: string, tx: string, id: string, source: string) =>
    n.as(msp, tx, (c, ctx) => c.reportMule(ctx, id, 'ACCOUNT', h(`ev-${tx}`), source, 'HIGH'));
  await file('BankAMSP', 'tx-pending', ids.pending, 'ANALYST');
  await file('BankAMSP', 'tx-sandbox', ids.sandboxOnly, 'SANDBOX_CAPTURE');
  const conf = await file('BankAMSP', 'tx-confirmed', ids.confirmed, 'ANALYST');
  await n.as('BankBMSP', 'tx-confirm-1', (c, ctx) => c.confirmReport(ctx, conf.reportId, h('c1'), 'CUSTOMER_COMPLAINT'));
  const disp = await file('BankBMSP', 'tx-disputed', ids.disputed, 'ANALYST');
  await n.as('BankCMSP', 'tx-confirm-2', (c, ctx) => c.confirmReport(ctx, disp.reportId, h('c2'), 'ANALYST'));
  await n.as('BankAMSP', 'tx-dispute', (c, ctx) => c.disputeReport(ctx, disp.reportId, h('r1')));
  const ret = await file('BankCMSP', 'tx-retracted', ids.retracted, 'ANALYST');
  await n.as('BankCMSP', 'tx-retract', (c, ctx) => c.retractReport(ctx, ret.reportId));
  await file('BankAMSP', 'tx-rotated', ids.rotatedOld, 'ANALYST');

  const cases: Record<string, string[]> = {
    clear: [ids.clear],
    pendingOneBank: [ids.pending],
    sandboxOnly: [ids.sandboxOnly],
    confirmed: [ids.confirmed],
    disputedAfterConfirmed: [ids.disputed],
    retracted: [ids.retracted],
    keyRotationOldHashHit: [ids.rotatedNew, ids.rotatedOld],
  };
  const out: Record<string, { idHashes: string[]; answer: unknown }> = {};
  for (const [name, hashes] of Object.entries(cases)) {
    out[name] = { idHashes: hashes, answer: await n.as('BankBMSP', `q-${name}`, (c, ctx) => c.queryPayee(ctx, JSON.stringify(hashes))) };
  }
  return out;
}

describe('queryPayee contract with the risk API', () => {
  it('matches contracts/queryPayee.json', async () => {
    const now = await build();
    if (process.env.WRITE_CONTRACT === '1') fs.writeFileSync(FILE, JSON.stringify(now, null, 2) + '\n');
    const saved = JSON.parse(fs.readFileSync(FILE, 'utf8'));
    expect(now).to.deep.equal(saved);
  });

  it('covers every weight the API scores differently', async () => {
    const now = await build();
    const w = (k: string) => (now[k].answer as { maxWeight: number }).maxWeight;
    expect([w('clear'), w('sandboxOnly'), w('pendingOneBank'), w('confirmed'), w('disputedAfterConfirmed'), w('retracted')])
      .to.deep.equal([0, 0, 0.25, 1, 0.5, 0]);
    expect((now.confirmed.answer as { hardRuleEligible: boolean }).hardRuleEligible).to.equal(true);
    expect((now.disputedAfterConfirmed.answer as { hardRuleEligible: boolean }).hardRuleEligible).to.equal(false);
  });
});
