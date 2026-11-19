import { expect } from 'chai';
import { createHash, randomBytes } from 'crypto';
import { Context } from 'fabric-contract-api';
import { FraudIntelContract, paymentCollection } from '../src/fraudIntelContract';
import { FakeStub } from './fakeStub';

const A = 'BankAMSP';
const B = 'BankBMSP';
const C = 'BankCMSP';
const REG = 'RegulatorMSP';
const PID = '3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b';

function details(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    paymentId: PID,
    fromBank: A,
    toBank: C,
    payer: 'asha@banka',
    payerName: 'Asha Rao',
    payee: 'quick.refund@bankc',
    amountPaise: 2_000_000,
    verdict: 'ALLOWED',
    createdAt: '2026-11-19T10:00:00.000Z',
    salt: randomBytes(32).toString('hex'),
    ...over,
  };
}

class Harness {
  stub = new FakeStub();
  msp = A;
  ctx = { stub: this.stub, clientIdentity: { getMSPID: () => this.msp } } as unknown as Context;
  contract = new FraudIntelContract();

  async as<T>(msp: string, fn: (c: FraudIntelContract, ctx: Context) => Promise<T>, transient?: object): Promise<T> {
    this.msp = msp;
    this.stub.nextTx();
    this.stub.transient = new Map(transient ? [['payment', Buffer.from(JSON.stringify(transient))]] : []);
    try {
      return await fn(this.contract, this.ctx);
    } catch (e) {
      this.stub.rollback();
      throw e;
    }
  }

  record(from: string, d: Record<string, unknown>): Promise<string> {
    return this.as(from, (c, ctx) => c.recordPayment(ctx, String(d.paymentId)), d);
  }

  get(viewer: string, id = PID): Promise<Record<string, any>> {
    return this.as(viewer, async (c, ctx) => JSON.parse(await c.getPayment(ctx, id)));
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

describe('payments with private data', () => {
  let t: Harness;
  beforeEach(() => {
    t = new Harness();
  });

  it('names one shared collection per pair of banks, whichever way the money goes', () => {
    expect(paymentCollection(A, C)).to.equal('pay_BankA_BankC');
    expect(paymentCollection(C, A)).to.equal('pay_BankA_BankC');
    expect(paymentCollection(B, A)).to.equal('pay_BankA_BankB');
    expect(paymentCollection(B, B)).to.equal('_implicit_org_BankBMSP');
  });

  it('puts the details in the private collection and only their hash on the channel', async () => {
    const d = details();
    const pub = JSON.parse(await t.record(A, d));
    t.stub.commit();
    expect(pub.collection).to.equal('pay_BankA_BankC');
    expect(pub).to.not.have.property('amountPaise');
    expect(pub).to.not.have.property('payee');

    const publicBytes = t.stub.rawState(`payment:${PID}`)!.toString('utf8');
    expect(publicBytes).to.not.include('quick.refund');
    expect(publicBytes).to.not.include('Asha');

    const privateBytes = await t.stub.getPrivateData('pay_BankA_BankC', PID);
    expect(createHash('sha256').update(privateBytes).digest('hex')).to.equal(pub.dataHash);
  });

  it('shows the details to the two banks in the payment, and only the hash to everyone else', async () => {
    await t.record(A, details());
    for (const bank of [A, C]) {
      const v = await t.get(bank);
      expect(v.access).to.equal('member');
      expect(v.details.payee).to.equal('quick.refund@bankc');
      expect(v.details.amountPaise).to.equal(2_000_000);
      expect(v.fabricHashMatches).to.equal(true);
      expect(v.note).to.equal('Details match the public hash');
    }
    for (const outsider of [B, REG]) {
      const v = await t.get(outsider);
      expect(v.access).to.equal('hash-only');
      expect(v.details).to.equal(null);
      expect(v.public.dataHash).to.match(/^[0-9a-f]{64}$/);
      expect(v.fabricHashMatches).to.equal(true); // every peer holds the hash, member or not
      expect(v.note).to.match(/Private to BankAMSP and BankCMSP/);
    }
  });

  it('never even asks the peer for the details when the caller is not in the payment', async () => {
    await t.record(A, details());
    t.stub.memberOf = new Set(['pay_BankB_BankC']); // BankB's peer: not in A–C
    const v = await t.get(B);
    expect(v.details).to.equal(null); // no "not a member" error: the chaincode checked first
  });

  it('notices when a stored private copy no longer matches the public hash', async () => {
    const d = details();
    await t.record(A, d);
    t.stub.commit();
    t.stub.tamperPrivate('pay_BankA_BankC', PID, Buffer.from(JSON.stringify({ ...d, amountPaise: 1 })));
    const v = await t.get(A);
    expect(v.note).to.equal('DETAILS DO NOT MATCH THE PUBLIC HASH');
    expect(v.fabricHashMatches).to.equal(false);
  });

  it('only the sending bank can record a payment, and only once', async () => {
    await expectReject(t.record(C, details()), /Only the sending bank records a payment/);
    await t.record(A, details());
    await expectReject(t.record(A, details()), /already recorded/);
  });

  it('the Regulator cannot record payments', async () => {
    await expectReject(t.record(REG, details({ fromBank: REG })), /Only member banks/);
  });

  it('keeps the details out of the transaction: they must come in the transient field', async () => {
    await expectReject(t.as(A, (c, ctx) => c.recordPayment(ctx, PID)), /transient field "payment"/);
  });

  it('checks every field of the details', async () => {
    const bad: Array<[Record<string, unknown>, RegExp]> = [
      [{ amountPaise: 0 }, /amountPaise/],
      [{ amountPaise: 12.5 }, /amountPaise/],
      [{ toBank: 'BankZMSP' }, /toBank must be one of/],
      [{ payee: 'not a upi' }, /payment.payee/],
      [{ salt: 'short' }, /payment.salt/],
      [{ verdict: 'BLOCKED' }, /verdict must be one of/],
      [{ paymentId: '11111111-2222-4333-8444-555555555555' }, /must equal the paymentId argument/],
      [{ extra: 'x' }, /unknown fields: extra/],
    ];
    for (const [over, pattern] of bad) {
      const d = details(over);
      await expectReject(t.as(A, (c, ctx) => c.recordPayment(ctx, PID), d), pattern);
    }
    await expectReject(t.as(A, (c, ctx) => c.recordPayment(ctx, 'not-a-uuid'), details()), /lowercase UUID/);
  });

  it('a payment between two customers of one bank uses that bank\'s implicit collection', async () => {
    const pub = JSON.parse(await t.record(B, details({ fromBank: B, toBank: B, payer: 'bilal@bankb', payee: 'bhavna@bankb' })));
    expect(pub.collection).to.equal('_implicit_org_BankBMSP');
    expect((await t.get(B)).details.payee).to.equal('bhavna@bankb');
    expect((await t.get(A)).details).to.equal(null);
  });

  it('a payment that does not exist is an error, not an empty answer', async () => {
    await expectReject(t.get(A, '00000000-0000-4000-8000-000000000000'), /does not exist/);
  });
});

describe('payment hash contract (shared with the API)', () => {
  it('canonical bytes and hash match contracts/payment-hash.json', () => {
    // The API hashes its own copy of a payment to compare with the chain: both must produce these bytes.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const saved = require('../../../contracts/payment-hash.json');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { canonicalJson } = require('../src/fraudIntelContract');
    expect(canonicalJson(saved.record)).to.equal(saved.canonical);
    expect(createHash('sha256').update(Buffer.from(saved.canonical)).digest('hex')).to.equal(saved.sha256);
  });
});
