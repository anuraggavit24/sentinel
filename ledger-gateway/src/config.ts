/** Configuration from the environment. Paths default to Fablo's layout for BankA's User1. */
export interface Config {
  port: number;
  token: string;
  writeToken: string;       // empty = the write routes do not exist
  mspId: string;
  peerEndpoint: string;     // host:port the sidecar dials
  peerHostAlias: string;    // name on the peer's TLS certificate
  tlsCertPath: string;      // the peer organisation's TLS CA certificate
  mspDir: string;           // the identity: signcerts/ and keystore/ inside
  channel: string;
  chaincode: string;
  timeoutMs: number;
  submitTimeoutMs: number;  // endorse + order + commit for a write
}

const ORG = '/crypto/banka';

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
  return {
    port: Number(env.PORT ?? 8100),
    token,
    writeToken,
    mspId: env.MSP_ID ?? 'BankAMSP',
    peerEndpoint: env.PEER_ENDPOINT ?? 'host.docker.internal:7041',
    peerHostAlias: env.PEER_HOST_ALIAS ?? 'peer0.banka.example.com',
    tlsCertPath: env.TLS_CERT_PATH ?? `${ORG}/peers/peer0.banka.example.com/tls/ca.crt`,
    mspDir: env.MSP_DIR ?? `${ORG}/users/User1@banka.example.com/msp`,
    channel: env.CHANNEL ?? 'fraud-intel',
    chaincode: env.CHAINCODE ?? 'fraudintel',
    timeoutMs: Number(env.LEDGER_TIMEOUT_MS ?? 2000),
    submitTimeoutMs: Number(env.LEDGER_SUBMIT_TIMEOUT_MS ?? 15000),
  };
}
