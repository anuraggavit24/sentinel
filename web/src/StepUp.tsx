import { useState } from "react";
import { cancelHeldPayment, confirmHeldPayment, explain, type StepUpAnswers } from "./api";

type Answer = "yes" | "no" | "";

const QUESTIONS: { key: keyof StepUpAnswers; text: string }[] = [
  { key: "onCall", text: "Is someone on a phone or video call asking you to make this payment?" },
  { key: "promised", text: "Were you promised a refund, prize, job or investment return, or told your account is at risk?" },
  { key: "knowsPayee", text: "Do you know this payee personally, or have you paid them before?" },
];

/**
 * Module 3b: the customer's own check on a held payment. Honest answers and a passkey release it;
 * scam answers keep it held for the fraud team. The server decides; answers can only make it stricter.
 */
export default function StepUp({ paymentId, onDone }: { paymentId: string; onDone: () => void }) {
  const [answers, setAnswers] = useState<Record<keyof StepUpAnswers, Answer>>({ onCall: "", promised: "", knowsPayee: "" });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string; warning?: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const complete = Object.values(answers).every((a) => a !== "");

  async function run(fn: () => Promise<{ status: string; message: string; warning?: string | null }>) {
    setBusy(true);
    setError(null);
    try {
      const r = await fn();
      setResult({ ok: r.status === "completed", message: r.message, warning: r.warning });
      onDone();
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }

  if (result)
    return (
      <div className="stepup" data-testid="stepup-result">
        {result.warning && <p className="error"><strong>{result.warning}</strong></p>}
        <p className={result.ok ? "notice" : "muted"}>{result.message}</p>
      </div>
    );

  return (
    <div className="stepup" data-testid="stepup">
      <h3>Is this payment really yours?</h3>
      {QUESTIONS.map((q) => (
        <fieldset key={q.key}>
          <legend>{q.text}</legend>
          {(["yes", "no"] as const).map((v) => (
            <label key={v} className="inline">
              <input
                type="radio"
                name={`${paymentId}-${q.key}`}
                value={v}
                checked={answers[q.key] === v}
                onChange={() => setAnswers({ ...answers, [q.key]: v })}
              />{" "}
              {v === "yes" ? "Yes" : "No"}
            </label>
          ))}
        </fieldset>
      ))}
      <p className="muted small">You can answer once. We will never call you to ask for these answers, a code or a password.</p>
      <div className="row wrap">
        <button type="button" disabled={busy} onClick={() => void run(() => cancelHeldPayment(paymentId))}>
          Cancel payment
        </button>
        <button
          className="primary"
          type="button"
          disabled={busy || !complete}
          onClick={() =>
            void run(() =>
              confirmHeldPayment(paymentId, {
                onCall: answers.onCall === "yes",
                promised: answers.promised === "yes",
                knowsPayee: answers.knowsPayee === "yes",
              }),
            )
          }
        >
          Confirm with passkey
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
