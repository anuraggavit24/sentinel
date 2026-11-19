/** Configuration from the environment. Paths default to Fablo's layout (crypto-config/peerOrganizations). */
export interface OrgConfig {
  name: string;             // BankA
  mspId: string;            // BankAMSP
  peerEndpoint: string;     // host:port the sidecar dials (this organisation's own peer)
  peerHostAlias: string;    // name on that peer's TLS certificate
  tlsCertPath: string;      // the organisation's TLS CA certificate
  mspDir: string;           // the identity: signcerts/ (public certificate) and keystore/ (private key)
  orgDir: string;           // the organisation's folder: msp/cacerts, peers/peer0.../msp/signcerts
}

export interface Config {
  port: number;
  token: string;
  writeToken: string;       // empty = the write routes do not exist
  mspId: string;            // the default identity (this bank, for the risk API)
  peerEndpoint: string;
  peerHostAlias: string;
  tlsCertPath: string;
  mspDir: string;
  channel: string;
  chaincode: string;
  timeoutMs: number;
  submitTimeoutMs: number;  // endorse + order + commit for a write
  /**
   * Every organisation this sidecar can act as (the demo bank network); empty = only the default.
   * One sidecar holding several banks' keys is a demo shortcut: in production each bank runs its own
   * gateway with only its own key.
   */
  orgs?: OrgConfig[];
}

const ORG = '/crypto/banka';
/** Where Fablo publishes each organisation's peer0 on the host (CA on 7020 + 20n, peer0 one above). */
const FABLO_PEER_PORTS: Record<string, number> = { BankA: 7041, BankB: 7061, BankC: 7081, Regulator: 7101 };

export function orgConfig(name: string, env: NodeJS.ProcessEnv = process.env): OrgConfig {
  if (!/^[A-Za-z][A-Za-z0-9]{1,30}$/.test(name)) throw new Error(`bad organisation name "${name}"`);
  const lower = name.toLowerCase();
  const upper = name.toUpperCase();
  const domain = `${lower}.example.com`;
  const dir = env[`ORG_DIR_${upper}`] ?? `/crypto/${lower}`;
  const port = FABLO_PEER_PORTS[name];
  const endpoint = env[`PEER_ENDPOINT_${upper}`] ?? (port ? `host.docker.internal:${port}` : '');
  if (!endpoint) throw new Error(`PEER_ENDPOINT_${upper} must be set for ${name}`);
  return {
    name,
    mspId: `${name}MSP`,
    peerEndpoint: endpoint,
    peerHostAlias: env[`PEER_HOST_ALIAS_${upper}`] ?? `peer0.${domain}`,
    tlsCertPath: `${dir}/peers/peer0.${domain}/tls/ca.crt`,
    mspDir: `${dir}/users/User1@${domain}/msp`,
    orgDir: dir,
  };
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const token = env.LEDGER_TOKEN ?? '';
  if (token.length < 16) {
    // No default: a sidecar that answers anyone on the network is a free ledger lookup service.
    throw new Error('LEDGER_TOKEN must be set (16+ characters) and match the API');
  }
  const writeToken = env.LEDGER_WRITE_TOKEN ?? '';
  if (writeToken && (writeToken.length < 16 || writeToken === token)) {
    // A separate secret, so something that can only read (a leaked read token) can never write.
    throw new Error('LEDGER_WRITE_TOKEN must be 16+ characters and differ from LEDGER_TOKEN');
  }
  const mspId = env.MSP_ID ?? 'BankAMSP';
  const base = {
    mspId,
    peerEndpoint: env.PEER_ENDPOINT ?? 'host.docker.internal:7041',
    peerHostAlias: env.PEER_HOST_ALIAS ?? 'peer0.banka.example.com',
    tlsCertPath: env.TLS_CERT_PATH ?? `${ORG}/peers/peer0.banka.example.com/tls/ca.crt`,
    mspDir: env.MSP_DIR ?? `${ORG}/users/User1@banka.example.com/msp`,
  };
  // ORGS=BankA,BankB,BankC,Regulator turns on the demo bank network. The default identity keeps the
  // settings above (so nothing changes for the risk API); the others use Fablo's layout.
  const names = (env.ORGS ?? '').split(',').map((s) => s.trim()).filter(Boolean);
  const defaultName = mspId.replace(/MSP$/, '');
  const orgs: OrgConfig[] = names.map((n) =>
    n === defaultName
      ? { name: n, ...base, orgDir: env[`ORG_DIR_${n.toUpperCase()}`] ?? base.mspDir.replace(/\/users\/.*$/, '') }
      : orgConfig(n, env),
  );
  if (new Set(orgs.map((o) => o.mspId)).size !== orgs.length) throw new Error('ORGS lists an organisation twice');
  return {
    port: Number(env.PORT ?? 8100),
    token,
    writeToken,
    ...base,
    channel: env.CHANNEL ?? 'fraud-intel',
    chaincode: env.CHAINCODE ?? 'fraudintel',
    timeoutMs: Number(env.LEDGER_TIMEOUT_MS ?? 2000),
    submitTimeoutMs: Number(env.LEDGER_SUBMIT_TIMEOUT_MS ?? 15000),
    orgs,
  };
}
