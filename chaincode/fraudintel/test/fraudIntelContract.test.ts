import { expect } from 'chai';
import { createHash } from 'crypto';
import { Context } from 'fabric-contract-api';
import { FraudIntelContract } from '../src/fraudIntelContract';
import { FakeStub } from './fakeStub';

const h = (s: string): string => createHash('sha256').update(s).digest('hex');
const A = 'BankAMSP';
const B = 'BankBMSP';
const C = 'BankCMSP';
const REG = 'RegulatorMSP';
const MULE = h('HDFC0000123|50100012345678');
const INNOCENT = h('SBIN0000456|30000098765432');
const OTHER = h('ICIC0000789|000401234567');

class Harness {
  stub = new FakeStub();
  msp = A;
  ctx = { stub: this.stub, clientIdentity: { getMSPID: () => this.msp } } as unknown as Context;
  contract = new FraudIntelContract();

  async as<T>(msp: string, fn: (c: FraudIntelContract, ctx: Context) => Promise<T>, txId?: string): Promise<T> {
    this.msp = msp;
    this.stub.nextTx(txId);
    try {
      return await fn(this.contract, this.ctx);
    } catch (e) {
      this.stub.rollback(); // a failed transaction must not leave writes behind
      throw e;
    }
  }
}

async function expectReject(p: Promise<unknown>, pattern: RegExp): Promise<void> {
  try {
    await p;
  } catch (e) {
    expect((e as Error).message).to.match(pattern);
    return;
  }
  expect.fail(`expected rejection matching ${pattern}`);
}

