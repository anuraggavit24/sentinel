import { createHash } from 'crypto';

type Ts = { seconds: number; nanos: number };
type Mod = { txId: string; timestamp: Ts; isDelete: boolean; value: Buffer };

function iter<T>(items: T[]) {
  let i = 0;
  return {
    async next(): Promise<{ value: T | undefined; done: boolean }> {
      return i < items.length ? { value: items[i++], done: false } : { value: undefined, done: true };
    },
    async close(): Promise<void> {
      /* nothing to release */
    },
  };
}

/** In-memory stand-in for Fabric's ChaincodeStub, covering only what the contract uses. */
export class FakeStub {
  private state = new Map<string, Buffer>(); // committed world state
  private pending = new Map<string, Buffer | null>(); // this transaction's writes (null = delete)
  private history = new Map<string, Mod[]>();
  private priv = new Map<string, Buffer>(); // "collection\u0000key" -> committed private value
  private privPending = new Map<string, Buffer>();
  public transient = new Map<string, Uint8Array>();
  /** Collections this peer's organisation belongs to; null = all (peer of every org). */
  public memberOf: Set<string> | null = null;
  private counter = 0;
  private txId = 'tx0000';
  private ts: Ts = { seconds: 1763164800, nanos: 0 };
  public events: { name: string; payload: Buffer }[] = [];

  /** Commits the current transaction's writes, like a block commit. Reads never see uncommitted writes. */
  commit(): void {
    for (const [key, value] of this.pending) {
      if (value === null) {
        this.state.delete(key);
        this.record(key, true, Buffer.alloc(0));
      } else {
        this.state.set(key, value);
        this.record(key, false, value);
      }
    }
    this.pending.clear();
    for (const [k, v] of this.privPending) this.priv.set(k, v);
    this.privPending.clear();
  }

  /** Discards the current transaction's writes. A failed Fabric transaction changes nothing. */
  rollback(): void {
    this.pending.clear();
    this.privPending.clear();
  }

  nextTx(txId?: string): void {
    this.commit();
    this.counter += 1;
    this.txId = txId ?? `tx${String(this.counter).padStart(4, '0')}`;
    this.ts = { seconds: 1763164800 + this.counter, nanos: 0 };
  }

  getTxID(): string {
    return this.txId;
  }

  getTxTimestamp(): Ts {
    return this.ts;
  }

  async getState(key: string): Promise<Buffer> {
    return this.state.get(key) ?? Buffer.alloc(0);
  }

  async putState(key: string, value: Uint8Array): Promise<void> {
    const buf = Buffer.from(value);
    if (buf.length === 0) throw new Error('putState with an empty value is a delete in Fabric');
    this.pending.set(key, buf);
  }

  async deleteState(key: string): Promise<void> {
    this.pending.set(key, null);
  }

  getTransient(): Map<string, Uint8Array> {
    return this.transient;
  }

  async putPrivateData(collection: string, key: string, value: Uint8Array): Promise<void> {
    this.privPending.set(`${collection}\u0000${key}`, Buffer.from(value));
  }

  /** Like a peer: a non-member peer does not hold the data at all. */
  async getPrivateData(collection: string, key: string): Promise<Buffer> {
    if (this.memberOf && !this.memberOf.has(collection)) {
      throw new Error(`peer is not a member of collection ${collection}`);
    }
    return this.priv.get(`${collection}\u0000${key}`) ?? Buffer.alloc(0);
  }

  /** Every peer on the channel keeps the hash of private data, member or not. */
  async getPrivateDataHash(collection: string, key: string): Promise<Buffer> {
    const v = this.priv.get(`${collection}\u0000${key}`);
    return v ? createHash('sha256').update(v).digest() : Buffer.alloc(0);
  }

  /** Test only: change a bank's stored private copy, as a tamperer with database access would. */
  tamperPrivate(collection: string, key: string, value: Buffer): void {
    this.priv.set(`${collection}\u0000${key}`, value);
  }

  setEvent(name: string, payload: Uint8Array): void {
    this.events.push({ name, payload: Buffer.from(payload) });
  }

  createCompositeKey(objectType: string, attributes: string[]): string {
    return `\u0000${objectType}\u0000${attributes.map((a) => `${a}\u0000`).join('')}`;
  }

  splitCompositeKey(key: string): { objectType: string; attributes: string[] } {
    const parts = key.split('\u0000').filter((_, i, arr) => i > 0 && i < arr.length - 1);
    return { objectType: parts[0], attributes: parts.slice(1) };
  }

  async getStateByPartialCompositeKey(objectType: string, attributes: string[]) {
    const prefix = this.createCompositeKey(objectType, attributes);
    const keys = [...this.state.keys()].filter((k) => k.startsWith(prefix)).sort();
    return iter(keys.map((key) => ({ key, value: this.state.get(key) as Buffer })));
  }

  /** Supports simple equality selectors, which is all the contract sends. */
  async getQueryResult(query: string) {
    const { selector } = JSON.parse(query) as { selector: Record<string, unknown> };
    const rows: { key: string; value: Buffer }[] = [];
    for (const key of [...this.state.keys()].sort()) {
      const value = this.state.get(key) as Buffer;
      let doc: Record<string, unknown>;
      try {
        doc = JSON.parse(value.toString('utf8'));
      } catch {
        continue; // index entries are not JSON
      }
      if (Object.entries(selector).every(([field, expected]) => doc[field] === expected)) rows.push({ key, value });
    }
    return iter(rows);
  }

  /** Fabric returns key history newest first, so the stub does too. */
  async getHistoryForKey(key: string) {
    return iter([...(this.history.get(key) ?? [])].reverse());
  }

  rawState(key: string): Buffer | undefined {
    this.commit();
    return this.state.get(key);
  }

  private record(key: string, isDelete: boolean, value: Buffer): void {
    const list = this.history.get(key) ?? [];
    list.push({ txId: this.txId, timestamp: { ...this.ts }, isDelete, value });
    this.history.set(key, list);
  }
}
