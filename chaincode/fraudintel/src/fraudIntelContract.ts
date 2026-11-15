import { Context, Contract, Info, Returns, Transaction } from 'fabric-contract-api';
import stringify from 'json-stringify-deterministic';
import sortKeysRecursive from 'sort-keys-recursive';
import {
  ALL_MSPS,
  AuditAnchor,
  BANK_MSPS,
  ID_TYPES,
  IdType,
  MuleReport,
  OUTCOMES,
  Outcome,
  REGULATOR_MSP,
  SEVERITIES,
  SOURCES,
  Severity,
  Source,
  Status,
  WeightedReport,
} from './types';

const INDEX_ID_HASH = 'idHash~reportId';
const ANCHOR_KEY = 'anchor';
const HEX64 = /^[0-9a-f]{64}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ACTIVE: readonly Status[] = ['PENDING', 'CONFIRMED', 'DISPUTED'];
const INDEX_VALUE = Buffer.from('\u0000'); // putState with an empty value means delete in Fabric

// ---------- deterministic helpers (every endorsing peer must produce identical bytes) ----------

function toJson(value: object): string {
  return stringify(sortKeysRecursive(value));
}

function toBytes(value: object): Uint8Array {
  return Buffer.from(toJson(value));
}

interface TimestampLike {
  seconds?: unknown;
  nanos?: unknown;
  getSeconds?: () => unknown;
  getNanos?: () => unknown;
}

function isoFromTimestamp(ts: TimestampLike): string {
  const rawSeconds = typeof ts.getSeconds === 'function' ? ts.getSeconds() : ts.seconds;
  const rawNanos = typeof ts.getNanos === 'function' ? ts.getNanos() : ts.nanos;
  const seconds = Number(String(rawSeconds));
  const millis = Math.floor(Number(String(rawNanos ?? 0)) / 1e6);
  return new Date(seconds * 1000 + millis).toISOString();
}

function txTime(ctx: Context): string {
  return isoFromTimestamp(ctx.stub.getTxTimestamp() as unknown as TimestampLike);
}

function oneOf<T extends string>(name: string, value: string, allowed: readonly T[]): T {
  if (!(allowed as readonly string[]).includes(value)) {
    throw new Error(`${name} must be one of ${allowed.join(', ')}; got "${value}"`);
  }
  return value as T;
}

function hex64(name: string, value: string): string {
  if (!HEX64.test(value)) {
    throw new Error(`${name} must be a 64-character lowercase hex SHA-256/HMAC value`);
  }
  return value;
}

function reportKey(reportId: string): string {
  return `report:${reportId}`;
}

// ---------- scoring rules ----------

/** Banks whose involvement in a report is not a sandbox capture. */
function nonSandboxBanks(r: MuleReport): Set<string> {
  const orgs = new Set<string>();
  if (r.source !== 'SANDBOX_CAPTURE') orgs.add(r.reporterOrg);
  for (const c of r.confirmations) {
    if (c.source !== 'SANDBOX_CAPTURE') orgs.add(c.org);
  }
  return orgs;
}

function baseWeight(status: Status, r: MuleReport): number {
  if (status === 'CONFIRMED') return 1;
  if (status === 'PENDING') return nonSandboxBanks(r).size > 0 ? 0.25 : 0;
  return 0;
}

/** A disputed report keeps half of the weight it had before the dispute, never more. */
function weightOf(r: MuleReport): number {
  if (r.status === 'DISPUTED') return 0.5 * baseWeight(r.statusBeforeDispute ?? 'PENDING', r);
  return baseWeight(r.status, r);
}

@Info({ title: 'FraudIntel', description: 'Sentinel consortium fraud-intelligence ledger' })
export class FraudIntelContract extends Contract {
  constructor() {
    super('FraudIntel');
  }

  // ---------- hello world: deploy and call this before anything else ----------

  @Transaction(false)
  @Returns('string')
  public async ping(ctx: Context): Promise<string> {
    return toJson({ msp: ctx.clientIdentity.getMSPID(), txId: ctx.stub.getTxID(), txTime: txTime(ctx) });
  }

  // ---------- reports ----------

