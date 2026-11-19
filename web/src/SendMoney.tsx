import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  explain,
  getAccount,
  getDemoFri,
  getMe,
  getSignals,
  listPayments,
  putSignals,
  scorePayment,
  type Band,
  type Decision,
  traceOnLedger,
  type ConsortiumView,
  type DemoSignals,
  type PayeeType,
  type PaymentRow,
} from "./api";
import { useBehaviour } from "./behaviour";
import BehaviourProfile from "./BehaviourProfile";
import StepUp from "./StepUp";

const BAND_LABEL: Record<Band, string> = {
  allow: "Allowed",
  step_up: "Extra check needed",
  sandbox: "Held: suspected account takeover",
};

const BAND_HELP: Record<Band, string> = {
  allow: "Low risk. Payment sent.",
  step_up:
    "Before this payment goes through we need to check it with you. If someone on a call asked you to make it, stop: banks and police never ask you to move money.",
  sandbox: "This session shows signs of someone else using your account. The payment has been held.",
};

const REVIEW_LABEL: Record<NonNullable<PaymentRow["reviewed"]>, string> = {
  release: "Released after review",
  reject: "Stopped after review",
  customer_confirmed: "Confirmed by you",
  customer_cancelled: "Cancelled by you",
};

const NO_SIGNALS: DemoSignals = {
  simSwapHoursAgo: null,
  newDevice: false,
  integrity: "PASS",
  activeCall: false,
  screenSharing: false,
  accessibilityServices: false,
};

function newKey(): string {
  return crypto.randomUUID();
}

