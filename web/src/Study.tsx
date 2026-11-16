import { useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  explain,
  getStudyProtocol,
  getStudyStatus,
  joinStudy,
  sendStudySample,
  withdrawFromStudy,
  type StudyProtocol,
  type StudyStatus,
} from "./api";
import { useBehaviour } from "./behaviour";

type View = "welcome" | "consent" | "code" | "typing" | "paused" | "withdrawn";

/**
 * Volunteer typing study: real people, fixed protocol, timing only. Runs without a bank account.
 * Built for a shared laptop: nothing is stored in the browser, and "Finish" wipes everything before the
 * next volunteer sits down.
 */
export default function Study() {
  const [protocol, setProtocol] = useState<StudyProtocol | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [view, setView] = useState<View>("welcome");
  const [invite, setInvite] = useState("");
  const [returning, setReturning] = useState("");
  const [keyboard, setKeyboard] = useState("laptop");
  const [agreed, setAgreed] = useState(false);
  const [wroteDown, setWroteDown] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const [status, setStatus] = useState<StudyStatus | null>(null);
  const [upi, setUpi] = useState("");
  const [amount, setAmount] = useState("");
  const [deleted, setDeleted] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const behaviour = useBehaviour();

  useEffect(() => {
    getStudyProtocol()
      .then(setProtocol)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) setUnavailable(true);
        else setError(explain(err));
      });
  }, []);

  function show(next: StudyStatus) {
    setStatus(next);
    setView(next.next.allowed ? "typing" : "paused");
    behaviour.reset();
    setUpi("");
    setAmount("");
  }

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  function finish() {
    // Next volunteer: forget everything about this one.
    setView("welcome");
    setCode(null);
    setStatus(null);
    setInvite("");
    setReturning("");
    setAgreed(false);
    setWroteDown(false);
    setKeyboard("laptop");
    setError(null);
    behaviour.reset();
  }

  async function submitSample(e: FormEvent) {
    e.preventDefault();
    if (!protocol || !status || !code || status.next.promptNo === null) return;
    const prompt = protocol.prompts[status.next.promptNo];
    if (upi.trim() !== prompt.upi || amount.trim() !== prompt.amount) {
      setError("Type the sample exactly as shown.");
      return;
    }
    const snap = behaviour.snapshot();
    await act(async () => {
      try {
        show(await sendStudySample(code, status.next.promptNo!, snap));
      } catch (err) {
        behaviour.reset();
        setUpi("");
        setAmount("");
        throw err;
      }
    });
  }

  async function withdraw() {
    if (!code || !window.confirm("Withdraw from the study? All your samples are deleted now. This cannot be undone.")) return;
    await act(async () => {
      const r = await withdrawFromStudy(code);
      setDeleted(r.samplesDeleted);
      setCode(null);
      setStatus(null);
      setView("withdrawn");
    });
  }

  if (unavailable)
    return (
      <main className="card">
        <h1>Typing study</h1>
        <p className="muted">The typing study is not running on this server.</p>
        <a href="#/">Back to Sentinel Bank</a>
      </main>
    );
  if (!protocol) return <main className="card">{error ? <p className="error">{error}</p> : "Loading…"}</main>;

  const prompt = status?.next.promptNo != null ? protocol.prompts[status.next.promptNo] : null;
  const done = status?.sessions.reduce((a, s) => a + s.samples, 0) ?? 0;

  return (
    <main className="card" data-testid="study">
      <h1>Typing study</h1>

      {view === "welcome" && (
        <>
          <p className="muted">
            Help measure whether typing rhythm can tell people apart. About 5 minutes per session,{" "}
            {protocol.minSessions} sessions at least {protocol.sessionGapHours} hours apart.
          </p>
          <form onSubmit={(e) => { e.preventDefault(); setView("consent"); }}>
            <label>
              New volunteer: invite code
              <input value={invite} onChange={(e) => setInvite(e.target.value)} placeholder="XXXX-XXXX-XXXX" autoComplete="off" required />
            </label>
            <button className="primary" type="submit">Continue</button>
          </form>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void act(async () => {
                const c = returning.trim();
                const s = await getStudyStatus(c);
                setCode(c);
                show(s);
              });
            }}
          >
            <label>
              Returning volunteer: your participant code
              <input value={returning} onChange={(e) => setReturning(e.target.value)} placeholder="XXXX-XXXX-XXXX" autoComplete="off" required />
            </label>
            <button disabled={busy} type="submit">Resume</button>
          </form>
          <p className="small"><a href="#/">Back to Sentinel Bank</a></p>
        </>
      )}

      {view === "consent" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              const r = await joinStudy(invite.trim(), protocol.consentVersion, keyboard);
              setCode(r.participantCode);
              setView("code");
            });
          }}
        >
          <h2>Before you start</h2>
          <ul className="consent">
            {protocol.consent.map((line) => <li key={line}>{line}</li>)}
          </ul>
          <label>
            What are you typing on?
            <select value={keyboard} onChange={(e) => setKeyboard(e.target.value)}>
              <option value="laptop">Laptop keyboard</option>
              <option value="external">External keyboard</option>
              <option value="phone">Phone</option>
            </select>
          </label>
          <label className="inline check">
            <input type="checkbox" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} /> I have read this and agree to take part
          </label>
          <div className="row">
            <button type="button" onClick={finish}>Cancel</button>
            <button className="primary" disabled={!agreed || busy} type="submit">Join the study</button>
          </div>
        </form>
      )}

      {view === "code" && code && (
        <>
          <h2>Your participant code</h2>
          <p className="code-big" data-testid="participant-code">{code}</p>
          <p className="small">
            Write it down or take a photo. You need it for your next session and to withdraw. It is not saved on
            this computer and nobody can look it up for you.
          </p>
          <label className="inline check">
            <input type="checkbox" checked={wroteDown} onChange={(e) => setWroteDown(e.target.checked)} /> I have saved my code
          </label>
          <button
            className="primary"
            disabled={!wroteDown || busy}
            type="button"
            onClick={() => void act(async () => show(await getStudyStatus(code)))}
          >
            Start typing
          </button>
        </>
      )}

      {view === "typing" && status && prompt && (
        <form onSubmit={submitSample} {...behaviour.handlers} className="practice">
          <p className="small" data-testid="study-progress">
            Session {status.next.session} · sample {(status.next.promptNo ?? 0) + 1} of {protocol.samplesPerSession}. Type by hand at your
            normal speed: <code>{prompt.upi}</code> and <code>{prompt.amount}</code>
          </p>
          <div className="two">
            <label>
              Study UPI ID
              <input value={upi} onChange={(e) => setUpi(e.target.value)} autoComplete="off" onPaste={(e) => e.preventDefault()} autoFocus />
            </label>
            <label>
              Study amount
              <input value={amount} onChange={(e) => setAmount(e.target.value)} autoComplete="off" onPaste={(e) => e.preventDefault()} />
            </label>
          </div>
          <div className="row">
            <button type="button" disabled={busy} onClick={() => void withdraw()}>Withdraw</button>
            <button className="primary" disabled={busy} type="submit">Save sample</button>
          </div>
        </form>
      )}

      {view === "paused" && status && (
        <section data-testid="study-paused">
          {status.next.finished ? (
            <p className="notice">All sessions done ({done} samples). Thank you!</p>
          ) : (
            <p className="notice">
              Session {status.sessions.length} saved ({done} samples so far). Your next session opens{" "}
              <strong>{status.next.opensAt ? new Date(status.next.opensAt).toLocaleString() : "now"}</strong>. Bring your participant code.
            </p>
          )}
          <div className="row">
            <button type="button" disabled={busy} onClick={() => void withdraw()}>Withdraw</button>
            <button className="primary" type="button" onClick={finish}>Finish (next volunteer)</button>
          </div>
        </section>
      )}

      {view === "withdrawn" && (
        <>
          <p className="notice">You have left the study. {deleted} samples were deleted.</p>
          <button className="primary" type="button" onClick={finish}>Done</button>
        </>
      )}

      {error && <p className="error">{error}</p>}
    </main>
  );
}