describe('FraudIntelContract', () => {
  let t: Harness;

  beforeEach(() => {
    t = new Harness();
  });

  const report = async (msp: string, idHash: string, source: string, evidence = `ev-${msp}`, severity = 'HIGH') =>
    JSON.parse(await t.as(msp, (c, ctx) => c.reportMule(ctx, idHash, 'ACCOUNT', h(evidence), source, severity)));

  const confirm = async (msp: string, reportId: string, source: string, evidence = `conf-${msp}`) =>
    JSON.parse(await t.as(msp, (c, ctx) => c.confirmReport(ctx, reportId, h(evidence), source)));

  const dispute = async (msp: string, reportId: string) =>
    JSON.parse(await t.as(msp, (c, ctx) => c.disputeReport(ctx, reportId, h(`reason-${msp}`))));

  const resolve = async (reportId: string, outcome: string) =>
    JSON.parse(await t.as(REG, (c, ctx) => c.resolveDispute(ctx, reportId, outcome)));

  const query = async (msp: string, ...hashes: string[]) =>
    JSON.parse(await t.as(msp, (c, ctx) => c.queryPayee(ctx, JSON.stringify(hashes))));

  it('ping returns the caller MSP and transaction ID', async () => {
    const res = JSON.parse(await t.as(B, (c, ctx) => c.ping(ctx), 'txPING'));
    expect(res.msp).to.equal(B);
    expect(res.txId).to.equal('txPING');
  });

  it('creates a PENDING report with a reportId taken from the transaction ID', async () => {
    const r = await report(A, MULE, 'ANALYST');
    expect(r.status).to.equal('PENDING');
    expect(r.reportId).to.equal(t.stub.getTxID());
    const q = await query(C, MULE);
    expect(q.reports).to.have.length(1);
    expect(q.reports[0].weight).to.equal(0.25);
  });

  it('rejects a bank confirming its own report', async () => {
    const r = await report(A, MULE, 'ANALYST');
    await expectReject(t.as(A, (c, ctx) => c.confirmReport(ctx, r.reportId, h('x'), 'ANALYST')), /own report/);
  });

  it('confirms with 2 distinct banks using non-sandbox sources (scenario steps 1-4)', async () => {
    const r = await report(A, MULE, 'ANALYST');
    const after = await confirm(B, r.reportId, 'CUSTOMER_COMPLAINT');
    expect(after.status).to.equal('CONFIRMED');
    const q = await query(C, MULE);
    expect(q.maxWeight).to.equal(1);
    expect(q.hardRuleEligible).to.equal(true);
  });

  it('gives a sandbox capture zero weight and caps its severity at LOW (step 5)', async () => {
    const r = await report(A, INNOCENT, 'SANDBOX_CAPTURE', 'ev-sandbox', 'HIGH');
    expect(r.severity).to.equal('LOW');
    const q = await query(C, INNOCENT);
    expect(q.maxWeight).to.equal(0);
  });

  it('blocks the two-sandbox attack (step 6)', async () => {
    const r = await report(A, INNOCENT, 'SANDBOX_CAPTURE');
    const after = await confirm(B, r.reportId, 'SANDBOX_CAPTURE');
    expect(after.status).to.equal('PENDING');
    expect((await query(C, INNOCENT)).maxWeight).to.equal(0);
  });

  it('keeps a sandbox report plus one ANALYST confirmation PENDING, so one bank cannot blacklist (step 6b)', async () => {
    const r = await report(A, INNOCENT, 'SANDBOX_CAPTURE');
    const after = await confirm(B, r.reportId, 'ANALYST');
    expect(after.status).to.equal('PENDING');
    const q = await query(C, INNOCENT);
    expect(q.maxWeight).to.equal(0.25);
    expect(q.hardRuleEligible).to.equal(false);
  });

  it('confirms a sandbox capture once 2 other banks bring non-sandbox evidence', async () => {
    const r = await report(A, MULE, 'SANDBOX_CAPTURE');
    await confirm(B, r.reportId, 'ANALYST');
    const after = await confirm(C, r.reportId, 'CUSTOMER_COMPLAINT');
    expect(after.status).to.equal('CONFIRMED');
  });

  it('lets each bank confirm a report only once', async () => {
    const r = await report(A, MULE, 'SANDBOX_CAPTURE');
    await confirm(B, r.reportId, 'SANDBOX_CAPTURE', 'first');
    await expectReject(
      t.as(B, (c, ctx) => c.confirmReport(ctx, r.reportId, h('second'), 'ANALYST')),
      /already confirmed/,
    );
  });

  it('rejects evidence already attached to the report', async () => {
    const r = await report(A, MULE, 'ANALYST', 'shared-evidence');
    await expectReject(
      t.as(B, (c, ctx) => c.confirmReport(ctx, r.reportId, h('shared-evidence'), 'ANALYST')),
      /must differ/,
    );
  });

  it('rejects a second active report for the same identifier', async () => {
    await report(A, MULE, 'ANALYST');
    await expectReject(
      t.as(B, (c, ctx) => c.reportMule(ctx, MULE, 'ACCOUNT', h('b'), 'ANALYST', 'HIGH')),
      /already exists/,
    );
  });

  it('limits the Regulator to ruling on disputes (step 10)', async () => {
    await expectReject(
      t.as(REG, (c, ctx) => c.reportMule(ctx, MULE, 'ACCOUNT', h('r'), 'ANALYST', 'HIGH')),
      /Only member banks/,
    );
    const r = await report(A, MULE, 'ANALYST');
    await expectReject(t.as(REG, (c, ctx) => c.confirmReport(ctx, r.reportId, h('r'), 'ANALYST')), /Only member banks/);
    await dispute(C, r.reportId);
    await expectReject(t.as(B, (c, ctx) => c.resolveDispute(ctx, r.reportId, 'UPHELD')), /Only the Regulator/);
  });

  it('halves weight during a dispute, stops the hard rule, and drops to zero when overturned (step 9)', async () => {
    const r = await report(A, MULE, 'ANALYST');
    await confirm(B, r.reportId, 'CUSTOMER_COMPLAINT');
    await dispute(C, r.reportId);

    let q = await query(C, MULE);
    expect(q.reports[0].status).to.equal('DISPUTED');
    expect(q.maxWeight).to.equal(0.5);
    expect(q.hardRuleEligible).to.equal(false);

    await resolve(r.reportId, 'OVERTURNED');
    q = await query(C, MULE);
    expect(q.maxWeight).to.equal(0);

    const hist = JSON.parse(await t.as(REG, (c, ctx) => c.getHistory(ctx, r.reportId)));
    const statuses = hist.entries.map((e: { value: { status: string } }) => e.value.status);
    expect(statuses).to.deep.equal(['PENDING', 'CONFIRMED', 'DISPUTED', 'OVERTURNED']);
  });

  it('does not raise the weight of a zero-weight report when it is disputed', async () => {
    const r = await report(A, INNOCENT, 'SANDBOX_CAPTURE');
    await dispute(B, r.reportId);
    expect((await query(C, INNOCENT)).maxWeight).to.equal(0);
  });

  it('never promotes a report through a dispute: sandbox-only report disputed then UPHELD stays at zero', async () => {
    const r = await report(A, INNOCENT, 'SANDBOX_CAPTURE');
    await dispute(B, r.reportId);
    const after = await resolve(r.reportId, 'UPHELD');
    expect(after.status).to.equal('PENDING');
    expect(after.resolution.outcome).to.equal('UPHELD');
    const q = await query(C, INNOCENT);
    expect(q.maxWeight).to.equal(0);
    expect(q.hardRuleEligible).to.equal(false);
  });

  it('keeps a single-bank ANALYST report at 25% after an upheld dispute', async () => {
    const r = await report(A, MULE, 'ANALYST');
    await dispute(B, r.reportId);
    await resolve(r.reportId, 'UPHELD');
    const q = await query(C, MULE);
    expect(q.maxWeight).to.equal(0.25);
    expect(q.hardRuleEligible).to.equal(false);
  });

  it('rejects duplicate reportIds in one rehash transaction', async () => {
    const r = await report(A, MULE, 'ANALYST');
    const items = JSON.stringify([
      { reportId: r.reportId, newIdHash: h('n1') },
      { reportId: r.reportId, newIdHash: h('n2') },
    ]);
    await expectReject(t.as(A, (c, ctx) => c.rehashOwnRecords(ctx, '2', items)), /Duplicate reportId/);
    // The failed transaction must leave nothing behind: old hash still matches, new hash does not.
    expect((await query(C, MULE)).reports).to.have.length(1);
    expect((await query(C, h('n1'))).reports).to.have.length(0);
  });

  it('leaves state untouched when a transaction fails partway (no partial writes)', async () => {
    const r1 = await report(A, MULE, 'ANALYST');
    const r2 = await report(B, OTHER, 'ANALYST', 'ev-other');
    // Item 1 is BankA's own report and gets written; item 2 is BankB's, so the transaction fails.
    const items = JSON.stringify([
      { reportId: r1.reportId, newIdHash: h('n1') },
      { reportId: r2.reportId, newIdHash: h('n2') },
    ]);
    await expectReject(t.as(A, (c, ctx) => c.rehashOwnRecords(ctx, '2', items)), /only rehash reports it filed/);
    const after = JSON.parse(await t.as(C, (c, ctx) => c.getReport(ctx, r1.reportId)));
    expect(after.keyVersion).to.equal(1);
    expect(after.idHash).to.equal(MULE);
    expect((await query(C, MULE)).reports).to.have.length(1);
    expect((await query(C, h('n1'))).reports).to.have.length(0);
  });

  it('restores full weight when a dispute is upheld', async () => {
    const r = await report(A, MULE, 'ANALYST');
    await confirm(B, r.reportId, 'ANALYST');
    await dispute(C, r.reportId);
    await resolve(r.reportId, 'UPHELD');
    const q = await query(C, MULE);
    expect(q.maxWeight).to.equal(1);
    expect(q.hardRuleEligible).to.equal(true);
  });

  it('lets only the reporter rehash, moves the index, and accepts both hashes during rotation', async () => {
    const r = await report(A, MULE, 'ANALYST');
    const NEW = h('new-key|HDFC0000123|50100012345678');
    const items = JSON.stringify([{ reportId: r.reportId, newIdHash: NEW }]);

    await expectReject(t.as(B, (c, ctx) => c.rehashOwnRecords(ctx, '2', items)), /only rehash reports it filed/);
    await t.as(A, (c, ctx) => c.rehashOwnRecords(ctx, '2', items));

    expect((await query(C, MULE)).reports).to.have.length(0);
    expect((await query(C, NEW)).reports).to.have.length(1);
    expect((await query(C, MULE, NEW)).reports).to.have.length(1);
    await expectReject(t.as(A, (c, ctx) => c.rehashOwnRecords(ctx, '2', items)), /already at key version/);
  });

  it('makes audit anchors write-once', async () => {
    await t.as(A, (c, ctx) => c.anchorAuditRoot(ctx, '2026-11-15', h('root-1')));
    await expectReject(
      t.as(A, (c, ctx) => c.anchorAuditRoot(ctx, '2026-11-15', h('root-2'))),
      /cannot be overwritten/,
    );
    const anchor = JSON.parse(await t.as(REG, (c, ctx) => c.getAnchor(ctx, A, '2026-11-15')));
    expect(anchor.rootHash).to.equal(h('root-1'));
  });

  it('computes org stats from a query, so dispute abuse is visible', async () => {
    const r1 = await report(A, MULE, 'ANALYST');
    await confirm(C, r1.reportId, 'CUSTOMER_COMPLAINT');
    const r2 = await report(A, OTHER, 'ANALYST', 'ev-other');
    await confirm(C, r2.reportId, 'ANALYST', 'conf-other');

    await dispute(B, r1.reportId);
    await dispute(B, r2.reportId);
    await resolve(r1.reportId, 'UPHELD');
    await resolve(r2.reportId, 'OVERTURNED');

    const stats = JSON.parse(await t.as(REG, (c, ctx) => c.getOrgStats(ctx, B)));
    expect(stats.disputesFiled).to.equal(2);
    expect(stats.disputesRejected).to.equal(1);
    expect(stats.disputesAccepted).to.equal(1);
    expect(stats.disputeRejectionRate).to.equal(0.5);
  });

  it('produces identical bytes on two peers for the same transaction (determinism)', async () => {
    const peer1 = new Harness();
    const peer2 = new Harness();
    for (const p of [peer1, peer2]) {
      await p.as(A, (c, ctx) => c.reportMule(ctx, MULE, 'ACCOUNT', h('ev'), 'ANALYST', 'HIGH'), 'txSAME');
    }
    const bytes1 = peer1.stub.rawState('report:txSAME');
    const bytes2 = peer2.stub.rawState('report:txSAME');
    expect(bytes1).to.not.equal(undefined);
    expect(bytes1?.equals(bytes2 as Buffer)).to.equal(true);
  });
});
