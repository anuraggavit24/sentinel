/** Block parsing and hash checks, against a chain built with real Fabric protobufs. */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { test } from 'node:test';
import { headerHash, parseBlock, parseChainInfo } from '../src/chain';
import { buildChain, chain, envelope, block, testCAs, PAYMENT_ID } from './chainfixture';
import { common } from '@hyperledger/fabric-protos';

test('header hash matches an independent DER encoding made by openssl', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'der-'));
  const prev = crypto.randomBytes(32);
  const data = crypto.randomBytes(32);
  for (const number of [0, 1, 127, 128, 255, 256, 70000]) {
    const conf = `asn1=SEQUENCE:h\n[h]\nn=INTEGER:${number}\np=FORMAT:HEX,OCTETSTRING:${prev.toString('hex')}\nd=FORMAT:HEX,OCTETSTRING:${data.toString('hex')}\n`;
    fs.writeFileSync(path.join(dir, 'h.cnf'), conf);
    execFileSync('openssl', ['asn1parse', '-genconf', path.join(dir, 'h.cnf'), '-out', path.join(dir, 'h.der')], { stdio: 'pipe' });
    const expected = crypto.createHash('sha256').update(fs.readFileSync(path.join(dir, 'h.der'))).digest('hex');
    assert.equal(headerHash(number, prev, data), expected, `block ${number}`);
  }
  fs.rmSync(dir, { recursive: true, force: true });
});

test('blocks link: each previousHash is the hash of the block before', () => {
  const c = chain();
  const blocks = c.blocks.map((b) => parseBlock(b));
  assert.equal(blocks[1].previousHash, blocks[0].hash);
  assert.equal(blocks[2].previousHash, blocks[1].hash);
  assert.equal(blocks[3].previousHash, blocks[2].hash);
  const info = parseChainInfo(c.info);
  assert.deepEqual([info.height, info.currentBlockHash, info.previousBlockHash], [4, blocks[3].hash, blocks[2].hash]);
  assert.ok(blocks.every((b) => b.dataHashVerified));
});

test('transactions show function, creator, endorsers and validation', () => {
  const c = chain();
  const b1 = parseBlock(c.blocks[1]);
  assert.deepEqual(
    { id: b1.txs[0].txId, fn: b1.txs[0].fn, creator: b1.txs[0].creator, endorsers: b1.txs[0].endorsers, valid: b1.txs[0].valid },
    { id: c.reportTx, fn: 'reportMule', creator: 'BankAMSP', endorsers: ['BankAMSP', 'BankBMSP'], valid: true },
  );
  assert.equal(b1.txs[0].args[1], 'UPI');
  const b2 = parseBlock(c.blocks[2]);
  assert.deepEqual(b2.txs.map((t) => [t.fn, t.valid, t.validation]), [['confirmReport', true, 0], ['confirmReport', false, 11]]);
  const b0 = parseBlock(c.blocks[0]);
  assert.equal(b0.txs[0].type, 'config');
});

test('a tampered transaction no longer matches the data hash', () => {
  const c = chain();
  const tampered = Buffer.from(c.blocks[1]);
  const i = tampered.indexOf(Buffer.from('HIGH'));
  tampered.write('LOW!', i); // same length, different content
  const b = parseBlock(tampered);
  assert.equal(b.txs[0].args[4], 'LOW!');
  assert.equal(b.dataHashVerified, false);
  // ...and BankA's signature no longer matches what is in the block: the content was changed after signing.
  assert.equal(b.txs[0].signer?.signatureValid, false);
});

