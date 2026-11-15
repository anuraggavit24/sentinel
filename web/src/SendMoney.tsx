import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  explain,
  getDemoFri,
  getSignals,
  listPayments,
  putSignals,
  scorePayment,
  type Band,
  type Decision,
  type DemoSignals,
  type PayeeType,
  type PaymentRow,
} from "./api";

const BAND_LABEL: Record<Band, string> = {
  allow: "Allowed",
  step_up: "Extra check needed",
  sandbox: "Held: suspected account takeover",
};

const BAND_HELP: Record<Band, string> = {
  allow: "Low risk. This payment would go through.",
  step_up:
    "Medium risk. Module 3 will ask you to confirm with your passkey, show a warning, and may add a cooling-off delay.",
  sandbox:
    "High-confidence takeover signals. Module 3 will show the session a fake balance so no real money moves, and alert fraud ops.",
};

const NO_SIGNALS: DemoSignals = {
  simSwapHoursAgo: null,
  newDevice: false,
  integrity: "PASS",
  activeCall: false,
  screenSharing: false,
  accessibilityServices: false,
};

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
  const [signals, setSignals] = useState<DemoSignals>(NO_SIGNALS);
  const [demoAvailable, setDemoAvailable] = useState(true);
  const [friNumbers, setFriNumbers] = useState<Record<string, string>>({});

  const refreshHistory = useCallback(async () => {
    try {
      setHistory((await listPayments()).payments);
    } catch (err) {
      setError(explain(err));
    }
  }, []);

  useEffect(() => {
    void refreshHistory();
    getSignals()
      .then(setSignals)
      .catch(() => setDemoAvailable(false));
    getDemoFri()
      .then((r) => setFriNumbers(r.numbers))
      .catch(() => undefined);
  }, [refreshHistory]);

  async function updateSignals(next: DemoSignals) {
    setSignals(next);
    try {
      setSignals(await putSignals(next));
    } catch (err) {
      setError(explain(err));
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const payee: Record<string, string> =
      payeeType === "UPI" ? { upi } : payeeType === "PHONE" ? { phone } : { ifsc, account };
    try {
      setDecision(await scorePayment(payeeType, payee, amount));
      await refreshHistory();
    } catch (err) {
      setDecision(null);
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <section>
        <h2>Send money</h2>
        <form onSubmit={onSubmit}>
          <div className="field">
            <label htmlFor="payee-type">Pay to</label>
            <select id="payee-type" value={payeeType} onChange={(e) => setPayeeType(e.target.value as PayeeType)}>
              <option value="UPI">UPI ID</option>
              <option value="PHONE">Mobile number</option>
              <option value="ACCOUNT">Bank account (IFSC + account number)</option>
            </select>
          </div>
          {payeeType === "UPI" && (
            <label>
              UPI ID
              <input value={upi} onChange={(e) => setUpi(e.target.value)} placeholder="name@okhdfc" required />
            </label>
          )}
          {payeeType === "PHONE" && (
            <label>
              Mobile number
              <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+91 98765 43210" required />
            </label>
          )}
          {payeeType === "ACCOUNT" && (
            <div className="two">
              <label>
                IFSC
                <input value={ifsc} onChange={(e) => setIfsc(e.target.value)} placeholder="HDFC0001234" required />
              </label>
              <label>
                Account number
                <input value={account} onChange={(e) => setAccount(e.target.value)} placeholder="50100012345678" required />
              </label>
            </div>
          )}
          <label>
            Amount (₹)
            <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="2500" required />
          </label>
          <button className="primary" disabled={busy} type="submit">
            {busy ? "Checking…" : "Check and send"}
          </button>
        </form>
        {Object.keys(friNumbers).length > 0 && (
          <p className="muted small">
            FRI is mocked (live access is only for regulated entities). Demo risky numbers:{" "}
            {Object.entries(friNumbers)
              .map(([n, c]) => `${n} (${c.replace("_", " ").toLowerCase()})`)
              .join(", ")}
            .
          </p>
        )}
      </section>

      {error && <p className="error">{error}</p>}

      {decision && (
        <section className={`result band-${decision.band}`} aria-live="polite">
          <div className="row">
            <div>
              <h2>{BAND_LABEL[decision.band]}</h2>
              <p className="muted">
                {decision.amount} to {decision.payee}
              </p>
            </div>
            <div className="score" data-testid="score">
              {decision.score}
              <span>/100</span>
            </div>
          </div>
          <div className="meter">
            <div style={{ width: `${decision.score}%` }} />
          </div>
          <p>{BAND_HELP[decision.band]}</p>
          {decision.reasons.length === 0 ? (
            <p className="muted">No risk signals.</p>
          ) : (
            <ul className="reasons">
              {decision.reasons.map((r) => (
                <li key={r.code}>
                  <span className="pts">+{r.points}</span> {r.text} <span className="tag">{r.group.toLowerCase()}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="muted small">
            Engine: {decision.engine} (explainable rules; ML arrives in slice 2b). Consortium ledger: {decision.signals.consortium}.
            {decision.signals.injected && " Some signals were injected for this demo."}
          </p>
        </section>
      )}

      {demoAvailable && (
        <section className="demo">
          <h2>Demo signals</h2>
          <p className="muted small">
            A browser cannot read SIM, call or screen-sharing state, so these stand in for the mobile app's signals. They apply
            to this session only and are recorded in the audit log.
          </p>
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
        </section>
      )}

      <section>
        <h2>Recent payments</h2>
        {history.length === 0 ? (
          <p className="muted">None yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Payee</th>
                <th>Amount</th>
                <th>Score</th>
                <th>Decision</th>
              </tr>
            </thead>
            <tbody>
              {history.map((p) => (
                <tr key={p.id}>
                  <td>{new Date(p.createdAt).toLocaleTimeString()}</td>
                  <td>{p.payee}</td>
                  <td>{p.amount}</td>
                  <td>{p.score}</td>
                  <td>
                    <span className={`pill band-${p.band}`}>{BAND_LABEL[p.band]}</span>
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