  @Transaction()
  @Returns('string')
  public async reportMule(
    ctx: Context,
    idHash: string,
    idType: string,
    evidenceHash: string,
    source: string,
    severity: string,
  ): Promise<string> {
    const org = this.requireBank(ctx);
    hex64('idHash', idHash);
    hex64('evidenceHash', evidenceHash);
    const type = oneOf<IdType>('idType', idType, ID_TYPES);
    const src = oneOf<Source>('source', source, SOURCES);
    const requested = oneOf<Severity>('severity', severity, SEVERITIES);
    const sev: Severity = src === 'SANDBOX_CAPTURE' ? 'LOW' : requested; // sandbox captures are capped

    const existing = await this.reportsForIdHash(ctx, idHash);
    const active = existing.find((r) => ACTIVE.includes(r.status));
    if (active) {
      throw new Error(`Active report ${active.reportId} already exists for this identifier; confirm or dispute it instead`);
    }

    const now = txTime(ctx);
    const reportId = ctx.stub.getTxID();
    const report: MuleReport = {
      docType: 'report',
      reportId,
      idHash,
      idType: type,
      reporterOrg: org,
      evidenceHash,
      source: src,
      severity: sev,
      confirmations: [],
      disputes: [],
      status: 'PENDING',
      statusBeforeDispute: null,
      resolution: null,
      keyVersion: 1,
      createdAt: now,
      updatedAt: now,
    };

    await this.save(ctx, report);
    await ctx.stub.putState(ctx.stub.createCompositeKey(INDEX_ID_HASH, [idHash, reportId]), INDEX_VALUE);
    ctx.stub.setEvent('MuleReported', toBytes({ reportId, reporterOrg: org, source: src }));
    return toJson(report);
  }

  @Transaction()
  @Returns('string')
  public async confirmReport(ctx: Context, reportId: string, evidenceHash: string, source: string): Promise<string> {
    const org = this.requireBank(ctx);
    hex64('evidenceHash', evidenceHash);
    const src = oneOf<Source>('source', source, SOURCES);
    const report = await this.load(ctx, reportId);

    if (report.status !== 'PENDING' && report.status !== 'CONFIRMED') {
      throw new Error(`Report ${reportId} is ${report.status} and cannot be confirmed`);
    }
    if (report.reporterOrg === org) {
      throw new Error('A bank cannot confirm its own report');
    }
    if (report.confirmations.some((c) => c.org === org)) {
      throw new Error(`${org} has already confirmed report ${reportId}`);
    }
    // Only stops verbatim reuse of evidence already on the report. The source rule below does the real work.
    const usedEvidence = [report.evidenceHash, ...report.confirmations.map((c) => c.evidenceHash)];
    if (usedEvidence.includes(evidenceHash)) {
      throw new Error('evidenceHash must differ from evidence already attached to this report');
    }

    const now = txTime(ctx);
    report.confirmations.push({ org, source: src, evidenceHash, txTime: now });
    // CONFIRMED needs 2 distinct banks with non-sandbox sources. Sandbox captures never count.
    if (report.status === 'PENDING' && nonSandboxBanks(report).size >= 2) {
      report.status = 'CONFIRMED';
    }
    report.updatedAt = now;
    await this.save(ctx, report);
    return toJson(report);
  }

  @Transaction()
  @Returns('string')
  public async disputeReport(ctx: Context, reportId: string, reasonHash: string): Promise<string> {
    const org = this.requireBank(ctx);
    hex64('reasonHash', reasonHash);
    const report = await this.load(ctx, reportId);

    if (!['PENDING', 'CONFIRMED', 'DISPUTED'].includes(report.status)) {
      throw new Error(`Report ${reportId} is ${report.status} and cannot be disputed`);
    }
    if (report.reporterOrg === org) {
      throw new Error('The reporter cannot dispute its own report; use retractReport');
    }
    if (report.disputes.some((d) => d.org === org)) {
      throw new Error(`${org} has already disputed report ${reportId}`);
    }

    const now = txTime(ctx);
    if (report.status !== 'DISPUTED') report.statusBeforeDispute = report.status;
    report.status = 'DISPUTED';
    report.disputes.push({ org, reasonHash, txTime: now });
    report.updatedAt = now;
    await this.save(ctx, report);
    return toJson(report);
  }

