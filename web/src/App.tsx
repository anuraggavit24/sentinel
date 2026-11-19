import { useCallback, useEffect, useState, type FormEvent } from "react";
import { IsoBar, Mark } from "./Iso";
import Ledger from "./Ledger";
import Ops from "./Ops";
import SendMoney from "./SendMoney";
import {
  addDevice,
  explain,
  getMe,
  registerWithPasskey,
  removeDevice,
  revokeSession,
  signInWithPasskey,
  signOut,
  ApiError,
  type Me,
} from "./api";

type Mode = "signin" | "register";

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "never";
}

function browserName(ua: string | null): string {
  if (!ua) return "Unknown browser";
  if (ua.includes("Edg/")) return "Edge";
  if (ua.includes("Chrome/")) return "Chrome";
  if (ua.includes("Firefox/")) return "Firefox";
  if (ua.includes("Safari/")) return "Safari";
  return ua.slice(0, 40);
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<Mode>("signin");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [page, setPage] = useState<"account" | "send" | "ops" | "ledger">("send");
  const [traceTx, setTraceTx] = useState<string | null>(null);

  // "See it on the ledger" links anywhere in the app open the Ledger page on that transaction.
  useEffect(() => {
    const onTrace = (e: Event) => {
      setTraceTx((e as CustomEvent<string>).detail);
      setPage("ledger");
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
    window.addEventListener("sentinel:trace", onTrace);
    return () => window.removeEventListener("sentinel:trace", onTrace);
  }, []);

  const refresh = useCallback(async () => {
    try {
      setMe(await getMe());
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setMe(null);
      else setError(explain(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function run(action: () => Promise<unknown>, success?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (success) setNotice(success);
      await refresh();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const user = username.trim().toLowerCase();
    if (mode === "register") {
      void run(async () => {
        await registerWithPasskey(user, displayName.trim() || user, "This device");
        setMode("signin"); // next time they are signed out, show sign-in, not sign-up
      }, "Account created. You are signed in.");
    } else {
      void run(() => signInWithPasskey(user));
    }
  }

  if (loading) return <main className="shell"><p className="muted" style={{ paddingTop: 60 }}>Loading…</p></main>;

  if (!me) {
    return (
      <main className="shell">
        <nav className="nav">
          <div className="brand">
            <Mark />
            <div>
              Sentinel<small>CONSORTIUM FRAUD LEDGER</small>
            </div>
          </div>
          <span className="muted small">3 banks · 1 regulator · Hyperledger Fabric</span>
        </nav>

        <div className="hero">
          <div>
            <h1 className="display">
              We stop <span className="hl">mule accounts</span> before the <span className="hl">money moves.</span>
            </h1>
            <p className="lead">
              Banks share fraud reports on a permissioned blockchain. Every payment asks the ledger first: a payee two banks
              have confirmed as a mule is held, whatever the customer is told to do.
            </p>
          </div>
          <div className="card" style={{ width: "100%", position: "relative", zIndex: 1 }}>
            <h2>Sentinel Bank</h2>
            <p className="muted">No passwords and no OTPs. You sign in with a passkey: your fingerprint, face or device PIN.</p>
            <div className="tabs">
              <button className={mode === "signin" ? "tab active" : "tab"} onClick={() => setMode("signin")} type="button">
                Sign in
              </button>
              <button className={mode === "register" ? "tab active" : "tab"} onClick={() => setMode("register")} type="button">
                Create account
              </button>
            </div>
            <form onSubmit={onSubmit}>
              <label>
                Username
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username webauthn"
                  placeholder="e.g. anurag"
                  pattern="[a-z0-9_.\-]{3,32}"
                  title="3 to 32 characters: lowercase letters, numbers, dot, dash or underscore"
                  required
                />
              </label>
              {mode === "register" && (
                <label>
                  Display name
                  <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Anurag Gavit" />
                </label>
              )}
              <button className="primary" disabled={busy} type="submit">
                {busy ? "Waiting for your passkey…" : mode === "register" ? "Create account with a passkey" : "Sign in with a passkey"}
              </button>
            </form>
            {error && <p className="error">{error}</p>}
            {notice && <p className="notice">{notice}</p>}
          </div>
          <IsoBar length={150} size={40} style={{ right: -40, top: -20, opacity: 0.9 }} />
        </div>

        <section style={{ position: "relative", marginTop: 72 }}>
          <IsoBar length={220} size={56} style={{ left: -60, top: -30 }} />
          <h2 className="display" style={{ fontSize: "clamp(28px, 3.6vw, 44px)", marginLeft: "26%", maxWidth: 760 }}>
            We <span className="hl">share what one bank knows</span> with every bank, <span className="hl">at the moment of payment.</span>
          </h2>
        </section>

        <section className="grid-4" style={{ marginTop: 56 }}>
          <div className="stat"><b>3+1</b><span>banks and a regulator on one channel</span></div>
          <div className="stat fill"><b>2 of 3</b><span>banks must sign every write</span></div>
          <div className="stat"><b>~45 ms</b><span>ledger check on every payment (live)</span></div>
          <div className="stat fill"><b>0</b><span>account numbers on the ledger: keyed hashes only</span></div>
        </section>

        <section style={{ marginTop: 72 }}>
          <h2 className="display" style={{ fontSize: "clamp(36px, 6vw, 84px)" }}>
            <span className="grad">How it</span>
            <br />
            <span className="grad">works</span>
          </h2>
          <ol className="steps" style={{ marginTop: 28 }}>
            <li><strong>Hash</strong><span>The payee is normalised and keyed-hashed. The ledger never sees the account.</span></li>
            <li><strong>Ask</strong><span>The risk engine asks Bank A's peer what the consortium knows.</span></li>
            <li><strong>Weigh</strong><span>The chaincode weighs reports: one bank 0.25, two banks CONFIRMED.</span></li>
            <li><strong>Decide</strong><span>Confirmed mule: always held. Coached customer: step-up. Takeover: sandbox.</span></li>
            <li><strong>Share</strong><span>Analysts report back, endorsed by two banks, on every bank's copy.</span></li>
          </ol>
        </section>

        <section className="cta demo-cta" style={{ marginTop: 56 }}>
          <h2 className="display" style={{ fontSize: "clamp(32px, 5vw, 64px)" }}>
            Watch three banks
            <br />
            <span className="hl">stop one scammer</span>
          </h2>
          <div>
            <p className="muted" style={{ maxWidth: 380 }}>
              One-click customers at Bank A, B and C. Report, confirm, block. Private payment data, public hashes, every signature checked.
            </p>
            <a className="button-link" href="#/demo">
              Open the bank network demo →
            </a>
          </div>
        </section>

        <section className="chips" style={{ marginTop: 40 }}>
          {["Passkeys", "Risk engine", "Typing rhythm", "Consortium ledger", "Step-up", "Sandbox", "Fraud ops", "Copilot"].map((c) => (
            <span key={c}>{c}</span>
          ))}
        </section>

        <section className="cta">
          <h2 className="display" style={{ fontSize: "clamp(32px, 5vw, 64px)" }}>
            Help us measure
            <br />
            it on real people?
          </h2>
          <p className="small muted">
            <a href="#/study">Volunteer for the typing study</a> (no account needed)
          </p>
        </section>

        <footer className="footer">
          <span>Sentinel · Blockchain Technology project · Anurag Namdeo Gavit</span>
          <span>Demo bank, demo money. Fabric 2.5 · FastAPI · React</span>
        </footer>
      </main>
    );
  }

  const tab = (id: typeof page, label: string) => (
    <button
      className={page === id ? "tab active" : "tab"}
      onClick={() => {
        setPage(id);
        setNotice(null);
        setError(null);
      }}
      type="button"
    >
      {label}
    </button>
  );

  return (
    <main className="shell">
      <nav className="nav">
        <div className="brand">
          <Mark />
          <div>
            Sentinel<small>BANK · CONSORTIUM MEMBER (BANK A)</small>
          </div>
        </div>
        <div className="nav-links">
          <a className="tab" href="#/demo" style={{ textDecoration: "none" }}>Bank network demo</a>
          {tab("send", "Send money")}
          {tab("ledger", "Ledger")}
          {tab("account", "Passkeys and sessions")}
          {me.role === "analyst" && tab("ops", "Fraud ops")}
        </div>
        <button className="white" disabled={busy} onClick={() => void run(signOut)} type="button">
          Sign out
        </button>
      </nav>

      <header style={{ marginBottom: 28 }}>
        <h1>
          Hello, <span className="hl">{me.displayName}</span>
        </h1>
        <p className="muted">
          Signed in as {me.username}
          {me.role === "analyst" && <span className="badge">analyst</span>}
        </p>
      </header>

      {page === "send" && <SendMoney />}
      {page === "ops" && me.role === "analyst" && <Ops />}
      {page === "ledger" && <Ledger analyst={me.role === "analyst"} traceTx={traceTx} />}

      {page === "account" && (
      <>
      <section>
        <div className="row">
          <h2>Your passkeys</h2>
          <button
            disabled={busy}
            onClick={() => {
              const label = window.prompt("Name this device (e.g. 'My phone')", "Another device");
              if (label !== null) void run(() => addDevice(label), "Passkey added.");
            }}
            type="button"
          >
            Add a device
          </button>
        </div>
        <ul className="list">
          {me.devices.map((d) => (
            <li key={d.id} className="row">
              <div>
                <strong>{d.label}</strong>
                <span className="muted">
                  {" "}· {d.synced ? "synced passkey" : "this device only"} · added {when(d.createdAt)} · last used {when(d.lastUsedAt)}
                </span>
              </div>
              <button
                disabled={busy || me.devices.length <= 1}
                title={me.devices.length <= 1 ? "Add another device before removing your only passkey" : ""}
                onClick={() => {
                  if (window.confirm(`Remove "${d.label}"? It will no longer be able to sign in.`)) {
                    void run(() => removeDevice(d.id), "Passkey removed.");
                  }
                }}
                type="button"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2>Signed-in sessions</h2>
        <ul className="list">
          {me.sessions.map((s) => (
            <li key={s.id} className="row">
              <div>
                <strong>{browserName(s.userAgent)}</strong>
                {s.current && <span className="badge">this session</span>}
                <span className="muted">
                  {" "}· {s.ip ?? "unknown IP"} · since {when(s.createdAt)}
                </span>
              </div>
              <button
                disabled={busy}
                onClick={() => void run(() => revokeSession(s.id), s.current ? undefined : "Session signed out.")}
                type="button"
              >
                {s.current ? "Sign out here" : "Sign out"}
              </button>
            </li>
          ))}
        </ul>
      </section>

      </>
      )}

      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}
    </main>
  );
}
