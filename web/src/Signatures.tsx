import type { ChainTx, SignatureView } from "./api";

export const ORG_NAME: Record<string, string> = {
  BankAMSP: "Bank A",
  BankBMSP: "Bank B",
  BankCMSP: "Bank C",
  RegulatorMSP: "Regulator",
  OrdererMSP: "Orderer",
};

const fp = (h: string | null) => (h ? `${h.slice(0, 8)}…${h.slice(-4)}` : "?");

function Mark({ ok }: { ok: boolean | null }) {
  if (ok === null) return <span className="sig-mark unknown" title="not checked">?</span>;
  return <span className={ok ? "sig-mark ok" : "sig-mark bad"}>{ok ? "✓" : "✗"}</span>;
}

function SigLine({ s, role }: { s: SignatureView; role: string }) {
  const issuer = ORG_NAME[s.msp ?? ""] ?? s.msp ?? "?";
  return (
    <li className="sig" data-testid="sig">
      <Mark ok={s.signatureValid} />
      <span>
        <b>{role}</b> <span className="mono">{s.subject ?? s.msp}</span>
        <span className="muted">
          {" "}
          · public key {fp(s.keyFingerprint)} ·{" "}
          {s.signatureValid === null ? "nothing signed" : s.signatureValid ? "signature valid" : "SIGNATURE DOES NOT MATCH"}
          {s.issuedByOrgCA === true && <> · certificate issued by {issuer}'s CA</>}
          {s.issuedByOrgCA === false && <strong className="bad-text"> · NOT issued by {issuer}'s CA</strong>}
        </span>
      </span>
    </li>
  );
}

/** Who signed a transaction, each signature checked with the signer's public key, and any private data. */
export function Signatures({ tx }: { tx: ChainTx }) {
  if (tx.type !== "endorser") return null;
  const sigs = tx.endorsements ?? [];
  return (
    <div className="sigs">
      <ul className="sig-list">
        {tx.signer && <SigLine s={tx.signer} role="Submitted, signed with its private key by" />}
        {sigs.map((s, i) => (
          <SigLine key={i} s={s} role={`Endorsement ${i + 1} signed by`} />
        ))}
      </ul>
      {(tx.privateCollections ?? []).length > 0 && (
        <p className="private-note" data-testid="private-note">
          <span className="lock">PRIVATE</span> details written to <span className="mono">{tx.privateCollections!.join(", ")}</span>. Only
          their hash is in this block; the details went peer to peer to the two banks in the payment.
        </p>
      )}
    </div>
  );
}
