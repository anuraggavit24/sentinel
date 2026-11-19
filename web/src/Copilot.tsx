import { Fragment, useState } from "react";
import { askCopilot, explain, type CopilotAnswer } from "./api";

const SUGGESTED = [
  "Why was this payment held?",
  "What does the ledger say about this payee?",
  "What does policy say I should do?",
  "Which scam pattern does this look like?",
];

/** Turns "text [2] more [1, 3]" into text with clickable citation chips. */
function Cited({ text, onCite }: { text: string; onCite: (n: number) => void }) {
  const parts = text.split(/(\[\d+(?:,\s*\d+)*\])/g);
  return (
    <>
      {parts.map((p, i) => {
        const m = p.match(/^\[(.*)\]$/);
        if (!m) return <Fragment key={i}>{p}</Fragment>;
        return (
          <Fragment key={i}>
            {m[1].split(/,\s*/).map((n) => (
              <button key={n} className="cite" type="button" onClick={() => onCite(Number(n))}>
                {n}
              </button>
            ))}
          </Fragment>
        );
      })}
    </>
  );
}

/**
 * Module 4: ask about one held payment. Read-only: it explains from the case, the ledger, earlier
 * decisions on the payee and the policy playbook, and cannot release, reject or report anything.
 */
export default function Copilot({ paymentId }: { paymentId: string }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<CopilotAnswer | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ask(q: string) {
    if (q.trim().length < 3) return;
    setQuestion(q);
    setBusy(true);
    setError(null);
    setOpen(null);
    try {
      setAnswer(await askCopilot(paymentId, q.trim()));
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="copilot" data-testid="copilot">
      <div className="row wrap">
        {SUGGESTED.map((q) => (
          <button key={q} type="button" className="chip" disabled={busy} onClick={() => void ask(q)}>
            {q}
          </button>
        ))}
      </div>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
      >
        <input
          aria-label="Ask the copilot"
          value={question}
          maxLength={500}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about this case, e.g. has this payee been reported before?"
        />
        <button className="primary" type="submit" disabled={busy || question.trim().length < 3}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {answer && (
        <div className="copilot-answer" data-testid="copilot-answer">
          <p className="muted small">
            {answer.mode === "llm" ? "Written by the language model from the sources below" : "Extractive mode: quoted from the sources below (no language model)"}
            {answer.unverified && <span className="pill bad">no citations: unverified</span>}
          </p>
          <p className="answer-text">
            {answer.answer.split("\n").map((line, i) => (
              <Fragment key={i}>
                <Cited text={line} onCite={setOpen} />
                <br />
              </Fragment>
            ))}
          </p>
          {answer.warnings.map((w) => (
            <p key={w} className="pill warn" data-testid="copilot-warning">
              {w}
            </p>
          ))}
          <ol className="sources">
            {answer.sources.map((s) => (
              <li key={s.n} className={s.cited ? "cited" : ""}>
                <button className="link" type="button" onClick={() => setOpen(open === s.n ? null : s.n)}>
                  [{s.n}] {s.title}
                </button>{" "}
                <span className="tag">{s.kind}</span>
                {open === s.n && <p className="small source-text" data-testid="source-text">{s.text}</p>}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
