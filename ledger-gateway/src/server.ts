/**
 * HTTP front for the ledger client. Internal only: not published on the host.
 * - Reads (/health, /query-payee) need LEDGER_TOKEN (or the write token).
 * - Writes (/report-mule, /confirm-report) need LEDGER_WRITE_TOKEN, and do not exist without it.
 * Every request is validated here before Fabric sees it.
 */
import * as crypto from 'node:crypto';
import * as http from 'node:http';
import { CAs, parseBlock, parseChainInfo } from './chain';
import { OrgIdentity } from './identities';
import { LedgerClient, LedgerError, SubmitOptions, WriteFunction } from './ledger';

const HEX64 = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const BANKS = ['BankAMSP', 'BankBMSP', 'BankCMSP'];
const MAX_BODY = 4096;
const ID_TYPES = ['ACCOUNT', 'UPI', 'PHONE'];
const SOURCES = ['SANDBOX_CAPTURE', 'ANALYST', 'CUSTOMER_COMPLAINT'];
const SEVERITIES = ['LOW', 'MEDIUM', 'HIGH'];

function send(res: http.ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(text) });
  res.end(text);
}

function same(given: string | string[] | undefined, expected: string): boolean {
  if (typeof given !== 'string' || !expected) return false;
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(new Error('too large'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

export function validateHashes(body: unknown): string[] {
  const hashes = (body as { idHashes?: unknown })?.idHashes;
  if (!Array.isArray(hashes) || hashes.length < 1 || hashes.length > 2) {
    throw new Error('idHashes must be an array of 1 hash, or 2 during a key rotation');
  }
  for (const h of hashes) {
    if (typeof h !== 'string' || !HEX64.test(h)) throw new Error('each idHash must be 64 lowercase hex characters');
  }
  if (hashes.length === 2 && hashes[0] === hashes[1]) throw new Error('the two idHashes must differ');
  return hashes as string[];
}

function field(body: unknown, name: string, check: (v: string) => boolean, rule: string): string {
  const v = (body as Record<string, unknown> | null)?.[name];
  if (typeof v !== 'string' || !check(v)) throw new Error(`${name} must be ${rule}`);
  return v;
}
const hex = (v: string) => HEX64.test(v);
const oneOf = (list: string[]) => (v: string) => list.includes(v);

/** reportMule(idHash, idType, evidenceHash, source, severity) */
export function validateReport(body: unknown): string[] {
  return [
    field(body, 'idHash', hex, '64 lowercase hex characters'),
    field(body, 'idType', oneOf(ID_TYPES), ID_TYPES.join(' | ')),
    field(body, 'evidenceHash', hex, '64 lowercase hex characters'),
    field(body, 'source', oneOf(SOURCES), SOURCES.join(' | ')),
    field(body, 'severity', oneOf(SEVERITIES), SEVERITIES.join(' | ')),
  ];
}

/** confirmReport(reportId, evidenceHash, source). Report ids are Fabric transaction ids. */
export function validateConfirm(body: unknown): string[] {
  return [
    field(body, 'reportId', hex, 'a 64-character transaction id'),
    field(body, 'evidenceHash', hex, '64 lowercase hex characters'),
    field(body, 'source', oneOf(SOURCES), SOURCES.join(' | ')),
  ];
}

/**
 * recordPayment: the details go to the endorsing peers as transient data (never into the block); the
 * chaincode validates every field. Here: shape, size, and that the sender is the identity we act as.
 */
export function validatePayment(body: unknown, actingAs: string): { paymentId: string; details: Record<string, unknown>; endorsers: string[] } {
  const d = (body as { payment?: unknown })?.payment;
  if (!d || typeof d !== 'object' || Array.isArray(d)) throw new Error('payment must be an object');
  const details = d as Record<string, unknown>;
  if (typeof details.paymentId !== 'string' || !UUID.test(details.paymentId)) throw new Error('payment.paymentId must be a lowercase UUID');
  if (details.fromBank !== actingAs) throw new Error(`payment.fromBank must be the sending bank (${actingAs})`);
  if (typeof details.toBank !== 'string' || !BANKS.includes(details.toBank)) throw new Error(`payment.toBank must be one of ${BANKS.join(', ')}`);
  // The two banks in the payment endorse: they are the collection's members, so their peers keep the details.
  // One bank (both customers there): that bank plus one other, for the 2-of-3 chaincode policy; the other
  // peer stores only the hash.
  const endorsers = details.fromBank === details.toBank
    ? [actingAs, BANKS.find((b) => b !== actingAs)!]
    : [actingAs, details.toBank];
  return { paymentId: details.paymentId, details, endorsers };
}

function errorMessage(err: unknown): string {
  return String((err as Error)?.message ?? err).split('\n')[0].slice(0, 200);
}

function ledgerFailure(res: http.ServerResponse, what: string, err: unknown): void {
  const e = err instanceof LedgerError ? err : new LedgerError(`ledger call failed: ${errorMessage(err)}`, 'unavailable');
  // Logged every time: `docker logs sentinel-ledger-gateway` should say why, without a debugging session.
  console.error(`${new Date().toISOString()} ${what} failed [${e.kind}]: ${e.message}`);
  const status = e.kind === 'config' ? 503 : e.kind === 'timeout' ? 504 : e.kind === 'chaincode' ? 422 : 502;
  send(res, status, { error: e.message, kind: e.kind });
}

export interface Tokens {
  read: string;
  write: string; // empty: writes disabled
}

export interface Network {
  /** Every organisation this sidecar can act as, by MSP id (the demo bank network). */
  orgs?: Map<string, LedgerClient>;
  identities?: () => Promise<OrgIdentity[]>;
  cas?: () => Promise<CAs>;
}

const WRITES = ['/report-mule', '/confirm-report', '/record-payment'];

export function makeServer(ledger: LedgerClient, tokens: Tokens | string, net: Network = {}): http.Server {
  const t: Tokens = typeof tokens === 'string' ? { read: tokens, write: '' } : tokens;
  const orgs = net.orgs ?? new Map<string, LedgerClient>();
  /** The identity to act as: the default, or a named organisation of the demo network. */
  const actAs = (org: unknown): LedgerClient => {
    if (org === undefined || org === null || org === '') return ledger;
    const l = typeof org === 'string' ? orgs.get(org) : undefined;
    if (!l) throw new Error(`org must be one of ${[...orgs.keys()].join(', ') || '(none: ORGS is not set)'}`);
    return l;
  };
  return http.createServer(async (req, res) => {
    const started = Date.now();
    const given = req.headers['x-ledger-token'];
    const canWrite = same(given, t.write);
    const canRead = canWrite || same(given, t.read);
    const isWrite = req.method === 'POST' && WRITES.includes(req.url ?? '');
    try {
      if (isWrite && !t.write) return send(res, 404, { error: 'not found' });
      if (!canRead) return send(res, 401, { error: 'ledger token required' });
      if (isWrite && !canWrite) return send(res, 403, { error: 'this token can only read' });

      if (req.method === 'GET' && req.url === '/health') {
        try {
          const ping = JSON.parse(await ledger.ping());
          return send(res, 200, { ok: true, msp: ping.msp, writes: Boolean(t.write), latencyMs: Date.now() - started });
        } catch (err) {
          return ledgerFailure(res, 'health', err);
        }
      }

      // Public keys of the network (read token). Certificates only: private keys never leave.
      if (req.method === 'GET' && req.url === '/identities') {
        if (!net.identities) return send(res, 404, { error: 'not found' });
        return send(res, 200, { orgs: await net.identities() });
      }

      // A payment as one organisation sees it (read token): members get the details, others the hash.
      if (req.method === 'GET' && req.url?.startsWith('/payment/')) {
        const url = new URL(req.url, 'http://x');
        const m = url.pathname.match(/^\/payment\/([0-9a-f-]{36})$/);
        let as: LedgerClient;
        try {
          if (!m || !UUID.test(m[1])) throw new Error('payment id must be a lowercase UUID');
          as = actAs(url.searchParams.get('org'));
          if (!as.getPayment) throw new Error('this ledger cannot read payments');
        } catch (err) {
          return send(res, 400, { error: (err as Error).message });
        }
        try {
          return send(res, 200, { result: JSON.parse(await as.getPayment!(m![1])), latencyMs: Date.now() - started });
        } catch (err) {
          return ledgerFailure(res, 'getPayment', err);
        }
      }

      // Chain views (read token): the latest blocks, and the block that holds a given transaction.
      if (req.method === 'GET' && req.url?.startsWith('/chain/')) {
        const url = new URL(req.url, 'http://x');
        try {
          const cas = net.cas ? await net.cas() : undefined;
          if (url.pathname === '/chain/blocks') {
            const count = Math.min(Math.max(Number(url.searchParams.get('count') ?? 8) || 8, 1), 20);
            const info = parseChainInfo(await ledger.chainInfo());
            const blocks = [];
            for (let n = info.height - 1; n >= 0 && blocks.length < count; n--) {
              blocks.push(parseBlock(await ledger.blockByNumber(n), cas));
            }
            return send(res, 200, { info, blocks, latencyMs: Date.now() - started });
          }
          const tx = url.pathname.match(/^\/chain\/tx\/([0-9a-f]{64})$/);
          if (tx) {
            return send(res, 200, { block: parseBlock(await ledger.blockByTxId(tx[1]), cas), latencyMs: Date.now() - started });
          }
        } catch (err) {
          return ledgerFailure(res, 'chain', err);
        }
        return send(res, 404, { error: 'not found' });
      }

      if (req.method === 'POST' && (req.url === '/query-payee' || isWrite)) {
        let body: unknown;
        try {
          body = JSON.parse(await readBody(req));
        } catch (err) {
          const big = (err as Error).message === 'too large';
          return send(res, big ? 413 : 400, { error: big ? 'body too large' : 'body must be JSON' });
        }
        let fn: 'queryPayee' | WriteFunction;
        let args: string[];
        let as: LedgerClient;
        let opts: SubmitOptions = {};
        try {
          as = actAs((body as { org?: unknown })?.org);
          const msp = (as as { mspId?: string }).mspId ?? 'BankAMSP';
          if (req.url === '/query-payee') {
            fn = 'queryPayee';
            args = validateHashes(body);
          } else if (req.url === '/report-mule') {
            fn = 'reportMule';
            args = validateReport(body);
          } else if (req.url === '/confirm-report') {
            fn = 'confirmReport';
            args = validateConfirm(body);
          } else {
            fn = 'recordPayment';
            const p = validatePayment(body, msp);
            if (!as.submitTx) throw new Error('this ledger cannot record payments');
            args = [p.paymentId];
            opts = { transient: { payment: Buffer.from(JSON.stringify(p.details)) }, endorsingOrganizations: p.endorsers };
          }
        } catch (err) {
          return send(res, 400, { error: (err as Error).message });
        }
        try {
          if (fn === 'queryPayee') {
            return send(res, 200, { result: JSON.parse(await as.queryPayee(args)), latencyMs: Date.now() - started });
          }
          let result: string;
          let txId: string | null = null;
          let blockNumber: number | null = null;
          if (as.submitTx) {
            ({ result, txId, blockNumber } = await as.submitTx(fn, args, opts));
          } else {
            result = await as.submit(fn, args);
          }
          console.log(`${new Date().toISOString()} ${fn} as ${(as as { mspId?: string }).mspId ?? 'default'} committed in ${Date.now() - started} ms`);
          return send(res, 200, { result: JSON.parse(result), txId, blockNumber, latencyMs: Date.now() - started });
        } catch (err) {
          return ledgerFailure(res, fn, err);
        }
      }

      return send(res, 404, { error: 'not found' });
    } catch {
      if (!res.headersSent) send(res, 500, { error: 'internal error' });
    }
  });
}
