import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  demoAccounts,
  demoChain,
  demoKeys,
  demoLedger,
  demoLogin,
  demoLogout,
  demoMe,
  demoReport,
  demoReset,
  demoRestore,
  demoRetry,
  demoSendRaw,
  demoTamper,
  explain,
  type ChainBlock,
  type DemoAccounts,
  type DemoCheck,
  type DemoMe,
  type DemoPaymentView,
  type DemoTransfer,
  type OrgIdentity,
} from "./api";
import { Mark } from "./Iso";
import { ORG_NAME, Signatures } from "./Signatures";

const BANK_COLOR: Record<string, string> = { BankAMSP: "#1ed7c0", BankBMSP: "#ff6a3d", BankCMSP: "#ffb547", RegulatorMSP: "#a39eab" };
const VIEWERS = ["BankAMSP", "BankBMSP", "BankCMSP", "RegulatorMSP"];
const short = (h: string | null | undefined, n = 10) => (h ? `${h.slice(0, n)}…` : "");
const inr = (n: number) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
const bank = (msp: string) => ORG_NAME[msp] ?? msp;

function BankTag({ msp }: { msp: string }) {
  return (
    <span className="bank-tag" style={{ borderColor: BANK_COLOR[msp], color: BANK_COLOR[msp] }}>
      {bank(msp)}
    </span>
  );
}

// ---------------------------------------------------------------- login

function Login({ accounts, onIn }: { accounts: DemoAccounts; onIn: (me: DemoMe) => void }) {
  const [error, setError] = useState("");
  return (
    <>
      <section className="demo-hero">
        <h1 className="display">
          Three banks. <span className="hl">One shared ledger.</span>
        </h1>
        <p className="lead">
          Pick a customer at any bank. Payments between banks are recorded on Hyperledger Fabric: the details privately for the two
          banks involved, a hash for everyone. Scam reports from different banks add up, and once two banks confirm, every bank stops
          the payment.
        </p>
        <p className="sandbox-line" style={{ maxWidth: 720 }}>
          Demo only: these logins have no password or passkey. They exist only when the API runs with DEMO_LOGIN=1.
        </p>
      </section>
      {error && <p className="error">{error}</p>}
      <div className="bank-grid">
        {accounts.banks.map((b) => (
          <div key={b.bank} className="bank-card" style={{ borderTopColor: BANK_COLOR[b.bank] }} data-testid={`bank-${b.bank}`}>
            <h2 style={{ color: BANK_COLOR[b.bank] }}>{b.name}</h2>
            <p className="muted small">
              {b.bank} · its own peer, its own keys
            </p>
            {b.customers.map((c) => (
              <button
                key={c.id}
                className="login-as"
                onClick={async () => {
                  try {
                    onIn(await demoLogin(c.id));
                  } catch (err) {
                    setError(explain(err));
                  }
                }}
              >
                <b>{c.name}</b>
                <span className="mono">{c.upi}</span>
              </button>
            ))}
          </div>
        ))}
      </div>
      <Story scammer={accounts.scammer.upi} />
      {!accounts.ledgerWrites && (
        <p className="error">The API has no ledger write token: payments will move but nothing is written to Fabric.</p>
      )}
    </>
  );
}