  @Transaction()
  @Returns('string')
  public async resolveDispute(ctx: Context, reportId: string, outcome: string): Promise<string> {
    this.requireRegulator(ctx);
    const result = oneOf<Outcome>('outcome', outcome, OUTCOMES);
    const report = await this.load(ctx, reportId);
    if (report.status !== 'DISPUTED') {
      throw new Error(`Report ${reportId} is ${report.status}; only DISPUTED reports can be resolved`);
    }
    const now = txTime(ctx);
    // UPHELD restores the status the report had before the dispute. It never promotes it:
    // a PENDING or sandbox-only report stays PENDING, so the Regulator cannot bypass the 2-bank rule.
    report.status = result === 'UPHELD' ? report.statusBeforeDispute ?? 'PENDING' : 'OVERTURNED';
    report.statusBeforeDispute = null;
    report.resolution = { outcome: result, txTime: now };
    report.updatedAt = now;
    await this.save(ctx, report);
    return toJson(report);
  }

  @Transaction()
  @Returns('string')
  public async retractReport(ctx: Context, reportId: string): Promise<string> {
    const org = this.requireBank(ctx);
    const report = await this.load(ctx, reportId);
    if (report.reporterOrg !== org) {
      throw new Error('Only the reporting bank can retract a report');
    }
    if (report.status === 'RETRACTED' || report.status === 'OVERTURNED') {
      throw new Error(`Report ${reportId} is already ${report.status}`);
    }
    report.status = 'RETRACTED';
    report.updatedAt = txTime(ctx);
    await this.save(ctx, report);
    return toJson(report);
  }

  // ---------- lookups ----------

  /** idHashesJson: JSON array of 1 hash, or 2 hashes (old + new) during a key rotation. */
  @Transaction(false)
  @Returns('string')
  public async queryPayee(ctx: Context, idHashesJson: string): Promise<string> {
    this.requireKnownOrg(ctx);
    let parsed: unknown;
    try {
      parsed = JSON.parse(idHashesJson);
    } catch {
      throw new Error('idHashesJson must be a JSON array of hashes');
    }
    if (!Array.isArray(parsed) || parsed.length < 1 || parsed.length > 2) {
      throw new Error('idHashesJson must hold 1 hash, or 2 during a key rotation');
    }

    const seen = new Set<string>();
    const reports: WeightedReport[] = [];
    for (const value of parsed) {
      if (typeof value !== 'string') throw new Error('Every idHash must be a string');
      hex64('idHash', value);
      for (const r of await this.reportsForIdHash(ctx, value)) {
        if (seen.has(r.reportId)) continue;
        seen.add(r.reportId);
        reports.push({
          reportId: r.reportId,
          idHash: r.idHash,
          status: r.status,
          source: r.source,
          severity: r.severity,
          reporterOrg: r.reporterOrg,
          keyVersion: r.keyVersion,
          nonSandboxBanks: nonSandboxBanks(r).size,
          weight: weightOf(r),
          hardRuleEligible: r.status === 'CONFIRMED',
        });
      }
    }

    const maxWeight = reports.reduce((m, r) => Math.max(m, r.weight), 0);
    return toJson({
      idHashes: parsed,
      maxWeight,
      hardRuleEligible: reports.some((r) => r.hardRuleEligible),
      reports,
    });
  }

  @Transaction(false)
  @Returns('string')
  public async getReport(ctx: Context, reportId: string): Promise<string> {
    this.requireKnownOrg(ctx);
    return toJson(await this.load(ctx, reportId));
  }

