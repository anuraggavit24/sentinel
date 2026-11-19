/**
 * Public-key identities of the demo network, read from Fablo's crypto material.
 *
 * Every organisation has a certificate authority (CA). The CA's private key signs certificates; each
 * certificate binds a public key to a name ("User1@bankb.example.com", "peer0.bankb.example.com").
 * The matching private keys never leave their owner: the client's sits in its keystore (here, this
 * sidecar), the peer's inside the peer. Anyone can check a signature with the public key in the
 * certificate, and check the certificate with the CA's public key. Only public material leaves here.
 */
import * as crypto from 'node:crypto';
import { promises as fs } from 'node:fs';
import * as path from 'node:path';
import { OrgConfig } from './config';

export interface CertView {
  subject: string;           // common name, e.g. User1@bankb.example.com
  issuer: string;            // the CA that signed it
  serialNumber: string;
  validFrom: string;
  validTo: string;
  keyType: string;           // e.g. "EC P-256"
  publicKeyPem: string;
  keyFingerprint: string;    // SHA-256 of the public key (SubjectPublicKeyInfo, DER)
  issuedByOrgCA: boolean | null;
}

export interface OrgIdentity {
  msp: string;
  ca: CertView | null;
  client: CertView | null;   // signs the transactions this organisation submits
  peer: CertView | null;     // signs the endorsements this organisation's peer gives
  privateKey: string;        // where it lives; never the key itself
  error?: string;
}

export function cn(dn: string): string {
  const m = dn.match(/(?:^|\n|,\s*)CN=([^\n,]+)/);
  return m ? m[1].trim() : dn.split('\n')[0];
}

export function fingerprint(key: crypto.KeyObject): string {
  return crypto.createHash('sha256').update(key.export({ type: 'spki', format: 'der' })).digest('hex');
}

function keyType(key: crypto.KeyObject): string {
  const d = key.asymmetricKeyDetails;
  if (key.asymmetricKeyType === 'ec') return `EC ${d?.namedCurve === 'prime256v1' ? 'P-256' : d?.namedCurve ?? ''}`.trim();
  return String(key.asymmetricKeyType ?? 'unknown').toUpperCase();
}

/** True if `cert` was signed by one of `cas`. */
export function issuedBy(cert: crypto.X509Certificate, cas: crypto.X509Certificate[]): boolean {
  return cas.some((ca) => {
    try {
      return cert.checkIssued(ca) && cert.verify(ca.publicKey);
    } catch {
      return false;
    }
  });
}

export function certView(cert: crypto.X509Certificate, cas: crypto.X509Certificate[] | null): CertView {
  return {
    subject: cn(cert.subject),
    issuer: cn(cert.issuer),
    serialNumber: cert.serialNumber,
    validFrom: new Date(cert.validFrom).toISOString(),
    validTo: new Date(cert.validTo).toISOString(),
    keyType: keyType(cert.publicKey),
    publicKeyPem: cert.publicKey.export({ type: 'spki', format: 'pem' }).toString(),
    keyFingerprint: fingerprint(cert.publicKey),
    issuedByOrgCA: cas ? issuedBy(cert, cas) : null,
  };
}

async function pems(dir: string): Promise<crypto.X509Certificate[]> {
  const names = (await fs.readdir(dir)).filter((n) => !n.startsWith('.')).sort();
  const out: crypto.X509Certificate[] = [];
  for (const n of names) out.push(new crypto.X509Certificate(await fs.readFile(path.join(dir, n))));
  return out;
}

async function firstCert(dir: string): Promise<crypto.X509Certificate | null> {
  try {
    return (await pems(dir))[0] ?? null;
  } catch {
    return null;
  }
}

/** Each organisation's CA certificates, by MSP id: what endorsements and creators are checked against. */
export async function loadCAs(orgs: OrgConfig[]): Promise<Map<string, crypto.X509Certificate[]>> {
  const out = new Map<string, crypto.X509Certificate[]>();
  for (const o of orgs) {
    try {
      out.set(o.mspId, await pems(path.join(o.orgDir, 'msp', 'cacerts')));
    } catch {
      // missing: signatures are still checked, certificate issuance is reported as unknown
    }
  }
  return out;
}

export async function loadIdentities(orgs: OrgConfig[]): Promise<OrgIdentity[]> {
  const out: OrgIdentity[] = [];
  for (const o of orgs) {
    try {
      const cas = await pems(path.join(o.orgDir, 'msp', 'cacerts'));
      const domain = o.peerHostAlias.replace(/^peer0\./, '');
      const client = await firstCert(path.join(o.mspDir, 'signcerts'));
      const peer = await firstCert(path.join(o.orgDir, 'peers', `peer0.${domain}`, 'msp', 'signcerts'));
      out.push({
        msp: o.mspId,
        ca: cas[0] ? certView(cas[0], cas) : null,
        client: client ? certView(client, cas) : null,
        peer: peer ? certView(peer, cas) : null,
        privateKey: `client key: this gateway's keystore for ${o.mspId} (demo: one gateway holds every bank's key); peer key: inside peer0.${domain}`,
      });
    } catch (err) {
      out.push({ msp: o.mspId, ca: null, client: null, peer: null, privateKey: '', error: String((err as Error).message).slice(0, 200) });
    }
  }
  return out;
}
