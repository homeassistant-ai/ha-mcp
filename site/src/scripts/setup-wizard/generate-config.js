import { state, methodLabels, scopeLabels, remotePathLabels, needsPlatform, isStdioOnly } from './state';
import { buildInstructions, pushSettingsHint } from './instructions';
import { buildClientConfig } from './client-configs';

export function generateConfig() {
  const summaryEl = document.getElementById('config-summary');
  const instructionsEl = document.getElementById('setup-instructions');
  const configEl = document.getElementById('config-output').querySelector('code');
  const hintsEl = document.getElementById('replace-hints');
  const notesEl = document.getElementById('config-notes');

  const method = state.method;
  const isStdio = method === 'stdio-local';
  const isComponent = method === 'ha-component';
  const isAddon = method === 'ha-addon';

  // Semantic aliases kept from the old three-way connection model so the
  // large per-client/proxy templates below need no churn:
  //   isLocal   = the stdio/local run (old "local")
  //   isNetwork = HTTP server on the LAN (old "network")
  //   isRemote  = over-the-internet HTTPS (old "remote")
  const scope = isStdio ? 'local' : state.scope;
  const isLocal = isStdio;
  const isNetwork = !isStdio && scope === 'local';
  const isRemote = !isStdio && scope === 'remote';
  // Zed is not in stdioOnlyClients (it has native Streamable HTTP support),
  // so it never reaches needsStdioBridge. isZed exists purely to select its
  // own `context_servers` JSON shape below, independent of that flag.
  const isZed = state.client?.id === 'zed';
  const needsStdioBridge = isStdioOnly(state.client?.id) && !isStdio;

  const derivedDeployment = isStdio ? null : method; // ha-component | ha-addon | docker | uvx
  const derivedPlatform = state.platform?.id || null;
  // Port the tunnel/proxy instructions target: component 9584, app 9583,
  // docker/uvx HTTP server 8086.
  const serverPort = isComponent ? '9584' : isAddon ? '9583' : '8086';

  // Build summary badges
  const badges = [];
  if (state.method) badges.push(`<span class="px-3 py-1 rounded-full bg-orange-900/50 text-orange-300">${methodLabels[state.method] || state.method}</span>`);
  if (state.client) badges.push(`<span class="px-3 py-1 rounded-full bg-blue-900/50 text-blue-300">${state.client.name}</span>`);
  if (state.scope) badges.push(`<span class="px-3 py-1 rounded-full bg-green-900/50 text-green-300">${scopeLabels[state.scope] || state.scope}</span>`);
  if (state.platform && needsPlatform()) badges.push(`<span class="px-3 py-1 rounded-full bg-purple-900/50 text-purple-300">${state.platform.name}</span>`);
  if (state.remotePath) badges.push(`<span class="px-3 py-1 rounded-full bg-cyan-900/50 text-cyan-300">${remotePathLabels[state.remotePath] || state.remotePath}</span>`);
  summaryEl.innerHTML = badges.join('');

  // Build instructions
  const instructions = [];
  let hints = [];
  let notes = '';
  // isDocker is the local stdio "docker run --rm -i" shape; only the stdio path offers it.
  const isDocker = isStdio && state.platform?.id === 'docker';
  const mcpUrl = '{{MCP_SERVER_URL}}';
  const ctx = {
    method, isStdio, isComponent, isAddon, scope, isLocal, isNetwork, isRemote, isZed,
    needsStdioBridge, derivedDeployment, platformId: derivedPlatform, serverPort, isDocker, mcpUrl,
  };
  buildInstructions(ctx, instructions);
  const config = buildClientConfig(ctx, instructions);
  pushSettingsHint(ctx, instructions);

  // Render all instructions (including UI client steps added during config generation)
  instructionsEl.innerHTML = instructions.join('');
  if (window.initCopyButtons) window.initCopyButtons();

  // Show/hide config code box depending on whether it's a UI-based client
  const configSection = document.getElementById('config-section');
  if (state.client.configFormat === 'ui') {
    configSection.classList.add('hidden');
  } else {
    configSection.classList.remove('hidden');
    configEl.textContent = config;
  }

  // Hints
  hints = [];
  if (isStdio || derivedDeployment === 'uvx' || derivedDeployment === 'docker') {
    // Docker-specific URL hint
    if (isDocker) {
      hints.push(`<li>Replace <code class="bg-amber-900/30 px-1 rounded">{{HOMEASSISTANT_URL}}</code> (braces and all) with <code>http://host.docker.internal:8123</code> if Home Assistant runs on your host machine. Docker Desktop resolves that name; on Linux Docker Engine add <code>--add-host=host.docker.internal:host-gateway</code> to the <code>docker run</code> command</li>`);
    } else {
      hints.push(`<li>Replace <code class="bg-amber-900/30 px-1 rounded">{{HOMEASSISTANT_URL}}</code> (braces and all) with your Home Assistant URL (e.g., <code>http://homeassistant.local:8123</code>)</li>`);
    }
    hints.push(`<li>Replace <code class="bg-amber-900/30 px-1 rounded">{{HOMEASSISTANT_TOKEN}}</code> (braces and all) with your long-lived access token (an administrator's is recommended)</li>`);
  }
  if (isRemote && (derivedDeployment === 'uvx' || derivedDeployment === 'docker') && state.remotePath !== 'webhook-proxy') {
    hints.push(`<li>Replace <code class="bg-red-900/30 px-1 rounded text-red-300">{{RANDOM_STRING}}</code> (braces and all) with a random secret string (e.g., <code>abc123xyz</code>) — <strong class="text-red-400">keep this private!</strong></li>`);
  }
  if (!isStdio) {
    const urlHint = isComponent
      ? "the connect URL from the HA-MCP Server entry's Configure screen"
      : state.remotePath === 'webhook-proxy'
        ? 'your webhook proxy URL (from the webhook proxy app logs)'
        : 'your MCP server URL (from the deployment step above)';
    hints.push(`<li>Replace <code class="bg-amber-900/30 px-1 rounded">{{MCP_SERVER_URL}}</code> (braces and all) with ${urlHint}</li>`);
  }
  hintsEl.innerHTML = hints.join('');

  // Notes
  if (state.client.configLocation) {
    const locations = state.client.configLocation.split('\n').map(l => l.trim()).filter(Boolean);
    if (locations.length === 1) {
      notes += `<p class="text-slate-400 mb-2"><strong>Config location:</strong> <code class="bg-slate-800 px-1 rounded">${locations[0]}</code></p>`;
    } else {
      const items = locations.map(l => `<li><code class="bg-slate-800 px-1 rounded">${l}</code></li>`).join('');
      notes += `<div class="text-slate-400 mb-2"><strong>Config location:</strong><ul class="ml-4 mt-1 list-disc">${items}</ul></div>`;
    }
  }
  if (state.client.clientNote) {
    notes += `<p class="text-amber-400 mb-2">${state.client.clientNote}</p>`;
  }
  if (state.client.httpNote && !isStdio) {
    notes += `<p class="text-amber-400">${state.client.httpNote}</p>`;
  }
  notesEl.innerHTML = notes;
}
