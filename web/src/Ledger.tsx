import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  explain,
  getChain,
  getChainTx,
  lookupPayee,
  type ChainBlock,
  type ChainTx,
  type ChainView,
  type LookupStep,
  type PayeeType,
} from "./api";
import { Signatures } from "./Signatures";

const short = (h: string, n = 10) => (h ? `${h.slice(0, n)}…` : "(none)");
const ORG_NAME: Record<string, string> = { BankAMSP: "Bank A", BankBMSP: "Bank B", BankCMSP: "Bank C", RegulatorMSP: "Regulator", OrdererMSP: "Orderer" };
const ARG_NAMES: Record<string, string[]> = {
  reportMule: ["payee hash", "id type", "evidence hash", "source", "severity"],
  confirmReport: ["report id (a tx id)", "evidence hash", "source"],
  disputeReport: ["report id", "evidence hash"],
  retractReport: ["report id"],
  resolveDispute: ["report id", "outcome"],
  recordPayment: ["payment id (the details are private)"],
};
const FN_PLAIN: Record<string, string> = {
  reportMule: "a bank reported a payee as a mule",
  confirmReport: "a second bank confirmed a report",
  disputeReport: "a bank disputed a report",
  retractReport: "the reporting bank withdrew its report",
  resolveDispute: "the regulator resolved a dispute",
  recordPayment: "a bank recorded a payment (details private to the two banks)",
};

/** The four organisations around the orderer; endorsers of the selected transaction light up. */
function Network({ view, tx }: { view: ChainView; tx: ChainTx | null }) {
  const pos: Record<string, [number, number]> = { BankAMSP: [110, 70], BankBMSP: [370, 40], BankCMSP: [630, 70], RegulatorMSP: [880, 70] };
  const endorsed = new Set(tx?.endorsers ?? []);
  const creator = tx?.creator;
  return (
    <svg viewBox="0 0 1000 250" width="100%" role="img" aria-label="Consortium network" data-testid="network">
      {view.network.orgs.map((o) => {
        const [x, y] = pos[o.msp] ?? [500, 60];
        const on = endorsed.has(o.msp);
        return <line key={o.msp} x1={x} y1={y + 34} x2={500} y2={205} stroke={on ? "#1ed7c0" : "#3b3742"} strokeWidth={on ? 3 : 1.5} strokeDasharray={on ? "6 5" : ""} />;
      })}
      <rect x="40" y="138" width="920" height="34" rx="4" fill="#213230" stroke="#1ed7c0" />
      <text x="500" y="160" textAnchor="middle" fill="#1ed7c0" fontSize="14" fontWeight="700">
        CHANNEL {view.network.channel} · CHAINCODE {view.network.chaincode} · EVERY PEER HOLDS THE SAME CHAIN
      </text>
      {view.network.orgs.map((o) => {
        const [x, y] = pos[o.msp] ?? [500, 60];
        const on = endorsed.has(o.msp);
        return (
          <g key={o.msp} className="net-node">
            <rect x={x - 85} y={y - 30} width="170" height="64" rx="4" fill={on ? "#1ed7c0" : "#27242c"} stroke={o.msp === creator ? "#ffffff" : on ? "#1ed7c0" : "#3b3742"} strokeWidth={o.msp === creator ? 3 : 1.5} />
            <text x={x} y={y - 4} textAnchor="middle" fill={on ? "#062f2a" : "#f4f3f5"} fontSize="17" fontWeight="900" fontFamily="Archivo, sans-serif">
              {o.name.toUpperCase()}
              {o.us ? " (US)" : ""}
            </text>
            <text x={x} y={y + 18} textAnchor="middle" fill={on ? "#062f2a" : "#a39eab"} fontSize="12">
              {on ? (o.msp === creator ? "submitted + endorsed" : "endorsed") : o.msp === creator ? "submitted" : o.role}
            </text>
          </g>
        );
      })}
      <rect x="420" y="200" width="160" height="40" rx="4" fill="#302c36" stroke="#a39eab" />
      <text x="500" y="226" textAnchor="middle" fill="#f4f3f5" fontSize="14" fontWeight="800" fontFamily="Archivo, sans-serif">
        ORDERER (RAFT)
      </text>
    </svg>
  );
}

