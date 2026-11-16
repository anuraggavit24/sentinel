/**
 * HTTP front for the ledger client. Internal only: not published on the host, and every request needs
 * the shared LEDGER_TOKEN. It accepts exactly two requests and validates them before Fabric sees them.
 */
import * as crypto from 'node:crypto';
import * as http from 'node:http';
import { LedgerClient, LedgerError } from './ledger';

const HEX64 = /^[0-9a-f]{64}$/;
const MAX_BODY = 4096;

function send(res: http.ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(text) });
  res.end(text);
}

function tokenOk(given: string | string[] | undefined, expected: string): boolean {
  if (typeof given !== 'string') return false;
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

function ledgerFailure(res: http.ServerResponse, what: string, err: unknown): void {
  const e = err instanceof LedgerError
    ? err
    : new LedgerError(`ledger call failed: ${String((err as Error)?.message ?? err).split('\n')[0].slice(0, 200)}`, 'unavailable');
  // Logged every time: `docker logs sentinel-ledger-gateway` should say why, without a debugging session.
  console.error(`${new Date().toISOString()} ${what} failed [${e.kind}]: ${e.message}`);
  const status = e.kind === 'config' ? 503 : e.kind === 'timeout' ? 504 : 502;
  send(res, status, { error: e.message, kind: e.kind });
}

export function makeServer(ledger: LedgerClient, token: string): http.Server {
  return http.createServer(async (req, res) => {
    const started = Date.now();
    try {
      if (!tokenOk(req.headers['x-ledger-token'], token)) return send(res, 401, { error: 'ledger token required' });

      if (req.method === 'GET' && req.url === '/health') {
        try {
          const ping = JSON.parse(await ledger.ping());
          return send(res, 200, { ok: true, msp: ping.msp, latencyMs: Date.now() - started });
        } catch (err) {
          return ledgerFailure(res, 'health', err);
        }
      }

      if (req.method === 'POST' && req.url === '/query-payee') {
        let hashes: string[];
        try {
          hashes = validateHashes(JSON.parse(await readBody(req)));
        } catch (err) {
          const msg = (err as Error).message === 'too large' ? 'body too large' : (err as Error).message;
          return send(res, (err as Error).message === 'too large' ? 413 : 400, { error: msg });
        }
        try {
          const result = JSON.parse(await ledger.queryPayee(hashes));
          return send(res, 200, { result, latencyMs: Date.now() - started });
        } catch (err) {
          return ledgerFailure(res, 'queryPayee', err);
        }
      }

      return send(res, 404, { error: 'not found' });
    } catch {
      if (!res.headersSent) send(res, 500, { error: 'internal error' });
    }
  });
}
