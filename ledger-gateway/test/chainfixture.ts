/**
 * Builds Fabric blocks in memory (real protobufs, real ECDSA signatures from the test PKI in
 * test/fixtures/pki), for the chain tests and the dev fake peer.
 *
 * ECDSA signatures are randomised, so a freshly built chain differs byte for byte every time. chain()
 * therefore returns the chain saved in test/fixtures/chain.json (built once by writeFixture()), which keeps
 * contracts/chain-blocks.json stable for the API tests.
 */
import { common, ledger, msp, peer } from '@hyperledger/fabric-protos';
import { Timestamp } from 'google-protobuf/google/protobuf/timestamp_pb';
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { headerHash } from '../src/chain';

export const FIXTURES = path.resolve(__dirname, '../../test/fixtures');
const PKI = path.join(FIXTURES, 'pki');
const DIRS: Record<string, string> = { BankAMSP: 'banka', BankBMSP: 'bankb', BankCMSP: 'bankc', RegulatorMSP: 'regulator', OrdererMSP: 'orderer' };

export interface Signer {
  mspId: string;
  cert: Buffer;
  key: crypto.KeyObject;
}

/** A test identity: who = user (client) or peer; `rogue` = BankB's peer name on a key no BankB CA issued. */
export function signer(mspId: string, who: 'user' | 'peer', rogue = false): Signer {
  const dir = rogue ? path.join(PKI, 'rogue') : path.join(PKI, DIRS[mspId]);
  return {
    mspId,
    cert: fs.readFileSync(path.join(dir, `${who}.crt`)),
    key: crypto.createPrivateKey(fs.readFileSync(path.join(dir, `${who}.key`))),
  };
}

export function caCert(mspId: string): crypto.X509Certificate {
  return new crypto.X509Certificate(fs.readFileSync(path.join(PKI, DIRS[mspId], 'ca.crt')));
}

/** CA certificates of every test organisation, by MSP id (as the gateway loads them from msp/cacerts). */
export function testCAs(): Map<string, crypto.X509Certificate[]> {
  return new Map(Object.keys(DIRS).map((m) => [m, [caCert(m)]]));
}

export function serialized(s: Signer): Uint8Array {
  const id = new msp.SerializedIdentity();
  id.setMspid(s.mspId);
  id.setIdBytes(s.cert);
  return id.serializeBinary();
}

const sign = (s: Signer, data: Uint8Array) => crypto.sign('sha256', data, { key: s.key, dsaEncoding: 'der' });

/** The result an endorsing peer signs: chaincode response plus the read/write set (with private collections). */
export function responsePayload(result: string, collections: string[] = []): Uint8Array {
  const response = new peer.Response();
  response.setStatus(200);
  response.setPayload(Buffer.from(result));
  const ns = new ledger.rwset.NsReadWriteSet();
  ns.setNamespace('fraudintel');
  ns.setCollectionHashedRwsetList(collections.map((name) => {
    const c = new ledger.rwset.CollectionHashedReadWriteSet();
    c.setCollectionName(name);
    c.setHashedRwset(crypto.createHash('sha256').update(name).digest());
    return c;
  }));
  const rwset = new ledger.rwset.TxReadWriteSet();
  rwset.setDataModel(ledger.rwset.TxReadWriteSet.DataModel.KV);
  rwset.setNsRwsetList([ns]);
  const action = new peer.ChaincodeAction();
  action.setResponse(response);
  action.setResults(rwset.serializeBinary());
  const prp = new peer.ProposalResponsePayload();
  prp.setProposalHash(crypto.createHash('sha256').update(result).digest());
  prp.setExtension$(action.serializeBinary());
  return prp.serializeBinary();
}

