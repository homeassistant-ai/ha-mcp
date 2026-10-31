import { stdioOnlyClients, remoteOnlyClients } from './data';

// Site base path (import.meta.env.BASE_URL, e.g. /ha-mcp), so links built inside
// instruction templates keep working when the site is built with a different --base.
export const base = import.meta.env.BASE_URL;

// State
export const state = {
  method: null,      // ha-component | ha-addon | docker | uvx | stdio-local
  client: null,
  scope: null,       // local | remote (forced 'local' for stdio-local)
  platform: null,    // macos | linux | windows | docker (only when OS commands are needed)
  remotePath: null,  // builtin-webhook | webhook-proxy | cloudflared | custom (scope=remote)
};

// Display labels
export const methodLabels = {
  'ha-component': 'HA Custom Component',
  'ha-addon': 'HA app (add-on)',
  'docker': 'Docker',
  'uvx': 'uvx (Python)',
  'stdio-local': 'Local stdio',
};
export const scopeLabels = { 'local': 'Local', 'remote': 'Remote (internet)' };
export const remotePathLabels = {
  'builtin-webhook': 'Built-in webhook',
  'webhook-proxy': 'Webhook Proxy',
  'cloudflared': 'Cloudflare Tunnel',
  'custom': 'Custom Proxy',
};

// Which remote-path cards apply to each server method
export function remotePathsForMethod(method) {
  if (method === 'ha-component') return ['builtin-webhook', 'cloudflared', 'custom'];
  if (method === 'ha-addon') return ['webhook-proxy', 'cloudflared', 'custom'];
  return ['cloudflared', 'custom']; // docker, uvx
}

// The platform step only appears when OS-specific commands are needed:
// stdio-local (always), the uvx server host OS, or an HTTP method whose
// client is stdio-only (Node.js install on the client machine for the bridge).
export function needsPlatform() {
  if (state.method === 'stdio-local') return true;
  if (state.method === 'uvx') return true;
  if (state.method && state.method !== 'stdio-local' && isStdioOnly(state.client?.id)) return true;
  return false;
}

// Helper to check if client cannot speak HTTP directly and needs the
// mcp-remote bridge for HTTP connections.
export function isStdioOnly(clientId) {
  return stdioOnlyClients.includes(clientId);
}

// Helper to check if client requires remote (HTTPS)
export function isRemoteOnly(clientId) {
  return remoteOnlyClients.includes(clientId);
}
