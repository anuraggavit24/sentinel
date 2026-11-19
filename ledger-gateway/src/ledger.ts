/**
 * The ledger calls this service can make. Reads: queryPayee and ping (evaluations on one peer).
 * Writes (Module 3): reportMule and confirmReport only, submitted through the Gateway, which collects
 * endorsements from two banks as the chaincode's policy requires, orders and waits for the commit.
 */
import * as grpc from '@grpc/grpc-js';
import { CommitError, connect, Contract, Gateway, Network, signers } from '@hyperledger/fabric-gateway';
import * as crypto from 'node:crypto';
import { promises as fs } from 'node:fs';
import * as path from 'node:path';
import { Config, OrgConfig } from './config';

export type WriteFunction = 'reportMule' | 'confirmReport' | 'recordPayment';

export interface SubmitOptions {
  /** Private inputs: sent to the endorsing peers only, never written into the block. */
  transient?: Record<string, Uint8Array>;
  /** Which organisations' peers must endorse (for private data: the collection's members). */
  endorsingOrganizations?: string[];
}

export interface Submitted {
  result: string;
  txId: string;
  blockNumber: number | null;
}

export interface LedgerClient {
  queryPayee(idHashes: string[]): Promise<string>;
  ping(): Promise<string>;
  submit(fn: WriteFunction, args: string[]): Promise<string>;
  /** A write that also reports its transaction id (and can carry private data). Optional for fakes. */
  submitTx?(fn: WriteFunction, args: string[], opts?: SubmitOptions): Promise<Submitted>;
  /** getPayment(paymentId) as this organisation, on its own peer. */
  getPayment?(paymentId: string): Promise<string>;
  /** Raw protobuf bytes from the peer's query system chaincode (qscc): chain info, and blocks. */
  chainInfo(): Promise<Uint8Array>;
  blockByNumber(n: number): Promise<Uint8Array>;
  blockByTxId(txId: string): Promise<Uint8Array>;
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
  private qscc?: Contract;
  private connecting?: Promise<Contract>;

  private readonly org: Pick<OrgConfig, 'mspId' | 'peerEndpoint' | 'peerHostAlias' | 'tlsCertPath' | 'mspDir'>;

  /** As the default identity in cfg, or as another organisation of the demo network. */
  constructor(private readonly cfg: Config, org?: OrgConfig) {
    this.org = org ?? cfg;
  }

  get mspId(): string {
    return this.org.mspId;
  }

  private async open(): Promise<Contract> {
    const tlsRoot = await readCredential(this.org.tlsCertPath);
    const certPath = await firstFile(path.join(this.org.mspDir, 'signcerts'));
    const keyPath = await firstFile(path.join(this.org.mspDir, 'keystore'));
    const certificate = await readCredential(certPath);
    let privateKey: crypto.KeyObject;
    try {
      privateKey = crypto.createPrivateKey(await readCredential(keyPath));
    } catch (err) {
      if (err instanceof LedgerError) throw err;
      throw new LedgerError(`not a usable private key: ${keyPath}`, 'config');
    }
    const client = new grpc.Client(this.org.peerEndpoint, grpc.credentials.createSsl(tlsRoot), {
      'grpc.ssl_target_name_override': this.org.peerHostAlias,
    });
    const gateway = connect({
      client,
      identity: { mspId: this.org.mspId, credentials: certificate },
      signer: signers.newPrivateKeySigner(privateKey),
      evaluateOptions: () => ({ deadline: Date.now() + this.cfg.timeoutMs }),
      endorseOptions: () => ({ deadline: Date.now() + this.cfg.submitTimeoutMs }),
      submitOptions: () => ({ deadline: Date.now() + this.cfg.submitTimeoutMs }),
      commitStatusOptions: () => ({ deadline: Date.now() + this.cfg.submitTimeoutMs }),
    });
    this.client = client;
    this.gateway = gateway;
    const network: Network = gateway.getNetwork(this.cfg.channel);
    this.contract = network.getContract(this.cfg.chaincode);
    this.qscc = network.getContract('qscc');
    return this.contract;
  }

  private async contractOrConnect(): Promise<Contract> {
    if (this.contract) return this.contract;
    // One connection attempt at a time, however many requests arrive together.
    this.connecting ??= this.open().finally(() => (this.connecting = undefined));
    return this.connecting;
  }

  private async connected(): Promise<Contract> {
    try {
      return await this.contractOrConnect();
    } catch (err) {
      if (err instanceof LedgerError) throw err;
      throw new LedgerError(`could not set up the connection: ${String((err as Error)?.message ?? err).split('\n')[0].slice(0, 200)}`, 'config');
    }
  }

  private async call(fn: string, args: string[], write: boolean): Promise<string> {
    if (write) return (await this.submitTx(fn as WriteFunction, args)).result;
    const contract = await this.connected();
    try {
      return Buffer.from(await contract.evaluateTransaction(fn, ...args)).toString('utf8');
    } catch (err) {
      throw this.translate(err);
    }
  }

  /** Endorse (the Gateway collects the endorsements the policy needs), order, then wait for the commit. */
  async submitTx(fn: WriteFunction, args: string[], opts: SubmitOptions = {}): Promise<Submitted> {
    const contract = await this.connected();
    try {
      const proposal = contract.newProposal(fn, {
        arguments: args,
        transientData: opts.transient,
        endorsingOrganizations: opts.endorsingOrganizations,
      });
      const txn = await proposal.endorse();
      const commit = await txn.submit();
      const status = await commit.getStatus();
      if (!status.successful) {
        throw new LedgerError(`transaction ${status.transactionId} was not valid (validation code ${status.code})`, 'commit');
      }
      return { result: Buffer.from(txn.getResult()).toString('utf8'), txId: proposal.getTransactionId(),
        blockNumber: Number(status.blockNumber) };
    } catch (err) {
      if (err instanceof LedgerError) throw err;
      throw this.translate(err);
    }
  }

  getPayment(paymentId: string): Promise<string> {
    return this.call('getPayment', [paymentId], false);
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

  private async system(fn: string, args: string[]): Promise<Uint8Array> {
    await this.connected();
    try {
      return await this.qscc!.evaluateTransaction(fn, this.cfg.channel, ...args);
    } catch (err) {
      throw this.translate(err);
    }
  }

  chainInfo(): Promise<Uint8Array> {
    return this.system('GetChainInfo', []);
  }

  blockByNumber(n: number): Promise<Uint8Array> {
    return this.system('GetBlockByNumber', [String(n)]);
  }

  blockByTxId(txId: string): Promise<Uint8Array> {
    return this.system('GetBlockByTxID', [txId]);
  }

  close(): void {
    this.gateway?.close();
    this.client?.close();
    this.gateway = this.client = this.contract = this.qscc = undefined;
  }
}