/** An endorsement: the peer signs (proposal response payload || its own serialized identity). */
export function endorsement(prp: Uint8Array, s: Signer, forge = false): peer.Endorsement {
  const id = serialized(s);
  const e = new peer.Endorsement();
  e.setEndorser(id);
  const sig = sign(s, Buffer.concat([Buffer.from(prp), Buffer.from(id)]));
  if (forge) sig[sig.length - 1] ^= 0xff; // a signature that does not match what was signed
  e.setSignature(sig);
  return e;
}

export interface EnvelopeOpts {
  txId: string;
  type: number;
  creator: string;              // MSP id of the submitting client
  fn?: string;
  args?: string[];
  endorsers?: string[];         // MSP ids whose peers endorse
  collections?: string[];       // private collections written
  result?: string;
  seconds?: number;
  rogueEndorser?: string;       // this MSP's endorsement comes from the rogue (not CA-issued) key
  forgeEndorser?: string;       // this MSP's endorsement signature is corrupted
}

export function envelope(opts: EnvelopeOpts): Uint8Array {
  const client = DIRS[opts.creator] ? signer(opts.creator, 'user') : null;
  const ch = new common.ChannelHeader();
  ch.setType(opts.type);
  ch.setTxId(opts.txId);
  ch.setChannelId('fraud-intel');
  const ts = new Timestamp();
  ts.setSeconds(opts.seconds ?? 1_763_280_000);
  ch.setTimestamp(ts);
  const sh = new common.SignatureHeader();
  if (client) sh.setCreator(serialized(client));
  sh.setNonce(crypto.createHash('sha256').update(`nonce${opts.txId}`).digest().subarray(0, 24));
  const header = new common.Header();
  header.setChannelHeader(ch.serializeBinary());
  header.setSignatureHeader(sh.serializeBinary());
  const payload = new common.Payload();
  payload.setHeader(header);
  if (opts.type === common.HeaderType.ENDORSER_TRANSACTION) {
    const input = new peer.ChaincodeInput();
    input.setArgsList([opts.fn ?? 'noop', ...(opts.args ?? [])].map((a) => Buffer.from(a)));
    const spec = new peer.ChaincodeSpec();
    const id = new peer.ChaincodeID();
    id.setName('fraudintel');
    spec.setChaincodeId(id);
    spec.setInput(input);
    const cis = new peer.ChaincodeInvocationSpec();
    cis.setChaincodeSpec(spec);
    const cpp = new peer.ChaincodeProposalPayload();
    cpp.setInput(cis.serializeBinary());
    const prp = responsePayload(opts.result ?? '{}', opts.collections ?? []);
    const ea = new peer.ChaincodeEndorsedAction();
    ea.setProposalResponsePayload(prp);
    ea.setEndorsementsList((opts.endorsers ?? []).map((m) =>
      endorsement(prp, signer(m, 'peer', m === opts.rogueEndorser), m === opts.forgeEndorser)));
    const cap = new peer.ChaincodeActionPayload();
    cap.setChaincodeProposalPayload(cpp.serializeBinary());
    cap.setAction(ea);
    const ta = new peer.TransactionAction();
    ta.setPayload(cap.serializeBinary());
    const tx = new peer.Transaction();
    tx.setActionsList([ta]);
    payload.setData(tx.serializeBinary());
  }
  const env = new common.Envelope();
  const payloadBytes = payload.serializeBinary();
  env.setPayload(payloadBytes);
  // The client signs the whole payload with its private key. Config transactions here are unsigned.
  if (client && opts.type === common.HeaderType.ENDORSER_TRANSACTION) env.setSignature(sign(client, payloadBytes));
  return env.serializeBinary();
}

