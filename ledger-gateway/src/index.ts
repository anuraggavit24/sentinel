import { loadConfig } from './config';
import { loadCAs, loadIdentities } from './identities';
import { FabricLedger } from './ledger';
import { makeServer } from './server';

const cfg = loadConfig();
const ledger = new FabricLedger(cfg);
// The demo bank network (ORGS): one client per organisation, each with its own key and its own peer.
const orgs = new Map<string, FabricLedger>();
for (const o of cfg.orgs ?? []) orgs.set(o.mspId, o.mspId === cfg.mspId ? ledger : new FabricLedger(cfg, o));
let cas: ReturnType<typeof loadCAs> | undefined;
const server = makeServer(ledger, { read: cfg.token, write: cfg.writeToken }, {
  orgs,
  identities: () => loadIdentities(cfg.orgs ?? []),
  cas: () => (cas ??= loadCAs(cfg.orgs ?? [])),  // read once; restart the sidecar after `fablo recreate`
});
// Connects lazily on the first request, so the service starts (and reports why) even before Fablo is up.
server.listen(cfg.port, () => {
  console.log(`ledger-gateway on :${cfg.port} -> ${cfg.peerEndpoint} (${cfg.peerHostAlias}) as ${cfg.mspId}, ${cfg.channel}/${cfg.chaincode}, writes ${cfg.writeToken ? 'ON' : 'off'}`);
  for (const o of cfg.orgs ?? []) console.log(`  can act as ${o.mspId} -> ${o.peerEndpoint} (${o.peerHostAlias})`);
});
const stop = () => {
  ledger.close();
  for (const l of orgs.values()) l.close();
  server.close(() => process.exit(0));
};
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
