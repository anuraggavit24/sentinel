/**
 * Read-only views of the chain itself (blocks, not just chaincode state), for the "how it works" page.
 * Data comes from the peer's system chaincode qscc. Nothing here is trusted blindly: every block's data
 * hash is recomputed from its transactions, and every block's header hash is recomputed so the page can
 * show that block N+1 really points at block N.
 */
import { common, ledger, msp, peer } from '@hyperledger/fabric-protos';
import * as crypto from 'node:crypto';
import { cn, fingerprint, issuedBy } from './identities';

/** A signature in the block, checked with the public key in the signer's certificate. */
export interface SignatureView {
  msp: string | null;
  subject: string | null;        // certificate common name, e.g. peer0.bankb.example.com
  keyFingerprint: string | null; // SHA-256 of the signer's public key
  signatureValid: boolean | null; // null: nothing was signed (e.g. some config transactions)
  issuedByOrgCA: boolean | null;  // the certificate was signed by that organisation's CA (null: CA not known here)
}

/** Each organisation's CA certificates, by MSP id. */
export type CAs = Map<string, crypto.X509Certificate[]>;

export interface ChainInfo {
  height: number;
  currentBlockHash: string;
  previousBlockHash: string;
}

export interface TxView {
  txId: string;
  type: 'endorser' | 'config' | 'other';
  timestamp: string | null;
  creator: string | null;        // MSP id of the client that submitted it
  chaincode: string | null;
  fn: string | null;             // chaincode function, e.g. reportMule
  args: string[];                // function arguments (the API decides who may see them)
  endorsers: string[];           // MSP ids of the peers that signed the result
  signer: SignatureView | null;  // the client that submitted it: its signature over the whole transaction
  endorsements: SignatureView[]; // each endorsing peer's signature over the result it computed
  privateCollections: string[];  // private data written: only its hashes are in this block
  validation: number | null;     // 0 = VALID
  valid: boolean | null;
}

export interface BlockView {
  number: number;
  hash: string;                  // SHA-256 of the ASN.1 header (number, previousHash, dataHash), as Fabric does
  previousHash: string;
  dataHash: string;
  dataHashVerified: boolean;     // recomputed from the transactions in the block
  txCount: number;
  txs: TxView[];
}

const hex = (b: Uint8Array) => Buffer.from(b).toString('hex');

/** DER length bytes. */
function derLen(n: number): Buffer {
  if (n < 0x80) return Buffer.from([n]);
  const bytes: number[] = [];
  while (n > 0) {
    bytes.unshift(n & 0xff);
    n >>= 8;
  }
  return Buffer.from([0x80 | bytes.length, ...bytes]);
}

/** DER INTEGER for a non-negative block number (minimal two's complement). */
function derUint(n: number): Buffer {
  let v = BigInt(n);
  const bytes: number[] = [];
  do {
    bytes.unshift(Number(v & 0xffn));
    v >>= 8n;
  } while (v > 0n);
  if (bytes[0] & 0x80) bytes.unshift(0);
  return Buffer.concat([Buffer.from([0x02]), derLen(bytes.length), Buffer.from(bytes)]);
}

const derOctets = (b: Uint8Array) => Buffer.concat([Buffer.from([0x04]), derLen(b.length), Buffer.from(b)]);

/** Fabric's block header hash: SHA-256 over DER SEQUENCE { INTEGER number, OCTET STRING prev, OCTET STRING data }. */
export function headerHash(number: number, previousHash: Uint8Array, dataHash: Uint8Array): string {
  const body = Buffer.concat([derUint(number), derOctets(previousHash), derOctets(dataHash)]);
  const seq = Buffer.concat([Buffer.from([0x30]), derLen(body.length), body]);
  return crypto.createHash('sha256').update(seq).digest('hex');
}

export function parseChainInfo(bytes: Uint8Array): ChainInfo {
  const info = common.BlockchainInfo.deserializeBinary(bytes);
  return {
    height: info.getHeight(),
    currentBlockHash: hex(info.getCurrentblockhash_asU8()),
    previousBlockHash: hex(info.getPreviousblockhash_asU8()),
  };
}

function mspOf(serialized: Uint8Array): string | null {
  try {
    return msp.SerializedIdentity.deserializeBinary(serialized).getMspid() || null;
  } catch {
    return null;
  }
}

/**
 * Checks `signature` over `data` with the public key in the serialized identity's certificate, and the
 * certificate against the organisation's CA. Fabric uses ECDSA with SHA-256; signatures are DER.
 */
export function checkSignature(identity: Uint8Array, data: Uint8Array, signature: Uint8Array, cas?: CAs): SignatureView {
  const view: SignatureView = { msp: mspOf(identity), subject: null, keyFingerprint: null, signatureValid: null, issuedByOrgCA: null };
  let cert: crypto.X509Certificate | null = null;
  try {
    cert = new crypto.X509Certificate(Buffer.from(msp.SerializedIdentity.deserializeBinary(identity).getIdBytes_asU8()));
  } catch {
    cert = null;
  }
  if (cert) {
    view.subject = cn(cert.subject);
    view.keyFingerprint = fingerprint(cert.publicKey);
    const known = view.msp ? cas?.get(view.msp) : undefined;
    view.issuedByOrgCA = known ? issuedBy(cert, known) : null;
  }
  if (signature.length > 0) {
    // A signature we cannot check (no usable certificate) is not a valid signature.
    try {
      view.signatureValid = cert ? crypto.verify('sha256', data, { key: cert.publicKey, dsaEncoding: 'der' }, signature) : false;
    } catch {
      view.signatureValid = false;
    }
  }
  return view;
}