  @Transaction(false)
  @Returns('string')
  public async getHistory(ctx: Context, reportId: string): Promise<string> {
    this.requireKnownOrg(ctx);
    const iterator = await ctx.stub.getHistoryForKey(reportKey(reportId));
    const entries: object[] = [];
    try {
      let res = await iterator.next();
      while (!res.done) {
        const m = res.value;
        entries.push({
          txId: m.txId,
          txTime: isoFromTimestamp(m.timestamp as unknown as TimestampLike),
          isDelete: m.isDelete,
          value: m.isDelete ? null : JSON.parse(Buffer.from(m.value).toString('utf8')),
        });
        res = await iterator.next();
      }
    } finally {
      await iterator.close();
    }
    if (entries.length === 0) throw new Error(`Report ${reportId} does not exist`);
    // Fabric returns key history newest first. Return it oldest first (chronological) regardless,
    // sorting by transaction time with the transaction ID as a deterministic tie-breaker.
    const ordered = (entries as Array<{ txId: string; txTime: string }>).sort((x, y) =>
      x.txTime === y.txTime ? (x.txId < y.txId ? -1 : x.txId > y.txId ? 1 : 0) : x.txTime < y.txTime ? -1 : 1,
    );
    return toJson({ reportId, entries: ordered });
  }

  /** Stats are computed at query time with a CouchDB rich query. No counter keys, so no MVCC hot spots. */
  @Transaction(false)
  @Returns('string')
  public async getOrgStats(ctx: Context, org: string): Promise<string> {
    this.requireKnownOrg(ctx);
    const target = oneOf<string>('org', org, ALL_MSPS);
    const reports = await this.queryAllReports(ctx);

    let reportsFiled = 0;
    let confirmationsGiven = 0;
    let disputesFiled = 0;
    let disputesAccepted = 0; // report OVERTURNED: the disputing bank was right
    let disputesRejected = 0; // report UPHELD: the disputing bank was wrong
    for (const r of reports) {
      if (r.reporterOrg === target) reportsFiled += 1;
      if (r.confirmations.some((c) => c.org === target)) confirmationsGiven += 1;
      if (r.disputes.some((d) => d.org === target)) {
        disputesFiled += 1;
        if (r.resolution?.outcome === 'OVERTURNED') disputesAccepted += 1;
        if (r.resolution?.outcome === 'UPHELD') disputesRejected += 1;
      }
    }
    const resolved = disputesAccepted + disputesRejected;
    return toJson({
      org: target,
      reportsFiled,
      confirmationsGiven,
      disputesFiled,
      disputesAccepted,
      disputesRejected,
      disputesUnresolved: disputesFiled - resolved,
      disputeRejectionRate: resolved === 0 ? null : disputesRejected / resolved,
    });
  }

  // ---------- key rotation ----------

  /**
   * Each bank re-hashes only the reports it filed, using plaintext from its own off-chain evidence.
   * itemsJson: [{"reportId":"...","newIdHash":"..."}]
   */
  @Transaction()
  @Returns('string')
  public async rehashOwnRecords(ctx: Context, newKeyVersion: string, itemsJson: string): Promise<string> {
    const org = this.requireBank(ctx);
    const version = Number(newKeyVersion);
    if (!Number.isInteger(version) || version < 2) {
      throw new Error('newKeyVersion must be an integer of 2 or more');
    }
    let items: unknown;
    try {
      items = JSON.parse(itemsJson);
    } catch {
      throw new Error('itemsJson must be a JSON array');
    }
    if (!Array.isArray(items) || items.length < 1 || items.length > 100) {
      throw new Error('itemsJson must hold between 1 and 100 items');
    }

    const now = txTime(ctx);
    const seenIds = new Set<string>();
    for (const item of items as Array<{ reportId?: unknown; newIdHash?: unknown }>) {
      if (typeof item.reportId !== 'string' || typeof item.newIdHash !== 'string') {
        throw new Error('Each item needs string reportId and newIdHash');
      }
      // Fabric has no read-your-own-writes: a second load of the same report in this transaction
      // would see the committed (old) version, so duplicates must be rejected up front.
      if (seenIds.has(item.reportId)) throw new Error(`Duplicate reportId ${item.reportId} in itemsJson`);
      seenIds.add(item.reportId);
      hex64('newIdHash', item.newIdHash);
      const report = await this.load(ctx, item.reportId);
      if (report.reporterOrg !== org) {
        throw new Error(`${org} can only rehash reports it filed; ${report.reportId} belongs to ${report.reporterOrg}`);
      }
      if (version <= report.keyVersion) {
        throw new Error(`Report ${report.reportId} is already at key version ${report.keyVersion}`);
      }
      await ctx.stub.deleteState(ctx.stub.createCompositeKey(INDEX_ID_HASH, [report.idHash, report.reportId]));
      report.idHash = item.newIdHash;
      report.keyVersion = version;
      report.updatedAt = now;
      await this.save(ctx, report);
      await ctx.stub.putState(ctx.stub.createCompositeKey(INDEX_ID_HASH, [report.idHash, report.reportId]), INDEX_VALUE);
    }
    return toJson({ rehashed: items.length, keyVersion: version });
  }