function TxCard({ tx, analyst }: { tx: ChainTx; analyst: boolean }) {
  const names = ARG_NAMES[tx.fn ?? ""] ?? [];
  return (
    <div className="tx" data-testid="chain-tx">
      <div className="row">
        <div>
          <span className="fn">{tx.type === "config" ? "Channel configuration" : tx.fn ?? tx.type}</span>
          {tx.fn && FN_PLAIN[tx.fn] && <span className="muted"> · {FN_PLAIN[tx.fn]}</span>}
        </div>
        <span className={tx.valid === false ? "pill bad" : "pill ok"}>{tx.valid === false ? `invalid (code ${tx.validation})` : "valid"}</span>
      </div>
      <p className="small muted" style={{ margin: "6px 0" }}>
        submitted by <strong style={{ color: "#f4f3f5" }}>{ORG_NAME[tx.creator ?? ""] ?? tx.creator ?? "?"}</strong>
        {tx.timestamp && <> · {new Date(tx.timestamp).toLocaleString()}</>}
        {tx.ours && <span className="badge">filed by our fraud ops ({tx.ours.source.toLowerCase().replace("_", " ")})</span>}
      </p>
      {tx.endorsers.length > 0 && (
        <p className="small" style={{ margin: "4px 0" }}>
          endorsed by {tx.endorsers.map((e) => <span key={e} className="endorser">{ORG_NAME[e] ?? e}</span>)}
          {tx.endorsers.length >= 2 && <span className="muted"> satisfies OutOf(2 banks)</span>}
        </p>
      )}
      {tx.txId && <p className="mono muted" style={{ margin: "4px 0" }}>tx {tx.txId}</p>}
      <Signatures tx={tx} />
      {tx.type === "endorser" &&
        (analyst ? (
          tx.args.length > 0 && (
            <table className="small" style={{ marginTop: 6 }}>
              <tbody>
                {tx.args.map((a, i) => (
                  <tr key={i}>
                    <td className="muted" style={{ width: 170 }}>{names[i] ?? `arg ${i + 1}`}</td>
                    <td className="mono">{a.length > 40 ? short(a, 24) : a}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        ) : (
          <p className="small muted">Arguments (keyed hashes, report ids) are shown to analysts only.</p>
        ))}
    </div>
  );
}

const PIPE = ["Propose", "Endorse", "Order", "Validate", "Commit"];

function Pipeline({ tx, block, step }: { tx: ChainTx; block: ChainBlock; step: number }) {
  const bad = tx.valid === false;
  const text = [
    `${ORG_NAME[tx.creator ?? ""] ?? tx.creator} signs a proposal: ${tx.fn}(…)`,
    `${tx.endorsers.map((e) => ORG_NAME[e] ?? e).join(" + ") || "peers"} run the chaincode and sign the result`,
    `The orderer puts it in block #${block.number} with ${block.txCount - 1} other tx`,
    bad ? `Peers marked it INVALID (code ${tx.validation}): no effect on the state` : "Every peer checks signatures, the 2-bank policy and read conflicts",
    bad ? "Recorded in the chain, but the report was not changed" : `On every bank's copy. Block hash ${short(block.hash, 12)}`,
  ];
  return (
    <ol className="pipeline" data-testid="pipeline">
      {PIPE.map((p, i) => (
        <li key={p} className={i < step ? (bad && i >= 3 ? "bad" : "on") : ""}>
          <b>
            {i + 1}. {p}
          </b>
          <span>{text[i]}</span>
        </li>
      ))}
    </ol>
  );
}

function Weights({ weight }: { weight: number | null }) {
  const marks: [number, string][] = [[0, "0 · sandbox only"], [0.25, "0.25 · one bank"], [0.5, "0.5 · disputed"], [1, "1 · CONFIRMED"]];
  return (
    <div className="weights" aria-label="Ledger weight scale">
      <div className="bar" />
      {marks.map(([w, l]) => (
        <div key={w} className="mark" style={{ left: `${w * 100}%` }}>
          <i />
          {l}
        </div>
      ))}
      {weight !== null && <div className="you" style={{ left: `${weight * 100}%` }} title={`weight ${weight}`} />}
    </div>
  );
}

function PayeeCheck() {
  const [type, setType] = useState<PayeeType>("UPI");
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [res, setRes] = useState<{ steps: LookupStep[]; weight: number | null; points: number | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function go(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const payee: Record<string, string> = type === "UPI" ? { upi: a } : type === "PHONE" ? { phone: a } : { ifsc: a, account: b };
      const r = await lookupPayee(type, payee);
      setRes({ steps: r.steps, weight: typeof r.ledger.maxWeight === "number" ? r.ledger.maxWeight : null, points: r.points });
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel" data-testid="payee-check">
      <h2>Check a payee on the ledger</h2>
      <p className="muted">Exactly what a payment does, step by step. Analysts only: for anyone else this would be a mule-checking service.</p>
      <form className="inline-form" onSubmit={go}>
        <label>
          Payee type
          <select value={type} onChange={(e) => setType(e.target.value as PayeeType)}>
            <option value="UPI">UPI ID</option>
            <option value="ACCOUNT">Bank account</option>
            <option value="PHONE">Mobile number</option>
          </select>
        </label>
        <label style={{ flex: 1 }}>
          {type === "UPI" ? "Payee UPI ID" : type === "PHONE" ? "Payee mobile" : "Payee IFSC"}
          <input value={a} onChange={(e) => setA(e.target.value)} required />
        </label>
        {type === "ACCOUNT" && (
          <label style={{ flex: 1 }}>
            Payee account
            <input value={b} onChange={(e) => setB(e.target.value)} required />
          </label>
        )}
        <button className="primary" disabled={busy} type="submit">
          {busy ? "Asking…" : "Check"}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {res && (
        <>
          <ol className="pipeline" style={{ marginTop: 16 }} data-testid="lookup-steps">
            {res.steps.map((s) => (
              <li key={s.step} className="on">
                <b>{s.step}</b>
                <span>{s.detail}</span>
                {s.value && <p className="mono" style={{ margin: "6px 0 0", color: "#f4f3f5" }}>{s.value.length > 40 ? short(s.value, 20) : s.value}</p>}
              </li>
            ))}
          </ol>
          <Weights weight={res.weight} />
        </>
      )}
    </div>
  );
}

/**
 * The blockchain, live: the network, the latest blocks linked by hash (recomputed, not trusted), who
 * endorsed each transaction, and a step-by-step trace of any report through Fabric's
 * execute-order-validate flow.
 */
export default function Ledger({ analyst, traceTx }: { analyst: boolean; traceTx: string | null }) {
  const [view, setView] = useState<ChainView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [fresh, setFresh] = useState<number | null>(null);
  const [traceId, setTraceId] = useState(traceTx ?? "");
  const [trace, setTrace] = useState<{ tx: ChainTx; block: ChainBlock } | null>(null);
  const [step, setStep] = useState(0);
  const [traceErr, setTraceErr] = useState<string | null>(null);
  const top = useRef<number | null>(null);
  const strip = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const v = await getChain(8);
      setError(null);
      const newest = v.blocks[0]?.number ?? null;
      if (top.current !== null && newest !== null && newest > top.current) setFresh(newest);
      top.current = newest;
      setView(v);
      setSel((s) => s ?? newest);
    } catch (e) {
      setError(explain(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const runTrace = useCallback(async (id: string) => {
    setTraceErr(null);
    setTrace(null);
    setStep(0);
    try {
      const r = await getChainTx(id.trim());
      if (!r.tx) throw new Error("Transaction not found in its block");
      setTrace({ tx: r.tx, block: r.block });
      setSel(r.block.number);
      for (let i = 1; i <= 5; i++) window.setTimeout(() => setStep(i), i * 450);
    } catch (e) {
      setTraceErr(explain(e));
    }
  }, []);

  useEffect(() => {
    if (traceTx) {
      setTraceId(traceTx);
      void runTrace(traceTx);
    }
  }, [traceTx, runTrace]);

  // Keep the newest block in sight when the chain is wider than the page.
  useEffect(() => {
    if (strip.current) strip.current.scrollLeft = strip.current.scrollWidth;
  }, [view?.info.height]);

  const blocks = view ? [...view.blocks].reverse() : []; // oldest left, newest right
  const selected = view?.blocks.find((b) => b.number === sel) ?? null;
  const focusTx = trace?.tx ?? selected?.txs.find((t) => t.type === "endorser") ?? null;
  const txsInView = view?.blocks.reduce((n, b) => n + b.txCount, 0) ?? 0;

  return (
    <div data-testid="ledger" style={{ position: "relative" }}>
      <div className="row" style={{ alignItems: "flex-end", marginBottom: 20 }}>
        <h2 className="display" style={{ fontSize: "clamp(34px, 5vw, 64px)" }}>
          How the <span className="hl">ledger</span>
          <br />
          works, <span className="hl">live</span>
        </h2>
        {view && <span className="live">live · refreshes every 5 s</span>}
      </div>

      {error && (
        <div className="panel">
          <p className="error">{error}</p>
          <p className="muted">
            Start the Fabric network (<code>fablo start</code>) and the ledger gateway (<code>docker compose up -d</code>). Payments keep
            working without it; they just score without the ledger.
          </p>
        </div>
      )}

      {view && (
        <>
          <section className="grid-4" style={{ marginTop: 0 }}>
            <div className="stat fill" data-testid="height"><b>{view.info.height}</b><span>blocks on the chain</span></div>
            <div className="stat"><b className="mono">{short(view.info.currentBlockHash, 12)}</b><span>hash of the newest block</span></div>
            <div className="stat"><b>{txsInView}</b><span>transactions in the last {view.blocks.length} blocks</span></div>
            <div className="stat fill"><b>2 of 3</b><span>banks must endorse every write</span></div>
          </section>

          <section className="panel">
            <h3 style={{ marginTop: 0 }}>The network {focusTx?.fn ? `· ${focusTx.fn}` : ""}</h3>
            <p className="muted small" style={{ marginTop: 0 }}>
              Highlighted: the banks that endorsed the selected transaction. White outline: the bank that submitted it.
            </p>
            <Network view={view} tx={focusTx} />
          </section>

          <section>
            <h3>The chain: each block stores the hash of the one before</h3>
            <div className="chain" data-testid="chain" ref={strip}>
              {blocks.map((b, i) => (
                <div key={b.number} style={{ display: "flex", flex: "1 1 auto" }}>
                  {i > 0 && (
                    <div className={b.linkVerified === false ? "link-arrow broken" : "link-arrow"} title="previousHash = hash of the block before">
                      <b>{b.linkVerified === false ? "✗" : "→"}</b>
                      {b.linkVerified === false ? "broken" : "linked"}
                    </div>
                  )}
                  <button
                    type="button"
                    className={`block${b.number === sel ? " sel" : ""}${b.number === fresh ? " fresh" : ""}`}
                    onClick={() => setSel(b.number)}
                    data-testid="block"
                  >
                    <span className="num">#{b.number}</span>
                    <span className="small">{b.txCount} tx · {b.txs.some((t) => t.type === "config") ? "config" : b.txs.map((t) => t.fn).filter(Boolean).join(", ") || "—"}</span>
                    <span className="h">hash {short(b.hash, 14)}</span>
                    <span className="h">prev {short(b.previousHash, 14)}</span>
                    <span className="small" style={{ color: b.dataHashVerified ? "#3ddc97" : "#ff5c6c" }}>
                      {b.dataHashVerified ? "✓ data hash matches its txs" : "✗ data hash mismatch"}
                    </span>
                  </button>
                </div>
              ))}
            </div>
            <p className="muted small">
              Hashes are recomputed from the block contents, not taken on trust. Changing one byte of any transaction changes the data hash,
              which changes the block hash, which breaks the link from the next block.
            </p>
          </section>

          {selected && (
            <section className="panel" data-testid="block-detail">
              <h3 style={{ marginTop: 0 }}>Block #{selected.number}</h3>
              <p className="mono muted">hash {selected.hash}<br />previous {selected.previousHash || "(genesis)"}<br />data {selected.dataHash}</p>
              {selected.txs.map((t, i) => <TxCard key={t.txId || i} tx={t} analyst={analyst} />)}
            </section>
          )}
        </>
      )}

      <section className="panel" data-testid="trace">
        <h3 style={{ marginTop: 0 }}>Follow a report through Fabric</h3>
        <p className="muted small" style={{ marginTop: 0 }}>
          A ledger report id is the Fabric transaction id. Paste one (Fraud ops → Ledger filings shows them), or click "See it on the ledger" there.
        </p>
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); void runTrace(traceId); }}>
          <label style={{ flex: 1 }}>
            Transaction id
            <input value={traceId} onChange={(e) => setTraceId(e.target.value)} placeholder="64 hex characters" pattern="[0-9a-f]{64}" required />
          </label>
          <button className="primary" type="submit">Trace</button>
        </form>
        {traceErr && <p className="error">{traceErr}</p>}
        {trace && (
          <div style={{ marginTop: 16 }}>
            <Pipeline tx={trace.tx} block={trace.block} step={step} />
            <p className="small muted" style={{ marginTop: 10 }}>
              {trace.tx.fn} · endorsed by {trace.tx.endorsers.map((e) => <span key={e} className="endorser">{ORG_NAME[e] ?? e}</span>)} · in
              block #{trace.block.number} (selected above) · <span className="mono">tx {short(trace.tx.txId, 16)}</span>
            </p>
          </div>
        )}
      </section>

      {analyst && <section><PayeeCheck /></section>}
    </div>
  );
}
