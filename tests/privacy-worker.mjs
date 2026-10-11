import { parentPort } from 'node:worker_threads';
const ready = globalThis[Symbol.for('claude-termux.privacy.v1')]?.ready === true;
const r = await fetch('http://api.anthropic.com/api/event_logging/v2/batch', { method: 'POST', body: 'SYNTHETIC_PRIVACY_CANARY', signal: AbortSignal.timeout(2000) });
const value = { ready, status: r.status };
if (parentPort) parentPort.postMessage(value);
else postMessage(value);
