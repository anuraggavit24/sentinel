import { useCallback, useEffect, useState } from "react";
import Copilot from "./Copilot";
import {
  decidePayment,
  elevateOps,
  explain,
  getFilings,
  getOpsStatus,
  getQueue,
  getSandboxSessions,
  traceOnLedger,
  revokeSandboxSession,
  type SandboxSession,
  type Filing,
  type OpsStatus,
  type QueueItem,
} from "./api";

type Severity = "LOW" | "MEDIUM" | "HIGH";

const FILING_LABEL: Record<Filing["status"], string> = {
  pending: "Queued for the ledger",
  reported: "Reported (new report)",
  confirmed: "Confirmed another bank's report",
  already_ours: "Already reported by us",
  failed: "Failed",
};

function ago(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return mins < 1 ? "just now" : mins < 60 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`;
}

/**
 * Fraud-ops dashboard (Module 3a). Analysts only. Viewing needs the role; every decision needs a fresh
 * passkey check on this session (a few minutes), so a stolen analyst cookie cannot release money.
 */
export default function Ops() {
  const [status, setStatus] = useState<OpsStatus | null>(null);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [queueTotal, setQueueTotal] = useState(0);
  const [filings, setFilings] = useState<Filing[]>([]);
  const [sandbox, setSandbox] = useState<SandboxSession[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [severity, setSeverity] = useState<Severity>("MEDIUM");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [s, q, f, sb] = await Promise.all([getOpsStatus(), getQueue(), getFilings(), getSandboxSessions()]);
    setStatus(s);
    setQueue(q.payments);
    setQueueTotal(q.total);
    setFilings(f.filings);
    setSandbox(sb.sessions);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setError(explain(e)));
    // Filings move from "queued" to a result in the background: poll while any are pending.
    const t = window.setInterval(() => void refresh().catch(() => {}), 4000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const elevated = status?.elevatedUntil != null && new Date(status.elevatedUntil) > new Date();

  async function act(fn: () => Promise<unknown>, done?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (done) setNotice(done);
      await refresh();
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }

  function decide(item: QueueItem, decision: "release" | "reject", report: boolean) {
    const verb = report ? "Reject and report to the consortium" : decision === "release" ? "Release" : "Reject";
    void act(async () => {
      await decidePayment(item.id, { decision, note, report, severity });
      setOpen(null);
      setNote("");
    }, `${verb}: ${item.amount} to ${item.payee}.`);
  }

  return (
    <section className="ops" data-testid="ops">
      <div className="row">
        <div>
          <h2>Fraud operations</h2>
          <p className="muted small">
            {status ? `${status.bank} · ledger writes ${status.ledgerWrites ? "on" : "off"}` : "Loading…"}
          </p>
        </div>
        {elevated ? (
          <span className="pill ok" data-testid="elevated">
            Verified until {new Date(status!.elevatedUntil!).toLocaleTimeString()}
          </span>
        ) : (
          <button className="primary" disabled={busy} onClick={() => void act(elevateOps, "Verified. You can decide payments for a few minutes.")} type="button">
            Verify with passkey to decide
          </button>
        )}
      </div>

      <h3>Held payments ({queueTotal > queue.length ? `oldest ${queue.length} of ${queueTotal}` : queue.length})</h3>
      {queue.length === 0 && <p className="muted">Nothing waiting for review.</p>}
      <ul className="queue">
        {queue.map((item) => (
          <li key={item.id} className={open === item.id ? "open" : ""} data-testid="queue-item">
            <button className="queue-head" onClick={() => setOpen(open === item.id ? null : item.id)} type="button">
              <span className="score-chip">{item.score}</span>
              <span>
                <strong>{item.amount}</strong> to {item.payee} <span className="muted small">· {item.customer} · {ago(item.createdAt)}</span>
              </span>
              {item.consortium?.status === "hit" && (
                <span className={item.consortium.hardRule ? "pill bad" : "pill warn"}>
                  {item.consortium.hardRule ? "Ledger: confirmed mule" : "Ledger: reported"}
                </span>
              )}
              {item.otherCustomers7d > 0 && <span className="pill warn">{item.otherCustomers7d} other customer(s) tried this payee</span>}
              {item.coaching && <span className="pill warn">coaching signals</span>}
              {item.scamPattern && <span className="pill bad">Customer described a scam</span>}
            </button>
            {open === item.id && (
              <div className="queue-body">
                <ul className="reasons">
                  {item.reasons.map((r) => (
                    <li key={r.code}>
                      <span className="pts">+{r.points}</span> {r.text} <span className="tag">{r.group.toLowerCase()}</span>
                    </li>
                  ))}
                </ul>
                {item.stepUp && (
                  <p className="small" data-testid="stepup-answers">
                    Customer step-up: on a call <strong>{item.stepUp.answers.onCall ? "yes" : "no"}</strong> · promised something{" "}
                    <strong>{item.stepUp.answers.promised ? "yes" : "no"}</strong> · knows the payee{" "}
                    <strong>{item.stepUp.answers.knowsPayee ? "yes" : "no"}</strong>
                    {item.stepUp.blockers.length > 0 && <> · held because {item.stepUp.blockers.join("; ")}</>}
                  </p>
                )}
                <p className="muted small">
                  Payee ref <code>{item.payeeRef}</code> · ledger {item.consortium?.status ?? "not asked"}
                  {item.consortium?.reports?.length ? ` · ${item.consortium.reports.length} report(s)` : ""}
                </p>
                <label>
                  Note (kept in the case record; its hash goes on the ledger if you report)
                  <input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="e.g. customer confirmed by phone" />
                </label>
                <div className="row wrap">
                  <button disabled={busy || !elevated} onClick={() => decide(item, "release", false)} type="button">
                    Release
                  </button>
                  <button disabled={busy || !elevated} onClick={() => decide(item, "reject", false)} type="button">
                    Reject
                  </button>
                  <span className="row">
                    <select aria-label="Severity" value={severity} onChange={(e) => setSeverity(e.target.value as Severity)} disabled={!status?.ledgerWrites}>
                      <option value="LOW">Low</option>
                      <option value="MEDIUM">Medium</option>
                      <option value="HIGH">High</option>
                    </select>
                    <button
                      className="danger"
                      disabled={busy || !elevated || !status?.ledgerWrites}
                      onClick={() => decide(item, "reject", true)}
                      type="button"
                    >
                      Reject and report to consortium
                    </button>
                  </span>
                </div>
                {!elevated && <p className="muted small">Verify with your passkey (top right) to decide.</p>}
                <details className="copilot-wrap">
                  <summary>Ask the copilot about this case</summary>
                  <Copilot key={item.id} paymentId={item.id} />
                </details>
              </div>
            )}
          </li>
        ))}
      </ul>

      <h3>Sandboxed sessions ({sandbox.filter((x) => !x.revoked).length} active)</h3>
      {sandbox.length === 0 && <p className="muted">No suspected takeovers.</p>}
      <table className="filings" data-testid="sandbox">
        <tbody>
          {sandbox.map((x) => (
            <tr key={x.id} data-testid="sandbox-session">
              <td>
                {x.customer} · since {new Date(x.sandboxedAt).toLocaleTimeString()}
              </td>
              <td>
                {x.attempts} attempt(s), {x.attempted} to {x.payees} payee(s)
              </td>
              <td>
                {x.revoked ? (
                  <span className="pill">ended</span>
                ) : (
                  <button
                    disabled={busy || !elevated}
                    onClick={() => void act(() => revokeSandboxSession(x.id), "Sandboxed session ended.")}
                    type="button"
                  >
                    End session
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Ledger filings</h3>
      {filings.length === 0 && <p className="muted">No reports filed yet.</p>}
      <table className="filings" data-testid="filings">
        <tbody>
          {filings.map((f) => (
            <tr key={f.id}>
              <td>{f.amount} to {f.payee}</td>
              <td>
                <span className={f.status === "failed" ? "pill bad" : f.status === "pending" ? "pill warn" : "pill ok"} data-testid="filing-status">
                  {FILING_LABEL[f.status]}
                </span>
                {f.status === "pending" && f.attempts > 0 && <span className="muted small"> retry {f.attempts}</span>}
              </td>
              <td className="muted small">
                {f.reportId ? (
                  <>
                    <code title={f.reportId}>{f.reportId.slice(0, 12)}…</code>
                    <button className="link" type="button" onClick={() => traceOnLedger(f.reportId!)}>
                      See it on the ledger
                    </button>
                  </>
                ) : (
                  f.lastError ?? ""
                )}
              </td>
              <td className="muted small">{f.source === "SANDBOX_CAPTURE" ? "sandbox capture" : "analyst"} · {f.severity}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}
    </section>
  );
}
