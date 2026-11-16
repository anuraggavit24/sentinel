/** Configuration from the environment. Paths default to Fablo's layout for BankA's User1. */
export interface Config {
  port: number;
  token: string;
  mspId: string;
  peerEndpoint: string;     // host:port the sidecar dials
  peerHostAlias: string;    // name on the peer's TLS certificate
  tlsCertPath: string;      // the peer organisation's TLS CA certificate
  mspDir: string;           // the identity: signcerts/ and keystore/ inside
  channel: string;
  chaincode: string;
  timeoutMs: number;
}

const ORG = '/crypto/banka';

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const token = env.LEDGER_TOKEN ?? '';
  if (token.length < 16) {
    // No default: a sidecar that answers anyone on the network is a free ledger lookup service.
    throw new Error('LEDGER_TOKEN must be set (16+ characters) and match the API');
  }
  return {
    port: Number(env.PORT ?? 8100),
    token,
    mspId: env.MSP_ID ?? 'BankAMSP',
    peerEndpoint: env.PEER_ENDPOINT ?? 'host.docker.internal:7041',
    peerHostAlias: env.PEER_HOST_ALIAS ?? 'peer0.banka.example.com',
    tlsCertPath: env.TLS_CERT_PATH ?? `${ORG}/peers/peer0.banka.example.com/tls/ca.crt`,
    mspDir: env.MSP_DIR ?? `${ORG}/users/User1@banka.example.com/msp`,
    channel: env.CHANNEL ?? 'fraud-intel',
    chaincode: env.CHAINCODE ?? 'fraudintel',
    timeoutMs: Number(env.LEDGER_TIMEOUT_MS ?? 2000),
  };
}
