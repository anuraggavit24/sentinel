/**
 * The ledger calls this service can make. Reads: queryPayee and ping (evaluations on one peer).
 * Writes (Module 3): reportMule and confirmReport only, submitted through the Gateway, which collects
 * endorsements from two banks as the chaincode's policy requires, orders and waits for the commit.
 */
import * as grpc from '@grpc/grpc-js';
import { CommitError, connect, Contract, Gateway, signers } from '@hyperledger/fabric-gateway';
import * as crypto from 'node:crypto';
import { promises as fs } from 'node:fs';
import * as path from 'node:path';
import { Config } from './config';

export type WriteFunction = 'reportMule' | 'confirmReport';

export interface LedgerClient {
  queryPayee(idHashes: string[]): Promise<string>;
  ping(): Promise<string>;
  submit(fn: WriteFunction, args: string[]): Promise<string>;
  close(): void;
}

// commit = the transaction was ordered but the peers marked it invalid (e.g. a read conflict): retryable.
export type ErrorKind = 'config' | 'unavailable' | 'timeout' | 'chaincode' | 'commit';

export class LedgerError extends Error {
  constructor(message: string, readonly kind: ErrorKind) {
    super(message);
  }
}

/** Read a credential file; any failure (missing, permission, a directory) is a named config error. */
async function readCredential(file: string): Promise<Buffer> {
  try {
    return await fs.readFile(file);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code ?? 'read failed';
    const hint = code === 'EACCES' ? ' (the container user cannot read it: check the file owner and the compose "user:")' : '';
    throw new LedgerError(`cannot read ${file}: ${code}${hint}`, 'config');
  }
}

async function firstFile(dir: string): Promise<string> {
  let names: string[];
  try {
    names = (await fs.readdir(dir)).filter((n) => !n.startsWith('.')).sort();
  } catch {
    throw new LedgerError(`not found: ${dir}`, 'config');
  }
  if (names.length === 0) throw new LedgerError(`empty: ${dir}`, 'config');
  return path.join(dir, names[0]);
}

export class FabricLedger implements LedgerClient {
  private client?: grpc.Client;
  private gateway?: Gateway;
  private contract?: Contract;
  private connecting?: Promise<Contract>;

  constructor(private readonly cfg: Config) {}

  private async open(): Promise<Contract> {
    const tlsRoot = await readCredential(this.cfg.tlsCertPath);
    const certPath = await firstFile(path.join(this.cfg.mspDir, 'signcerts'));
    const keyPath = await firstFile(path.join(this.cfg.mspDir, 'keystore'));
    const certificate = await readCredential(certPath);
    let privateKey: crypto.KeyObject;
    try {
      privateKey = crypto.createPrivateKey(await readCredential(keyPath));
    } catch (err) {
      if (err instanceof LedgerError) throw err;
      throw new LedgerError(`not a usable private key: ${keyPath}`, 'config');
    }
    const client = new grpc.Client(this.cfg.peerEndpoint, grpc.credentials.createSsl(tlsRoot), {
      'grpc.ssl_target_name_override': this.cfg.peerHostAlias,
    });
    const gateway = connect({
      client,
      identity: { mspId: this.cfg.mspId, credentials: certificate },
      signer: signers.newPrivateKeySigner(privateKey),
      evaluateOptions: () => ({ deadline: Date.now() + this.cfg.timeoutMs }),
      endorseOptions: () => ({ deadline: Date.now() + this.cfg.submitTimeoutMs }),
      submitOptions: () => ({ deadline: Date.now() + this.cfg.submitTimeoutMs }),
      commitStatusOptions: () => ({ deadline: Date.now() + this.cfg.submitTimeoutMs }),
    });
    this.client = client;
    this.gateway = gateway;
    this.contract = gateway.getNetwork(this.cfg.channel).getContract(this.cfg.chaincode);
    return this.contract;
  }

  private async contractOrConnect(): Promise<Contract> {
    if (this.contract) return this.contract;
    // One connection attempt at a time, however many requests arrive together.
    this.connecting ??= this.open().finally(() => (this.connecting = undefined));
    return this.connecting;
  }

  private async call(fn: string, args: string[], write: boolean): Promise<string> {
    let contract: Contract;
    try {
      contract = await this.contractOrConnect();
    } catch (err) {
      if (err instanceof LedgerError) throw err;
      throw new LedgerError(`could not set up the connection: ${String((err as Error)?.message ?? err).split('\n')[0].slice(0, 200)}`, 'config');
    }
    try {
      const bytes = write ? await contract.submitTransaction(fn, ...args) : await contract.evaluateTransaction(fn, ...args);
      return Buffer.from(bytes).toString('utf8');
    } catch (err) {
      throw this.translate(err);
    }
  }

  private translate(err: unknown): LedgerError {
    // Checked first: a CommitError's code is a transaction validation code, not a gRPC status.
    if (err instanceof CommitError) {
      return new LedgerError(`transaction ${err.transactionId} was not valid (validation code ${err.code})`, 'commit');
    }
    const e = err as { code?: number; details?: unknown; message?: string };
    if (e.code === grpc.status.DEADLINE_EXCEEDED) return new LedgerError('ledger did not answer in time', 'timeout');
    if (e.code === grpc.status.UNAVAILABLE) {
      this.close(); // reconnect on the next request
      return new LedgerError('peer unreachable', 'unavailable');
    }
    // Chaincode errors arrive inside the gRPC details; keep the first line, never a stack.
    const details = Array.isArray(e.details) ? e.details.map((d: { message?: string }) => d.message).join('; ') : '';
    const msg = (details || e.message || 'evaluate failed').split('\n')[0].slice(0, 200);
    return new LedgerError(msg, 'chaincode');
  }

  queryPayee(idHashes: string[]): Promise<string> {
    return this.call('queryPayee', [JSON.stringify(idHashes)], false);
  }

  ping(): Promise<string> {
    return this.call('ping', [], false);
  }

  submit(fn: WriteFunction, args: string[]): Promise<string> {
    return this.call(fn, args, true);
  }

  close(): void {
    this.gateway?.close();
    this.client?.close();
    this.gateway = this.client = this.contract = undefined;
  }
}