  // ---------- audit anchors ----------

  @Transaction()
  @Returns('string')
  public async anchorAuditRoot(ctx: Context, date: string, rootHash: string): Promise<string> {
    const org = this.requireBank(ctx);
    if (!ISO_DATE.test(date)) throw new Error('date must be YYYY-MM-DD');
    hex64('rootHash', rootHash);
    const key = ctx.stub.createCompositeKey(ANCHOR_KEY, [org, date]);
    const existing = await ctx.stub.getState(key);
    if (existing && existing.length > 0) {
      throw new Error(`${org} already anchored a root for ${date}; anchors cannot be overwritten`);
    }
    const anchor: AuditAnchor = { docType: 'anchor', org, date, rootHash, txTime: txTime(ctx) };
    await ctx.stub.putState(key, toBytes(anchor));
    return toJson(anchor);
  }

  @Transaction(false)
  @Returns('string')
  public async getAnchor(ctx: Context, org: string, date: string): Promise<string> {
    this.requireKnownOrg(ctx);
    oneOf<string>('org', org, BANK_MSPS);
    const data = await ctx.stub.getState(ctx.stub.createCompositeKey(ANCHOR_KEY, [org, date]));
    if (!data || data.length === 0) throw new Error(`No anchor for ${org} on ${date}`);
    return Buffer.from(data).toString('utf8');
  }

  // ---------- internals ----------

  private requireBank(ctx: Context): string {
    const msp = ctx.clientIdentity.getMSPID();
    if (!BANK_MSPS.includes(msp)) throw new Error(`Only member banks can do this; caller is ${msp}`);
    return msp;
  }

  private requireRegulator(ctx: Context): string {
    const msp = ctx.clientIdentity.getMSPID();
    if (msp !== REGULATOR_MSP) throw new Error(`Only the Regulator can do this; caller is ${msp}`);
    return msp;
  }

  private requireKnownOrg(ctx: Context): string {
    const msp = ctx.clientIdentity.getMSPID();
    if (!ALL_MSPS.includes(msp)) throw new Error(`Unknown organisation ${msp}`);
    return msp;
  }

  private async load(ctx: Context, reportId: string): Promise<MuleReport> {
    const data = await ctx.stub.getState(reportKey(reportId));
    if (!data || data.length === 0) throw new Error(`Report ${reportId} does not exist`);
    return JSON.parse(Buffer.from(data).toString('utf8')) as MuleReport;
  }

  private async save(ctx: Context, report: MuleReport): Promise<void> {
    await ctx.stub.putState(reportKey(report.reportId), toBytes(report));
  }

  private async reportsForIdHash(ctx: Context, idHash: string): Promise<MuleReport[]> {
    const iterator = await ctx.stub.getStateByPartialCompositeKey(INDEX_ID_HASH, [idHash]);
    const out: MuleReport[] = [];
    try {
      let res = await iterator.next();
      while (!res.done) {
        const { attributes } = ctx.stub.splitCompositeKey(res.value.key);
        const data = await ctx.stub.getState(reportKey(attributes[1]));
        if (data && data.length > 0) out.push(JSON.parse(Buffer.from(data).toString('utf8')) as MuleReport);
        res = await iterator.next();
      }
    } finally {
      await iterator.close();
    }
    return out;
  }

  private async queryAllReports(ctx: Context): Promise<MuleReport[]> {
    const iterator = await ctx.stub.getQueryResult(toJson({ selector: { docType: 'report' } }));
    const out: MuleReport[] = [];
    try {
      let res = await iterator.next();
      while (!res.done) {
        out.push(JSON.parse(Buffer.from(res.value.value).toString('utf8')) as MuleReport);
        res = await iterator.next();
      }
    } finally {
      await iterator.close();
    }
    return out;
  }
}