function Story({ scammer }: { scammer: string }) {
  return (
    <div className="panel story">
      <h3>The story to click through</h3>
      <ol>
        <li>
          <b>Asha (Bank A)</b> pays <span className="mono">{scammer}</span>. The ledger knows nothing yet, so it goes through. She
          reports it: Bank A files a report, signed with Bank A's key. <span className="pill warn">PENDING</span>
        </li>
        <li>
          <b>Bilal (Bank B)</b> pays the same account. Bank B sees Bank A's report and warns him; he sends anyway, then reports it. Bank B
          confirms Bank A's report with its own key. <span className="pill bad">CONFIRMED</span>
        </li>
        <li>
          <b>Chitra (Bank C)</b> tries to pay it. Bank C asks the ledger and gets two banks' word: <b>blocked</b>, no money moves.
        </li>
      </ol>
      <p className="muted small">
        Then switch the ledger view between banks: Bank B can read its own payments, but Asha's payment to Bank C shows only a hash.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------- portal

function CheckLine({ check }: { check: DemoCheck | null }) {
  if (!check) return null;
  if (check.status === "unavailable") return <span className="muted small">ledger unavailable: checked without it</span>;
  if (check.status === "clear") return <span className="muted small">ledger: no reports for this payee</span>;
  const r = check.reports ?? [];
  return (
    <span className="small">
      ledger:{" "}
      {r.map((x) => (
        <span key={x.reportId}>
          reported by <BankTag msp={x.reporterOrg} />, {x.banks} bank{x.banks === 1 ? "" : "s"} in total,{" "}
          <span className={x.status === "CONFIRMED" ? "pill bad" : "pill warn"}>{x.status}</span>
        </span>
      ))}
    </span>
  );
}

function SendForm({ me, onDone }: { me: DemoMe; onDone: (m: DemoMe) => void }) {
  const [to, setTo] = useState(me.scammer);
  const [amount, setAmount] = useState("20000");
  const [busy, setBusy] = useState(false);
  const [warning, setWarning] = useState<{ message: string; check: DemoCheck } | null>(null);
  const [error, setError] = useState("");

  const go = async (ack: boolean) => {
    setBusy(true);
    setError("");
    try {
      const r = await demoSendRaw(to.trim(), Number(amount), ack);
      if (r.ok) {
        setWarning(null);
        onDone(r.me);
      } else if (r.status === 409 && (r.detail as { needsAck?: boolean })?.needsAck) {
        setWarning(r.detail as { message: string; check: DemoCheck });
      } else {
        setError(typeof r.detail === "string" ? r.detail : JSON.stringify(r.detail));
      }
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    go(false);
  };
  const others = ["asha@banka", "arjun@banka", "bilal@bankb", "bhavna@bankb", "chitra@bankc", "chetan@bankc"].filter((u) => u !== me.account.upi);

  return (
    <form className="panel" onSubmit={submit} data-testid="send-form">
      <h3 style={{ marginTop: 0 }}>Send money</h3>
      <div className="two">
        <label className="field">
          <span className="small muted">To (UPI ID)</span>
          <input aria-label="To UPI ID" value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
        <label className="field">
          <span className="small muted">Amount (₹)</span>
          <input aria-label="Amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
        </label>
      </div>
      <div className="chips quick" style={{ margin: "10px 0" }}>
        <button type="button" className="chip scam" onClick={() => setTo(me.scammer)}>
          scammer · {me.scammer}
        </button>
        {others.map((u) => (
          <button key={u} type="button" className="chip" onClick={() => setTo(u)}>
            {u}
          </button>
        ))}
      </div>
      <button className="primary" disabled={busy}>
        {busy ? "Asking the ledger…" : "Send"}
      </button>
      {warning && (
        <div className="warn-box" data-testid="warning">
          <p>
            <b>{warning.message}</b>
          </p>
          <CheckLine check={warning.check} />
          <div className="row wrap" style={{ marginTop: 10 }}>
            <button type="button" className="danger" onClick={() => go(true)} disabled={busy}>
              Send anyway
            </button>
            <button type="button" onClick={() => setWarning(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function LastResult({ me }: { me: DemoMe }) {
  const r = me.result;
  if (!r) return null;
  if (r.verdict) {
    const cls = r.verdict === "BLOCKED" ? "band-sandbox" : r.verdict === "WARNED" ? "band-step_up" : "band-allow";
    return (
      <div className={`result ${cls}`} data-testid="last-result">
        <h2>{r.verdict === "BLOCKED" ? "Payment blocked" : r.verdict === "WARNED" ? "Sent after a warning" : "Sent"}</h2>
        <p>{r.reason}</p>
        {r.check && <CheckLine check={r.check} />}
        {r.verdict === "BLOCKED" && <p className="small muted">No money left the account and nothing was recorded as a payment.</p>}
      </div>
    );
  }
  if (r.action) {
    const text =
      r.action === "reported"
        ? `${bank(r.bank!)} filed a new report on the ledger`
        : r.action === "confirmed"
          ? `${bank(r.bank!)} confirmed another bank's report`
          : `${bank(r.bank!)} had already reported this payee`;
    return (
      <div className="result band-step_up" data-testid="last-result">
        <h2>Reported</h2>
        <p>
          {text}. The payee is now <span className={r.statusAfter === "CONFIRMED" ? "pill bad" : "pill warn"}>{r.statusAfter ?? "?"}</span>
        </p>
        {r.txId && (
          <p className="small muted">
            <span className="mono">tx {short(r.txId, 16)}</span> · block #{r.block} · signed with {bank(r.bank!)}'s private key
          </p>
        )}
      </div>
    );
  }
  return null;
}

function LedgerPill({ t }: { t: DemoTransfer }) {
  if (t.verdict === "BLOCKED") return <span className="pill bad">blocked</span>;
  if (t.ledgerStatus === "recorded") return <span className="pill teal">on the ledger · block #{t.ledgerBlock}</span>;
  if (t.ledgerStatus === "pending") return <span className="pill">recording on the ledger…</span>;
  if (t.ledgerStatus === "failed") return <span className="pill bad" title={t.ledgerError ?? ""}>ledger record failed</span>;
  return <span className="pill">not on the ledger</span>;
}

function Transfers({ me, onChange, setErr }: { me: DemoMe; onChange: (m: DemoMe) => void; setErr: (s: string) => void }) {
  const [busy, setBusy] = useState("");
  const act = async (id: string, f: (id: string) => Promise<DemoMe>) => {
    setBusy(id);
    setErr("");
    try {
      onChange(await f(id));
    } catch (err) {
      setErr(explain(err));
    } finally {
      setBusy("");
    }
  };
  if (me.transfers.length === 0) return <p className="muted">No payments yet.</p>;
  return (
    <ul className="list" data-testid="transfers">
      {me.transfers.map((t) => (
        <li key={t.id} data-testid="transfer">
          <div className="row">
            <div>
              <b>{t.direction === "sent" ? `To ${t.counterpartyName ?? t.counterparty}` : `From ${t.counterpartyName ?? t.counterparty}`}</b>{" "}
              <BankTag msp={t.counterpartyBank} />
              <div className="mono small muted">{t.counterparty}</div>
            </div>
            <div style={{ textAlign: "right" }}>
              {t.verdict === "BLOCKED" ? (
                <div className="amt blocked"><s>{inr(t.amount)}</s> not sent</div>
              ) : (
                <div className={t.direction === "sent" ? "amt out" : "amt in"}>
                  {t.direction === "sent" ? "−" : "+"}
                  {inr(t.amount)}
                </div>
              )}
              <LedgerPill t={t} />
            </div>
          </div>
          {t.direction === "sent" && (
            <>
              <p className="small" style={{ margin: "6px 0" }}>
                {t.reason}
              </p>
              {t.report && (
                <p className="small" data-testid="report-line">
                  Reported: {t.report.action === "reported" ? `${bank(t.report.bank)} filed a report` : t.report.action === "confirmed" ? `${bank(t.report.bank)} confirmed the report` : "already reported by your bank"}{" "}
                  → <span className={t.report.statusAfter === "CONFIRMED" ? "pill bad" : "pill warn"}>{t.report.statusAfter}</span>
                  {t.report.txId && <span className="mono muted"> tx {short(t.report.txId, 12)} · block #{t.report.block}</span>}
                </p>
              )}
              <div className="row wrap">
                {t.verdict !== "BLOCKED" && !t.report && (
                  <button className="danger small-btn" disabled={busy === t.id} onClick={() => act(t.id, demoReport)}>
                    {busy === t.id ? "Filing on the ledger…" : "I was scammed: report it"}
                  </button>
                )}
                {t.ledgerStatus === "failed" && (
                  <button className="small-btn" onClick={() => act(t.id, demoRetry)}>
                    Retry ledger record
                  </button>
                )}
                {t.ledgerStatus === "recorded" &&
                  (t.tampered ? (
                    <button className="small-btn" onClick={() => act(t.id, demoRestore)}>
                      Undo the database edit
                    </button>
                  ) : (
                    <button className="small-btn ghost" title="Demo: change this bank's database copy of the payment" onClick={() => act(t.id, demoTamper)}>
                      Tamper: edit the bank's database copy
                    </button>
                  ))}
              </div>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------- the ledger, per bank

function ChainStrip({ blocks, selected, onPick }: { blocks: ChainBlock[]; selected: number | null; onPick: (n: number) => void }) {
  const ordered = [...blocks].reverse();
  const strip = useRef<HTMLDivElement>(null);
  const newest = blocks[0]?.number;
  useEffect(() => {
    if (strip.current) strip.current.scrollLeft = strip.current.scrollWidth; // newest block in view
  }, [newest]);
  return (
    <div className="mini-chain" data-testid="demo-chain" ref={strip}>
      {ordered.map((b, i) => (
        <div key={b.number} className="mini-wrap">
          {i > 0 && (
            <span className={b.linkVerified === false ? "mini-link broken" : "mini-link"} title="previous hash = hash of the block before">
              →
            </span>
          )}
          <button className={selected === b.number ? "mini-block on" : "mini-block"} onClick={() => onPick(b.number)} data-testid="mini-block">
            <b>#{b.number}</b>
            <span className="mono">{short(b.hash, 8)}</span>
            <span className="small">{b.txs.map((t) => t.fn ?? t.type).join(", ")}</span>
            <span className={b.dataHashVerified ? "tiny ok-text" : "tiny bad-text"}>{b.dataHashVerified ? "data hash ✓" : "data hash ✗"}</span>
          </button>
        </div>
      ))}
    </div>
  );
}

function BlockDetail({ block }: { block: ChainBlock }) {
  return (
    <div className="block-detail" data-testid="demo-block">
      <p className="mono small muted">
        block #{block.number} · hash {short(block.hash, 20)} · previous {short(block.previousHash, 20)}{" "}
        {block.linkVerified === true && <span className="ok-text">· linked ✓</span>}
      </p>
      {block.txs.map((t, i) => (
        <div key={i} className="tx">
          <div className="row">
            <span className="fn">{t.type === "config" ? "Channel configuration" : t.fn}</span>
            <span className={t.valid === false ? "pill bad" : "pill ok"}>{t.valid === false ? `invalid (${t.validation})` : "valid"}</span>
          </div>
          {t.fn === "recordPayment" && <p className="small muted">argument: payment id {t.args[0]} (that is all the block holds)</p>}
          <Signatures tx={t} />
        </div>
      ))}
    </div>
  );
}

function PaymentRow({ p, viewer }: { p: DemoPaymentView; viewer: string }) {
  const member = p.access === "member";
  return (
    <li className={member ? "pay-row member" : "pay-row outsider"} data-testid="ledger-payment">
      <div className="row">
        <div>
          <BankTag msp={p.public.fromBank} /> → <BankTag msp={p.public.toBank} />
          <span className="mono muted small"> · tx {short(p.txId, 10)}</span>
        </div>
        {member ? <span className="pill teal">{bank(viewer)} can read this</span> : <span className="lock">PRIVATE</span>}
      </div>
      {member && p.details ? (
        <p className="small" style={{ margin: "6px 0 2px" }} data-testid="payment-details">
          <b>{inr(p.details.amountPaise / 100)}</b> from {p.details.payerName} (<span className="mono">{p.details.payer}</span>) to{" "}
          <span className="mono">{p.details.payee}</span> · {p.details.verdict.toLowerCase()}
        </p>
      ) : (
        <p className="small muted" style={{ margin: "6px 0 2px" }} data-testid="payment-hidden">
          Private to {bank(p.public.fromBank)} and {bank(p.public.toBank)}. {bank(viewer)}'s peer has only the hash: the amount, names and
          UPI IDs never reached it.
        </p>
      )}
      <p className="mono tiny muted" style={{ margin: 0 }}>
        public hash {short(p.public.dataHash, 24)} · collection {p.public.collection}
      </p>
      {p.bankCopy && (
        <p className={p.bankCopy.matchesChain ? "small ok-text" : "small bad-text"} data-testid="bank-copy" style={{ margin: "4px 0 0" }}>
          {bank(viewer)}'s database copy: {inr(p.bankCopy.amount)} ·{" "}
          {p.bankCopy.matchesChain ? "its hash matches the chain ✓" : "its hash does NOT match the chain: the database was edited after the payment ✗"}
        </p>
      )}
    </li>
  );
}

function Keys() {
  const [orgs, setOrgs] = useState<OrgIdentity[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    demoKeys().then((r) => setOrgs(r.orgs), (e) => setErr(explain(e)));
  }, []);
  if (err) return <p className="error">{err}</p>;
  if (!orgs) return <p className="muted">Loading certificates…</p>;
  return (
    <div className="keys" data-testid="keys">
      {orgs.map((o) => (
        <div key={o.msp} className="key-card" style={{ borderTopColor: BANK_COLOR[o.msp] }}>
          <h3 style={{ color: BANK_COLOR[o.msp], marginTop: 0 }}>{bank(o.msp)}</h3>
          {o.error && <p className="error small">{o.error}</p>}
          {[["Certificate authority", o.ca], ["Client (signs transactions)", o.client], ["Peer (signs endorsements)", o.peer]].map(([label, c]) =>
            c && typeof c === "object" ? (
              <div key={label as string} className="key-line">
                <span className="small muted">{label as string}</span>
                <span className="mono small">{c.subject}</span>
                <span className="mono tiny">
                  {c.keyType} public key {c.keyFingerprint.slice(0, 12)}…
                </span>
                {c.issuedByOrgCA !== null && label !== "Certificate authority" && (
                  <span className={c.issuedByOrgCA ? "tiny ok-text" : "tiny bad-text"}>{c.issuedByOrgCA ? "✓ signed by the bank's CA" : "✗ not signed by the bank's CA"}</span>
                )}
              </div>
            ) : null,
          )}
          <p className="tiny muted">Private keys never leave: {o.privateKey}</p>
        </div>
      ))}
    </div>
  );
}

function LedgerSide({ me, tick }: { me: DemoMe; tick: number }) {
  const [viewer, setViewer] = useState(me.account.bank);
  const [payments, setPayments] = useState<DemoPaymentView[]>([]);
  const [blocks, setBlocks] = useState<ChainBlock[]>([]);
  const [height, setHeight] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<"payments" | "chain" | "keys">("payments");

  useEffect(() => setViewer(me.account.bank), [me.account.bank]);
  useEffect(() => {
    let live = true;
    Promise.all([demoLedger(viewer), demoChain(6)])
      .then(([l, c]) => {
        if (!live) return;
        setPayments(l.payments);
        setBlocks(c.blocks);
        setHeight(c.info.height);
        setErr(l.error ?? "");
      })
      .catch((e) => live && setErr(explain(e)));
    return () => {
      live = false;
    };
  }, [viewer, tick]);
  const block = blocks.find((b) => b.number === selected) ?? blocks[0];

  return (
    <section className="ledger-side" data-testid="ledger-side">
      <div className="row">
        <h2>
          The ledger <span className="hl">as {bank(viewer)} sees it</span>
        </h2>
        {height !== null && <span className="pill teal">height {height}</span>}
      </div>
      <p className="small muted" style={{ margin: "6px 0 10px" }}>
        Each view is a real query to that organisation's own peer, signed with its own key. Switch to compare.
      </p>
      <div className="tabs viewer-tabs">
        {VIEWERS.map((v) => (
          <button key={v} className={viewer === v ? "tab active" : "tab"} onClick={() => setViewer(v)} data-testid={`view-${v}`}>
            {bank(v)}
            {v === me.account.bank ? " (you)" : ""}
          </button>
        ))}
      </div>
      {err && <p className="error">{err}</p>}
      <div className="tabs">
        <button className={tab === "payments" ? "tab active" : "tab"} onClick={() => setTab("payments")}>
          Payments
        </button>
        <button className={tab === "chain" ? "tab active" : "tab"} onClick={() => setTab("chain")}>
          Blocks and signatures
        </button>
        <button className={tab === "keys" ? "tab active" : "tab"} onClick={() => setTab("keys")}>
          Keys
        </button>
      </div>
      {tab === "payments" &&
        (payments.length === 0 ? (
          <p className="muted">No payments on the ledger yet. Send one.</p>
        ) : (
          <ul className="list">{payments.map((p) => <PaymentRow key={p.paymentId} p={p} viewer={viewer} />)}</ul>
        ))}
      {tab === "chain" && (
        <>
          <ChainStrip blocks={blocks} selected={block?.number ?? null} onPick={setSelected} />
          {block && <BlockDetail block={block} />}
          <p className="small muted">
            Every peer holds the same blocks. What differs per bank is the private data beside them: a bank's peer stores the details only
            for payments it is part of.
          </p>
        </>
      )}
      {tab === "keys" && (
        <>
          <p className="small muted">
            Each bank has a certificate authority. It signs certificates that bind a public key to a name. The matching private key signs
            transactions and endorsements; anyone can check those signatures with the public key, as the Blocks tab does.
          </p>
          <Keys />
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- page

export default function Demo() {
  const [accounts, setAccounts] = useState<DemoAccounts | null>(null);
  const [me, setMe] = useState<DemoMe | null>(null);
  const [checked, setChecked] = useState(false);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const fresh = await demoMe();
      setMe((old) => ({ ...fresh, result: old?.result }));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setMe(null);
    }
    setTick((t) => t + 1);
  }, []);

  useEffect(() => {
    demoAccounts().then(setAccounts, (e) => setError(e instanceof ApiError && e.status === 404 ? "The demo is off: start the API with DEMO_LOGIN=1." : explain(e)));
    demoMe()
      .then(setMe, () => setMe(null))
      .finally(() => setChecked(true));
  }, []);
  useEffect(() => {
    if (!me) return;
    const id = window.setInterval(refresh, 4000);
    return () => window.clearInterval(id);
  }, [me?.account.id, refresh]); // eslint-disable-line react-hooks/exhaustive-deps

  const update = (m: DemoMe) => {
    setMe(m);
    setTick((t) => t + 1);
    window.setTimeout(refresh, 2500); // the ledger record lands a few seconds after the payment
  };

  return (
    <div className="shell demo-page">
      <nav className="nav">
        <a className="brand" href="#/demo" style={{ textDecoration: "none" }}>
          <Mark />
          <span>
            Sentinel
            <small>BANK NETWORK DEMO · HYPERLEDGER FABRIC</small>
          </span>
        </a>
        <div className="row wrap">
          {me && (
            <>
              <button
                onClick={async () => {
                  if (window.confirm("Start the story again? Balances reset and there is a new scammer account. The ledger keeps every block.")) update(await demoReset());
                }}
              >
                Reset the story
              </button>
              <button
                className="white"
                onClick={async () => {
                  await demoLogout();
                  setMe(null);
                }}
              >
                Switch customer
              </button>
            </>
          )}
          <a href="#/" className="small">
            Back to the app
          </a>
        </div>
      </nav>
      {error && <p className="error">{error}</p>}
      {checked && !me && accounts && <Login accounts={accounts} onIn={update} />}
      {me && (
        <div className="demo-grid">
          <section>
            <div className="customer-head" style={{ borderColor: BANK_COLOR[me.account.bank] }} data-testid="customer-head">
              <div>
                <span className="bank-tag big" style={{ borderColor: BANK_COLOR[me.account.bank], color: BANK_COLOR[me.account.bank] }}>
                  {me.account.bankName}
                </span>
                <h1 style={{ margin: "10px 0 2px" }}>{me.account.name}</h1>
                <span className="mono muted">{me.account.upi}</span>
              </div>
              <div className="stat fill demo-balance">
                <span>Balance</span>
                <b data-testid="balance">{inr(me.account.balance)}</b>
              </div>
            </div>
            <SendForm key={me.account.id} me={me} onDone={update} />
            <LastResult me={me} />
            <h3>Your payments</h3>
            <Transfers me={me} onChange={update} setErr={setError} />
          </section>
          <LedgerSide me={me} tick={tick} />
        </div>
      )}
    </div>
  );
}