function collectionsWritten(prpBytes: Uint8Array): string[] {
  try {
    const prp = peer.ProposalResponsePayload.deserializeBinary(prpBytes);
    const action = peer.ChaincodeAction.deserializeBinary(prp.getExtension_asU8());
    const rwset = ledger.rwset.TxReadWriteSet.deserializeBinary(action.getResults_asU8());
    const names = new Set<string>();
    for (const ns of rwset.getNsRwsetList()) {
      for (const c of ns.getCollectionHashedRwsetList()) names.add(c.getCollectionName());
    }
    return [...names].sort();
  } catch {
    return [];
  }
}

function parseTx(envBytes: Uint8Array, validation: number | null, cas?: CAs): TxView {
  const view: TxView = { txId: '', type: 'other', timestamp: null, creator: null, chaincode: null, fn: null, args: [],
    endorsers: [], signer: null, endorsements: [], privateCollections: [], validation,
    valid: validation === null ? null : validation === 0 };
  try {
    const env = common.Envelope.deserializeBinary(envBytes);
    const payload = common.Payload.deserializeBinary(env.getPayload_asU8());
    const header = payload.getHeader();
    if (header) {
      const ch = common.ChannelHeader.deserializeBinary(header.getChannelHeader_asU8());
      view.txId = ch.getTxId();
      const ts = ch.getTimestamp();
      if (ts) view.timestamp = new Date(ts.getSeconds() * 1000 + Math.floor(ts.getNanos() / 1e6)).toISOString();
      view.type = ch.getType() === common.HeaderType.ENDORSER_TRANSACTION ? 'endorser'
        : ch.getType() === common.HeaderType.CONFIG ? 'config' : 'other';
      const sig = common.SignatureHeader.deserializeBinary(header.getSignatureHeader_asU8());
      view.creator = mspOf(sig.getCreator_asU8());
      // The client signed the payload bytes (header + transaction) with its private key.
      if (sig.getCreator_asU8().length > 0) {
        view.signer = checkSignature(sig.getCreator_asU8(), env.getPayload_asU8(), env.getSignature_asU8(), cas);
      }
    }
    if (view.type === 'endorser') {
      const tx = peer.Transaction.deserializeBinary(payload.getData_asU8());
      const action = tx.getActionsList()[0];
      if (action) {
        const cap = peer.ChaincodeActionPayload.deserializeBinary(action.getPayload_asU8());
        const cpp = peer.ChaincodeProposalPayload.deserializeBinary(cap.getChaincodeProposalPayload_asU8());
        const spec = peer.ChaincodeInvocationSpec.deserializeBinary(cpp.getInput_asU8()).getChaincodeSpec();
        if (spec) {
          view.chaincode = spec.getChaincodeId()?.getName() ?? null;
          const all = spec.getInput()?.getArgsList_asU8().map((a) => Buffer.from(a).toString('utf8')) ?? [];
          view.fn = all[0] ?? null;
          view.args = all.slice(1).map((a) => a.slice(0, 300));
        }
        const endorsed = cap.getAction();
        const prpBytes = endorsed?.getProposalResponsePayload_asU8() ?? new Uint8Array();
        const list = endorsed?.getEndorsementsList() ?? [];
        view.endorsers = list.map((e) => mspOf(e.getEndorser_asU8())).filter((m): m is string => Boolean(m));
        // Each peer signed (the result it computed || its own identity) with its private key.
        view.endorsements = list.map((e) =>
          checkSignature(e.getEndorser_asU8(), Buffer.concat([Buffer.from(prpBytes), Buffer.from(e.getEndorser_asU8())]),
            e.getSignature_asU8(), cas));
        view.privateCollections = collectionsWritten(prpBytes);
      }
    }
  } catch {
    // a transaction we cannot parse is still shown, with what we know
  }
  return view;
}

export function parseBlock(bytes: Uint8Array, cas?: CAs): BlockView {
  const block = common.Block.deserializeBinary(bytes);
  const h = block.getHeader()!;
  const data = block.getData()?.getDataList_asU8() ?? [];
  const metadata = block.getMetadata()?.getMetadataList_asU8() ?? [];
  // BlockMetadataIndex.TRANSACTIONS_FILTER = 2: one validation code byte per transaction.
  const filter = metadata[2] ?? new Uint8Array();
  const prev = h.getPreviousHash_asU8();
  const dataHash = h.getDataHash_asU8();
  const recomputed = crypto.createHash('sha256').update(Buffer.concat(data.map((d) => Buffer.from(d)))).digest('hex');
  return {
    number: h.getNumber(),
    hash: headerHash(h.getNumber(), prev, dataHash),
    previousHash: hex(prev),
    dataHash: hex(dataHash),
    dataHashVerified: recomputed === hex(dataHash),
    txCount: data.length,
    txs: data.map((d, i) => parseTx(d, i < filter.length ? filter[i] : null, cas)),
  };
}
