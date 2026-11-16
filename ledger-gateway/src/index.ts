import { loadConfig } from './config';
import { FabricLedger } from './ledger';
import { makeServer } from './server';

const cfg = loadConfig();
const ledger = new FabricLedger(cfg);
const server = makeServer(ledger, cfg.token);
// Connects lazily on the first request, so the service starts (and reports why) even before Fablo is up.
server.listen(cfg.port, () => {
  console.log(`ledger-gateway on :${cfg.port} -> ${cfg.peerEndpoint} (${cfg.peerHostAlias}) as ${cfg.mspId}, ${cfg.channel}/${cfg.chaincode}`);
});
const stop = () => {
  ledger.close();
  server.close(() => process.exit(0));
};
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