export function block(number: number, prevHash: Uint8Array, envelopes: Uint8Array[], codes: number[]): { bytes: Uint8Array; hash: Buffer } {
  const dataHash = crypto.createHash('sha256').update(Buffer.concat(envelopes.map((e) => Buffer.from(e)))).digest();
  const header = new common.BlockHeader();
  header.setNumber(number);
  header.setPreviousHash(prevHash);
  header.setDataHash(dataHash);
  const data = new common.BlockData();
  data.setDataList(envelopes);
  const md = new common.BlockMetadata();
  md.setMetadataList([new Uint8Array(), new Uint8Array(), Uint8Array.from(codes), new Uint8Array(), new Uint8Array()]);
  const b = new common.Block();
  b.setHeader(header);
  b.setData(data);
  b.setMetadata(md);
  return { bytes: b.serializeBinary(), hash: Buffer.from(headerHash(number, prevHash, dataHash), 'hex') };
}

export interface Chain {
  blocks: Uint8Array[];
  info: Uint8Array;
  reportTx: string;
  paymentTx: string;
}

export const PAYMENT_ID = '3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b';

/**
 * Genesis (config); a reportMule by BankA endorsed by BankA + BankB; a block with one valid confirm and
 * one invalid tx; and a recordPayment A -> C whose details went to the private collection pay_BankA_BankC.
 */
export function buildChain(): Chain {
  const reportTx = 'a1'.repeat(32);
  const paymentTx = 'd4'.repeat(32);
  const b0 = block(0, new Uint8Array(), [envelope({ txId: '', type: common.HeaderType.CONFIG, creator: 'OrdererMSP' })], [0]);
  const b1 = block(1, b0.hash, [envelope({ txId: reportTx, type: common.HeaderType.ENDORSER_TRANSACTION, creator: 'BankAMSP',
    fn: 'reportMule', args: ['f'.repeat(64), 'UPI', 'e'.repeat(64), 'ANALYST', 'HIGH'], endorsers: ['BankAMSP', 'BankBMSP'] })], [0]);
  const b2 = block(2, b1.hash, [
    envelope({ txId: 'b2'.repeat(32), type: common.HeaderType.ENDORSER_TRANSACTION, creator: 'BankCMSP', fn: 'confirmReport',
      args: [reportTx, 'd'.repeat(64), 'ANALYST'], endorsers: ['BankCMSP', 'BankAMSP'] }),
    envelope({ txId: 'c3'.repeat(32), type: common.HeaderType.ENDORSER_TRANSACTION, creator: 'BankBMSP', fn: 'confirmReport',
      args: [reportTx, 'c'.repeat(64), 'ANALYST'], endorsers: ['BankBMSP', 'BankCMSP'] }),
  ], [0, 11]);
  const b3 = block(3, b2.hash, [envelope({ txId: paymentTx, type: common.HeaderType.ENDORSER_TRANSACTION, creator: 'BankAMSP',
    fn: 'recordPayment', args: [PAYMENT_ID], endorsers: ['BankAMSP', 'BankCMSP'], collections: ['pay_BankA_BankC'] })], [0]);
  const info = new common.BlockchainInfo();
  info.setHeight(4);
  info.setCurrentblockhash(b3.hash);
  info.setPreviousblockhash(b2.hash);
  return { blocks: [b0.bytes, b1.bytes, b2.bytes, b3.bytes], info: info.serializeBinary(), reportTx, paymentTx };
}

const SAVED = path.join(FIXTURES, 'chain.json');

export function writeFixture(): void {
  const c = buildChain();
  fs.writeFileSync(SAVED, JSON.stringify({
    reportTx: c.reportTx,
    paymentTx: c.paymentTx,
    info: Buffer.from(c.info).toString('base64'),
    blocks: c.blocks.map((b) => Buffer.from(b).toString('base64')),
  }, null, 1) + '\n');
}

/** The saved chain (stable bytes). */
export function chain(): Chain {
  const saved = JSON.parse(fs.readFileSync(SAVED, 'utf8'));
  return {
    reportTx: saved.reportTx,
    paymentTx: saved.paymentTx,
    info: Buffer.from(saved.info, 'base64'),
    blocks: saved.blocks.map((b: string) => Buffer.from(b, 'base64')),
  };
}