test('every signature in the block checks out with the signer\'s public key, and each key was issued by its bank', () => {
  const c = chain();
  const tx = parseBlock(c.blocks[1], testCAs()).txs[0];
  assert.equal(tx.signer?.msp, 'BankAMSP');
  assert.equal(tx.signer?.subject, 'User1@banka.example.com');
  assert.equal(tx.signer?.signatureValid, true);
  assert.equal(tx.signer?.issuedByOrgCA, true);
  assert.match(tx.signer?.keyFingerprint ?? '', /^[0-9a-f]{64}$/);
  assert.deepEqual(tx.endorsements.map((e) => [e.msp, e.subject, e.signatureValid, e.issuedByOrgCA]), [
    ['BankAMSP', 'peer0.banka.example.com', true, true],
    ['BankBMSP', 'peer0.bankb.example.com', true, true],
  ]);
  // The client key and the peer key are different keys.
  assert.notEqual(tx.signer?.keyFingerprint, tx.endorsements[0].keyFingerprint);
});

test('without the banks\' CA certificates, issuance is "unknown", never "yes"', () => {
  const tx = parseBlock(chain().blocks[1]).txs[0];
  assert.equal(tx.signer?.signatureValid, true);
  assert.equal(tx.signer?.issuedByOrgCA, null);
});

test('a forged endorsement is caught', () => {
  const env = envelope({ txId: 'e5'.repeat(32), type: common.HeaderType.ENDORSER_TRANSACTION, creator: 'BankAMSP',
    fn: 'reportMule', args: ['1'.repeat(64)], endorsers: ['BankAMSP', 'BankBMSP'], forgeEndorser: 'BankBMSP' });
  const tx = parseBlock(block(9, Buffer.alloc(32), [env], [0]).bytes, testCAs()).txs[0];
  assert.deepEqual(tx.endorsements.map((e) => e.signatureValid), [true, false]);
});

test('a key that signs correctly but was not issued by the bank\'s CA is flagged', () => {
  // Someone made their own key and certificate with BankB's peer name. The signature is mathematically
  // valid for that key, but BankB's CA never vouched for it.
  const env = envelope({ txId: 'f6'.repeat(32), type: common.HeaderType.ENDORSER_TRANSACTION, creator: 'BankAMSP',
    fn: 'reportMule', args: ['2'.repeat(64)], endorsers: ['BankAMSP', 'BankBMSP'], rogueEndorser: 'BankBMSP' });
  const e = parseBlock(block(9, Buffer.alloc(32), [env], [0]).bytes, testCAs()).txs[0].endorsements[1];
  assert.equal(e.subject, 'peer0.bankb.example.com');
  assert.equal(e.signatureValid, true);
  assert.equal(e.issuedByOrgCA, false);
});

test('a payment shows which private collection got the details, and nothing of the details', () => {
  const c = chain();
  const b3 = parseBlock(c.blocks[3], testCAs());
  const tx = b3.txs[0];
  assert.equal(tx.txId, c.paymentTx);
  assert.equal(tx.fn, 'recordPayment');
  assert.deepEqual(tx.args, [PAYMENT_ID]); // the id only: the details were transient
  assert.deepEqual(tx.privateCollections, ['pay_BankA_BankC']);
  assert.deepEqual(tx.endorsers, ['BankAMSP', 'BankCMSP']);
  const raw = Buffer.from(c.blocks[3]).toString('latin1');
  assert.ok(!raw.includes('amountPaise') && !raw.includes('@bank'), 'no payment details anywhere in the block bytes');
});

test('the saved fixture is a well-formed chain like a freshly built one', () => {
  const fresh = buildChain().blocks.map((b) => parseBlock(b, testCAs()));
  const saved = chain().blocks.map((b) => parseBlock(b, testCAs()));
  for (const [f, s] of fresh.map((f, i) => [f, saved[i]] as const)) {
    assert.deepEqual(s.txs.map((t) => [t.txId, t.fn, t.endorsers, t.privateCollections]),
      f.txs.map((t) => [t.txId, t.fn, t.endorsers, t.privateCollections]));
    assert.ok(s.txs.every((t) => t.type !== 'endorser' || (t.signer?.signatureValid && t.endorsements.every((e) => e.signatureValid))));
  }
});
