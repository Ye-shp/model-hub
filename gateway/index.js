import {readConfig} from './config.js';
import {createApp} from './app.js';
import {ClientStore} from './clients.js';
import {UsageLedger} from './ledger.js';

const config = readConfig();
const ledger = new UsageLedger(config.dataDir, config.frontierMonthlyCalls);
await ledger.init();
const clients = new ClientStore(config.dataDir);
const app = createApp({config, clients, ledger, log: entry => console.log(JSON.stringify({t: new Date().toISOString(), ...entry}))});
await app.listen({port: config.port, host: config.host});
console.log(`Gateway listening on ${config.host}:${config.port} with models: ${config.models.map(m => m.id).join(', ')}`);
if (clients.list().length === 0) console.log('No API keys yet. Create one with: node gateway/keys.js add <name>');
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => { await app.close(); process.exit(0); });