export default function SendMoney() {
  const [payeeType, setPayeeType] = useState<PayeeType>("UPI");
  const [upi, setUpi] = useState("");
  const [phone, setPhone] = useState("");
  const [ifsc, setIfsc] = useState("");
  const [account, setAccount] = useState("");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [history, setHistory] = useState<PaymentRow[]>([]);
  const [balance, setBalance] = useState<string | null>(null);
  const [stepUpFor, setStepUpFor] = useState<string | null>(null);
  // One idempotency key per payment attempt: reused if the same payment is retried after an error,
  // replaced as soon as any field changes or a decision comes back.
  const attemptKey = useRef<string | null>(null);
  const behaviour = useBehaviour();

  // Operator console (demo only): a separate role with its own token.
  const [operatorToken, setOperatorToken] = useState("");
  const [operatorReady, setOperatorReady] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [signals, setSignals] = useState<DemoSignals>(NO_SIGNALS);
  const [friNumbers, setFriNumbers] = useState<Record<string, string>>({});
  const [operatorError, setOperatorError] = useState<string | null>(null);

  const refreshHistory = useCallback(async () => {
    try {
      const [h, a] = await Promise.all([listPayments(), getAccount()]);
      setHistory(h.payments);
      setBalance(a.balance);
    } catch (err) {
      setError(explain(err));
    }
  }, []);

  useEffect(() => {
    void refreshHistory();
  }, [refreshHistory]);

  function edited<T>(setter: (v: T) => void) {
    return (v: T) => {
      attemptKey.current = null;
      setter(v);
    };
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    attemptKey.current ??= newKey();
    const payee: Record<string, string> =
      payeeType === "UPI" ? { upi } : payeeType === "PHONE" ? { phone } : { ifsc, account };
    try {
      const d = await scorePayment(payeeType, payee, amount, attemptKey.current, behaviour.snapshot());
      setDecision(d);
      setStepUpFor(d.status === "held" && d.band === "step_up" && !d.replayed ? d.paymentId : null);
      attemptKey.current = null;
      behaviour.reset();
      await refreshHistory();
    } catch (err) {
      setDecision(null);
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  async function connectOperator(e: FormEvent) {
    e.preventDefault();
    setOperatorError(null);
    try {
      const me = await getMe();
      const current = me.sessions.find((s) => s.current);
      if (!current) throw new Error("No current session found");
      setSignals(await getSignals(current.id, operatorToken));
      setFriNumbers((await getDemoFri(operatorToken)).numbers);
      setSessionId(current.id);
      setOperatorReady(true);
    } catch (err) {
      setOperatorReady(false);
      setOperatorError(explain(err));
    }
  }

  async function updateSignals(next: DemoSignals) {
    if (!sessionId) return;
    setSignals(next);
    try {
      setSignals(await putSignals(sessionId, operatorToken, next));
    } catch (err) {
      setOperatorError(explain(err));
    }
  }

  const a = decision?.analyst;

  return (
    <div className="stack">
      <BehaviourProfile />

      <section>
        <h2>Send money</h2>
        <form onSubmit={onSubmit} {...behaviour.handlers}>
          <div className="field">
            <label htmlFor="payee-type">Pay to</label>
            <select id="payee-type" value={payeeType} onChange={(e) => edited(setPayeeType)(e.target.value as PayeeType)}>
              <option value="UPI">UPI ID</option>
              <option value="PHONE">Mobile number</option>
              <option value="ACCOUNT">Bank account (IFSC + account number)</option>
            </select>
          </div>
          {payeeType === "UPI" && (
            <label>
              UPI ID
              <input value={upi} onChange={(e) => edited(setUpi)(e.target.value)} placeholder="name@okhdfc" required />
            </label>
          )}
          {payeeType === "PHONE" && (
            <label>
              Mobile number
              <input value={phone} onChange={(e) => edited(setPhone)(e.target.value)} placeholder="+91 98765 43210" required />
            </label>
          )}
          {payeeType === "ACCOUNT" && (
            <div className="two">
              <label>
                IFSC
                <input value={ifsc} onChange={(e) => edited(setIfsc)(e.target.value)} placeholder="HDFC0001234" required />
              </label>
              <label>
                Account number
                <input value={account} onChange={(e) => edited(setAccount)(e.target.value)} placeholder="50100012345678" required />
              </label>
            </div>
          )}
          <label>
            Amount (₹)
            <input value={amount} onChange={(e) => edited(setAmount)(e.target.value)} inputMode="decimal" placeholder="2500" required />
          </label>
          <button className="primary" disabled={busy} type="submit">
            {busy ? "Checking…" : "Check and send"}
          </button>
        </form>
      </section>

      {error && <p className="error">{error}</p>}

      {decision && (
        <section className={`result band-${decision.band}`} aria-live="polite">
          <h2>{BAND_LABEL[decision.band]}</h2>
          <p className="muted">
            {decision.amount} to {decision.payee}
            {decision.replayed && " · already submitted, showing the original decision"}
          </p>
          <p>{BAND_HELP[decision.band]}</p>
          {decision.reasons.length > 0 && (
            <ul className="reasons plain">
              {decision.reasons.map((r) => (
                <li key={r.code}>{r.text}</li>
              ))}
            </ul>
          )}
          {stepUpFor === decision.paymentId && <StepUp key={decision.paymentId} paymentId={decision.paymentId} onDone={() => void refreshHistory()} />}

          {a && (
            <div className="analyst">
              <div className="row">
                <strong>Analyst view (demo only: customers never see this)</strong>
                <div className="score" data-testid="score">
                  {a.score}
                  <span>/100</span>
                </div>
              </div>
              <div className="meter">
                <div style={{ width: `${a.score}%` }} />
              </div>
              <ul className="reasons">
                {a.reasons.map((r) => (
                  <li key={r.code}>
                    <span className="pts">+{r.points}</span> {r.text} <span className="tag">{r.group.toLowerCase()}</span>
                  </li>
                ))}
              </ul>
              <p className="muted small" data-testid="behaviour-status">
                Behaviour model:{" "}
                {a.signals.behaviour.model === "learning"
                  ? `learning your typing (${a.signals.behaviour.samples} of ${a.signals.behaviour.enrolMin} clean payments)`
                  : a.signals.behaviour.model === "none"
                    ? "no typing data for this payment"
                    : `${a.signals.behaviour.model}, anomaly ${a.signals.behaviour.anomaly?.toFixed(2)} from ${a.signals.behaviour.samples} clean payments`}
              </p>
              <p className="muted small" data-testid="behaviour-this-payment">
                This payment: {a.signals.behaviour.captured ? "typing captured" : `typing not captured (${a.signals.behaviour.note})`}
                {" · "}
                {a.signals.behaviour.learned ? "added to your profile" : `not added to your profile (${a.signals.behaviour.notLearnedBecause})`}
              </p>
              {a.sandbox?.active && (
                <p className="sandbox-line" data-testid="sandbox-line">
                  SANDBOX ({a.sandbox.why}): this session is shown “Allowed” and a falling balance, but nothing was sent.
                  The payee is captured for the consortium ledger as a sandbox report.
                </p>
              )}
              <ConsortiumLine c={a.signals.consortium} />
              <p className="muted small">
                Engine: {decision.engine}.
                {a.signals.operatorSignalsOn && " Device and telecom signals were set by the demo operator."}
              </p>
            </div>
          )}
        </section>
      )}

      <section className="demo">
        <h2>Operator console (demo)</h2>
        <p className="muted small">
          A browser cannot read SIM, call or screen-sharing state, so a demo operator sets those signals for this session. It
          needs the operator token, so the session being scored can never change its own signals. Every change is recorded in
          the audit log.
        </p>
        {!operatorReady ? (
          <form className="inline-form" onSubmit={connectOperator}>
            <input
              type="password"
              aria-label="Operator token"
              placeholder="Operator token"
              value={operatorToken}
              onChange={(e) => setOperatorToken(e.target.value)}
              required
            />
            <button type="submit">Connect</button>
          </form>
        ) : (
          <>
            <div className="toggles">
              <label className="inline" htmlFor="sim-swap">
                SIM changed
              </label>
              <select
                id="sim-swap"
                value={signals.simSwapHoursAgo === null ? "" : String(signals.simSwapHoursAgo)}
                onChange={(e) =>
                  void updateSignals({ ...signals, simSwapHoursAgo: e.target.value === "" ? null : Number(e.target.value) })
                }
              >
                <option value="">not recently</option>
                <option value="2">2 hours ago</option>
                <option value="48">2 days ago</option>
                <option value="200">8 days ago</option>
              </select>
              {(
                [
                  ["newDevice", "New device"],
                  ["activeCall", "Phone call active"],
                  ["screenSharing", "Screen being shared"],
                  ["accessibilityServices", "Accessibility service on"],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="inline check">
                  <input
                    type="checkbox"
                    checked={signals[key]}
                    onChange={(e) => void updateSignals({ ...signals, [key]: e.target.checked })}
                  />
                  {label}
                </label>
              ))}
              <label className="inline check">
                <input
                  type="checkbox"
                  checked={signals.integrity === "FAIL"}
                  onChange={(e) => void updateSignals({ ...signals, integrity: e.target.checked ? "FAIL" : "PASS" })}
                />
                Integrity check failed
              </label>
            </div>
            <button type="button" onClick={() => void updateSignals(NO_SIGNALS)}>
              Reset signals
            </button>
            {Object.keys(friNumbers).length > 0 && (
              <p className="muted small">
                FRI is mocked (live access is only for regulated entities). Demo risky numbers:{" "}
                {Object.entries(friNumbers)
                  .map(([n, c]) => `${n} (${c.replace("_", " ").toLowerCase()})`)
                  .join(", ")}
                .
              </p>
            )}
          </>
        )}
        {operatorError && <p className="error">{operatorError}</p>}
      </section>

      <section>
        <div className="row">
          <h2>Recent payments</h2>
          {balance && <span className="balance" data-testid="balance">Balance {balance}</span>}
        </div>
        {stepUpFor && stepUpFor !== decision?.paymentId && <StepUp key={stepUpFor} paymentId={stepUpFor} onDone={() => void refreshHistory()} />}
        {history.length === 0 ? (
          <p className="muted">None yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Payee</th>
                <th>Amount</th>
                {history[0].score !== undefined && <th>Score</th>}
                <th>Decision</th>
              </tr>
            </thead>
            <tbody>
              {history.map((p) => (
                <tr key={p.id}>
                  <td>{new Date(p.createdAt).toLocaleTimeString()}</td>
                  <td>{p.payee}</td>
                  <td>{p.amount}</td>
                  {p.score !== undefined && <td>{p.score}</td>}
                  <td>
                    <span className={`pill band-${p.band}`}>{BAND_LABEL[p.band]}</span>
                    {p.status === "blocked_takeover" && (
                      <span className="pill bad" data-testid="blocked">Blocked: someone else was using your account</span>
                    )}
                    {p.reviewed && (
                      <span className={p.reviewed === "release" || p.reviewed === "customer_confirmed" ? "pill ok" : "pill bad"} data-testid="reviewed">
                        {REVIEW_LABEL[p.reviewed]}
                      </span>
                    )}
                    {p.status === "held" && p.band === "step_up" && !p.reviewed && !p.stepUpFlagged && stepUpFor !== p.id && (
                      <button className="link" type="button" onClick={() => setStepUpFor(p.id)}>
                        Confirm or cancel
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

/** Analyst-only line describing the consortium ledger lookup for this payment. */
function ConsortiumLine({ c }: { c: ConsortiumView }) {
  const text =
    c.status === "disabled"
      ? "not connected (LEDGER_URL not set)"
      : c.status === "unavailable"
        ? `unavailable, scored without it (${c.error})`
        : c.status === "clear"
          ? `no reports about this payee (${c.latencyMs} ms)`
          : `${c.reports?.length} report(s), max weight ${c.maxWeight?.toFixed(2)}${c.hardRule ? ", CONFIRMED" : ""} (${c.latencyMs} ms)`;
  return (
    <div className="muted small" data-testid="consortium-status">
      Consortium ledger: {text}
      {c.status === "hit" && (
        <ul className="ledger-reports">
          {c.reports?.map((r) => (
            <li key={r.reportId}>
              {r.status} · {r.severity} · reported by {r.reporterOrg} via {r.source.toLowerCase().replace(/_/g, " ")} ·{" "}
              {r.nonSandboxBanks} non-sandbox bank(s) · weight {r.weight} · <code>{r.reportId.slice(0, 12)}</code>
              <button className="link" type="button" onClick={() => traceOnLedger(r.reportId)}>
                See it on the ledger
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
