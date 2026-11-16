import { useCallback, useEffect, useState, type FormEvent } from "react";
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
  const [page, setPage] = useState<"account" | "send" | "ops">("send");

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

  if (loading) return <main className="card">Loading…</main>;

  if (!me) {
    return (
      <main className="card">
        <h1>Sentinel Bank</h1>
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
        <p className="small muted">
          <a href="#/study">Volunteer for the typing study</a> (no account needed)
        </p>
      </main>
    );
  }

  return (
    <main className="card wide">
      <header className="row">
        <div>
          <h1>Hello, {me.displayName}</h1>
          <p className="muted">Signed in as {me.username}</p>
        </div>
        <button disabled={busy} onClick={() => void run(signOut)} type="button">
          Sign out
        </button>
      </header>

      <div className="tabs">
        <button className={page === "send" ? "tab active" : "tab"} onClick={() => { setPage("send"); setNotice(null); setError(null); }} type="button">
          Send money
        </button>
        <button className={page === "account" ? "tab active" : "tab"} onClick={() => { setPage("account"); setNotice(null); setError(null); }} type="button">
          Passkeys and sessions
        </button>
        {me.role === "analyst" && (
          <button className={page === "ops" ? "tab active" : "tab"} onClick={() => { setPage("ops"); setNotice(null); setError(null); }} type="button">
            Fraud ops
          </button>
        )}
      </div>

      {page === "send" && <SendMoney />}
      {page === "ops" && me.role === "analyst" && <Ops />}

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
