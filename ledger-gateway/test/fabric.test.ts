/**
 * The real Fabric adapter (FabricLedger) against a fake Gateway peer speaking the actual Gateway gRPC
 * protocol over TLS. Proves: certificates and keys load from an MSP folder, TLS works with the host-name
 * override, the request names the right channel, chaincode, function and arguments, the proposal is
 * signed by the identity's key, and deadlines and outages are reported as such.
 * A live Fablo network is still the final check (scripts/ledger_smoke.sh).
 */
import * as grpc from '@grpc/grpc-js';
import { common, gateway, peer } from '@hyperledger/fabric-protos';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { after, before, test } from 'node:test';
import { Config } from '../src/config';
import { FabricLedger, LedgerError } from '../src/ledger';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fake-peer-'));
const ssl = (...args: string[]) => execFileSync('openssl', args, { cwd: dir, stdio: 'pipe' });
let port = 0;
let mode: 'ok' | 'slow' | 'error' = 'ok';
const seen: { channel: string; chaincode: string; args: string[]; signatureValid: boolean }[] = [];
const server = new grpc.Server();

function makeCerts(): void {
  ssl('ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'ca.key');
  ssl('req', '-x509', '-new', '-key', 'ca.key', '-subj', '/CN=tlsca.banka.example.com', '-days', '1', '-out', 'ca.crt');
  ssl('ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'peer.key');
  ssl('req', '-new', '-key', 'peer.key', '-subj', '/CN=peer0.banka.example.com', '-out', 'peer.csr');
  fs.writeFileSync(path.join(dir, 'san.ext'), 'subjectAltName=DNS:peer0.banka.example.com\n');
  ssl('x509', '-req', '-in', 'peer.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial', '-days', '1',
    '-extfile', 'san.ext', '-out', 'peer.crt');
  // The client identity, laid out like a Fabric MSP folder (Fablo's names differ; any file name works).
  fs.mkdirSync(path.join(dir, 'msp/signcerts'), { recursive: true });
  fs.mkdirSync(path.join(dir, 'msp/keystore'), { recursive: true });
  ssl('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256', '-out', 'msp/keystore/priv_sk');
  ssl('req', '-x509', '-new', '-key', 'msp/keystore/priv_sk', '-subj', '/CN=User1@banka.example.com', '-days', '1',
    '-out', 'msp/signcerts/User1@banka.example.com-cert.pem');
}

function evaluate(call: grpc.ServerUnaryCall<gateway.EvaluateRequest, gateway.EvaluateResponse>,
  cb: grpc.sendUnaryData<gateway.EvaluateResponse>): void {
  const signed = call.request.getProposedTransaction()!;
  const proposalBytes = signed.getProposalBytes_asU8();
  const userCert = fs.readFileSync(path.join(dir, 'msp/signcerts/User1@banka.example.com-cert.pem'));
  const signatureValid = crypto.verify('sha256', proposalBytes, { key: crypto.createPublicKey(userCert), dsaEncoding: 'der' },
    signed.getSignature_asU8());
  const proposal = peer.Proposal.deserializeBinary(proposalBytes);
  const header = common.Header.deserializeBinary(proposal.getHeader_asU8());
  const channel = common.ChannelHeader.deserializeBinary(header.getChannelHeader_asU8()).getChannelId();
  const payload = peer.ChaincodeProposalPayload.deserializeBinary(proposal.getPayload_asU8());
  const spec = peer.ChaincodeInvocationSpec.deserializeBinary(payload.getInput_asU8()).getChaincodeSpec()!;
  const args = spec.getInput()!.getArgsList_asU8().map((a) => Buffer.from(a).toString('utf8'));
  seen.push({ channel, chaincode: spec.getChaincodeId()!.getName(), args, signatureValid });

  const reply = () => {
    if (mode === 'error') {
      cb({ code: grpc.status.UNKNOWN, details: 'chaincode response 500, idHashesJson must hold 1 hash\n  at stack' });
      return;
    }
    const result = new peer.Response();
    result.setStatus(200);
    const body = args[0] === 'ping'
      ? { msp: 'BankAMSP', txId: 't1', txTime: 'now' }
      : { idHashes: JSON.parse(args[1]), maxWeight: 0.25, hardRuleEligible: false, reports: [] };
    result.setPayload(Buffer.from(JSON.stringify(body)));
    const res = new gateway.EvaluateResponse();
    res.setResult(result);
    cb(null, res);
  };
  if (mode === 'slow') setTimeout(reply, 1500);
  else reply();
}

const unimplemented = (_call: unknown, cb: grpc.sendUnaryData<never>) => cb({ code: grpc.status.UNIMPLEMENTED });

before(async () => {
  makeCerts();
  server.addService(gateway.GatewayService, {
    evaluate, endorse: unimplemented, submit: unimplemented, commitStatus: unimplemented,
    chaincodeEvents: (call: grpc.ServerWritableStream<unknown, unknown>) => call.end(),
  });
  const creds = grpc.ServerCredentials.createSsl(null, [{
    private_key: fs.readFileSync(path.join(dir, 'peer.key')), cert_chain: fs.readFileSync(path.join(dir, 'peer.crt')),
  }], false);
  port = await new Promise<number>((resolve, reject) =>
    server.bindAsync('127.0.0.1:0', creds, (err, p) => (err ? reject(err) : resolve(p))));
});
after(() => {
  server.forceShutdown();
  fs.rmSync(dir, { recursive: true, force: true });
});

const config = (over: Partial<Config> = {}): Config => ({
  port: 0, token: 'x'.repeat(16), mspId: 'BankAMSP',
  peerEndpoint: `127.0.0.1:${port}`, peerHostAlias: 'peer0.banka.example.com',
  tlsCertPath: path.join(dir, 'ca.crt'), mspDir: path.join(dir, 'msp'),
  channel: 'fraud-intel', chaincode: 'fraudintel', timeoutMs: 500, ...over,
});

test('queryPayee reaches the right chaincode function, signed by the MSP identity, over TLS', async () => {
  mode = 'ok';
  const ledger = new FabricLedger(config());
  const h = 'c'.repeat(64);
  const out = JSON.parse(await ledger.queryPayee([h]));
  ledger.close();
  assert.deepEqual(out.idHashes, [h]);
  const call = seen.at(-1)!;
  assert.equal(call.channel, 'fraud-intel');
  assert.equal(call.chaincode, 'fraudintel');
  assert.deepEqual(call.args, ['queryPayee', JSON.stringify([h])]);
  assert.equal(call.signatureValid, true, 'proposal must be signed with the keystore key');
});

test('ping works and the connection is reused', async () => {
  mode = 'ok';
  const ledger = new FabricLedger(config());
  assert.equal(JSON.parse(await ledger.ping()).msp, 'BankAMSP');
  assert.equal(JSON.parse(await ledger.ping()).msp, 'BankAMSP');
  ledger.close();
});

test('a slow peer becomes a timeout, not a hang', async () => {
  mode = 'slow';
  const ledger = new FabricLedger(config({ timeoutMs: 300 }));
  const t0 = Date.now();
  await assert.rejects(ledger.queryPayee(['d'.repeat(64)]), (e: LedgerError) => e.kind === 'timeout');
  assert.ok(Date.now() - t0 < 1400, 'gave up at the deadline');
  ledger.close();
  mode = 'ok';
});

test('a chaincode error keeps its first line only', async () => {
  mode = 'error';
  const ledger = new FabricLedger(config());
  await assert.rejects(ledger.queryPayee(['e'.repeat(64)]), (e: LedgerError) =>
    e.kind === 'chaincode' && e.message.includes('idHashesJson') && !e.message.includes('at stack'));
  ledger.close();
  mode = 'ok';
});

test('a peer that is down is reported as unavailable', async () => {
  const closed = new FabricLedger(config({ peerEndpoint: '127.0.0.1:1' }));
  await assert.rejects(closed.queryPayee(['f'.repeat(64)]), (e: LedgerError) =>
    e.kind === 'unavailable' || e.kind === 'timeout');
  closed.close();
});

test('a wrong TLS host name is refused (no silent trust)', async () => {
  const ledger = new FabricLedger(config({ peerHostAlias: 'peer0.bankb.example.com' }));
  await assert.rejects(ledger.queryPayee(['1'.repeat(64)]));
  ledger.close();
});

test('missing crypto material is a configuration error naming the path', async () => {
  const ledger = new FabricLedger(config({ mspDir: path.join(dir, 'nope') }));
  await assert.rejects(ledger.ping(), (e: LedgerError) => e.kind === 'config' && e.message.includes('nope'));
  const noTls = new FabricLedger(config({ tlsCertPath: path.join(dir, 'missing.crt') }));
  await assert.rejects(noTls.ping(), (e: LedgerError) => e.kind === 'config');
});

function mspCopy(name: string, key: { dir?: boolean; content?: string; mode?: number }): string {
  const msp = path.join(dir, name);
  fs.mkdirSync(path.join(msp, 'signcerts'), { recursive: true });
  fs.mkdirSync(path.join(msp, 'keystore'), { recursive: true });
  fs.copyFileSync(path.join(dir, 'msp/signcerts/User1@banka.example.com-cert.pem'), path.join(msp, 'signcerts/cert.pem'));
  const keyFile = path.join(msp, 'keystore/priv-key.pem');   // Fablo's file name
  if (key.dir) fs.mkdirSync(keyFile);
  else fs.writeFileSync(keyFile, key.content ?? fs.readFileSync(path.join(dir, 'msp/keystore/priv_sk')));
  if (key.mode !== undefined) fs.chmodSync(keyFile, key.mode);
  return msp;
}

test('a key that cannot be read is a named config error, never an anonymous failure', async () => {
  // Regression: an unreadable key used to surface as "ledger call failed" with no reason.
  const ledger = new FabricLedger(config({ mspDir: mspCopy('msp-dir-key', { dir: true }) }));
  await assert.rejects(ledger.ping(), (e: LedgerError) =>
    e instanceof LedgerError && e.kind === 'config' && /cannot read .*priv-key\.pem: EISDIR/.test(e.message));
});

test('permission denied names the file and says what to check', { skip: process.getuid?.() === 0 && 'root can read any file' }, async () => {
  const ledger = new FabricLedger(config({ mspDir: mspCopy('msp-no-perm', { mode: 0o000 }) }));
  await assert.rejects(ledger.ping(), (e: LedgerError) =>
    e.kind === 'config' && e.message.includes('EACCES') && e.message.includes('compose "user:"'));
});

test('a file that is not a private key is a named config error', async () => {
  const ledger = new FabricLedger(config({ mspDir: mspCopy('msp-garbage', { content: 'not a key' }) }));
  await assert.rejects(ledger.ping(), (e: LedgerError) => e.kind === 'config' && e.message.includes('not a usable private key'));
});

test("Fablo's key file name (priv-key.pem) works", async () => {
  mode = 'ok';
  const ledger = new FabricLedger(config({ mspDir: mspCopy('msp-fablo-names', {}) }));
  assert.equal(JSON.parse(await ledger.ping()).msp, 'BankAMSP');
  ledger.close();
});
