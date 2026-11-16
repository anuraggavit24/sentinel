/**
 * HTTP front for the ledger client. Internal only: not published on the host.
 * - Reads (/health, /query-payee) need LEDGER_TOKEN (or the write token).
 * - Writes (/report-mule, /confirm-report) need LEDGER_WRITE_TOKEN, and do not exist without it.
 * Every request is validated here before Fabric sees it.
 */
import * as crypto from 'node:crypto';
import * as http from 'node:http';
import { LedgerClient, LedgerError } from './ledger';

const HEX64 = /^[0-9a-f]{64}$/;
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

export function makeServer(ledger: LedgerClient, tokens: Tokens | string): http.Server {
  const t: Tokens = typeof tokens === 'string' ? { read: tokens, write: '' } : tokens;
  return http.createServer(async (req, res) => {
    const started = Date.now();
    const given = req.headers['x-ledger-token'];
    const canWrite = same(given, t.write);
    const canRead = canWrite || same(given, t.read);
    const isWrite = req.method === 'POST' && (req.url === '/report-mule' || req.url === '/confirm-report');
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

      if (req.method === 'POST' && (req.url === '/query-payee' || isWrite)) {
        let body: unknown;
        try {
          body = JSON.parse(await readBody(req));
        } catch (err) {
          const big = (err as Error).message === 'too large';
          return send(res, big ? 413 : 400, { error: big ? 'body too large' : 'body must be JSON' });
        }
        let fn: 'queryPayee' | 'reportMule' | 'confirmReport';
        let args: string[];
        try {
          if (req.url === '/query-payee') {
            fn = 'queryPayee';
            args = validateHashes(body);
          } else if (req.url === '/report-mule') {
            fn = 'reportMule';
            args = validateReport(body);
          } else {
            fn = 'confirmReport';
            args = validateConfirm(body);
          }
        } catch (err) {
          return send(res, 400, { error: (err as Error).message });
        }
        try {
          const raw = fn === 'queryPayee' ? await ledger.queryPayee(args) : await ledger.submit(fn, args);
          if (fn !== 'queryPayee') console.log(`${new Date().toISOString()} ${fn} committed in ${Date.now() - started} ms`);
          return send(res, 200, { result: JSON.parse(raw), latencyMs: Date.now() - started });
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
