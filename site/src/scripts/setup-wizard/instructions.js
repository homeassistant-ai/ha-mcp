import { base, state } from './state';

// Instruction blocks rendered above the config box, in page order. Each
// function appends zero or more blocks for the current wizard state.
// pushSettingsHint runs separately, after the client config, so its block
// lands below any steps the config builder adds.
export function buildInstructions(ctx, instructions) {
  pushGuideLink(ctx, instructions);
  pushPlatformInstall(ctx, instructions);
  pushDeployment(ctx, instructions);
  pushRemotePath(ctx, instructions);
  pushHttpNotes(ctx, instructions);
  pushStdioBridge(ctx, instructions);
  pushGeminiNotes(ctx, instructions);
}

function pushGuideLink(ctx, instructions) {
  const { isLocal } = ctx;
  // Add link to full guide for Claude Desktop + Local
  if (state.client?.id === 'claude-desktop' && isLocal) {
    const guideUrl = state.platform?.id === 'macos' ? `${base}/guide-macos` :
                     state.platform?.id === 'windows' ? `${base}/guide-windows` : null;
    if (guideUrl) {
      instructions.push(`<div class="instruction-block border-blue-700/50 bg-blue-900/10">
          <h3 class="instruction-title text-blue-300">Full Step-by-Step Guide Available</h3>
          <p class="text-slate-300 mb-3">For a complete guided walkthrough with screenshots and troubleshooting tips:</p>
          <a href="${guideUrl}" class="inline-block px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white font-medium rounded-lg transition-colors">
            View ${state.platform?.id === 'macos' ? 'macOS' : 'Windows'} Setup Guide →
          </a>
        </div>`);
    }
  }
}

function pushPlatformInstall(ctx, instructions) {
  const { isLocal, needsStdioBridge, derivedDeployment, platformId } = ctx;
  // Platform instructions (uv, Node.js or Docker) - shown only when the
  // platform step ran (stdio run OS, uvx server OS, or bridge client OS).
  const isDockerPlatform = platformId === 'docker';

  if (isDockerPlatform && isLocal) {
    instructions.push(`<div class="instruction-block">
        <h3 class="instruction-title">Docker Required</h3>
        <p class="text-slate-300">Make sure Docker or Docker Desktop is installed and running.</p>
      </div>`);
  } else if (platformId && !isDockerPlatform) {
    // [tool, needed, {os: [command, note HTML]}]; an empty command shows the note alone.
    const nodeNote = 'mcp-remote needs Node.js 18 or newer; <a href="https://nodejs.org/en/download" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">nodejs.org</a> has installers for every OS.';
    const installs = [
      ['uv', isLocal || derivedDeployment === 'uvx', {
        macos: ['brew install uv', 'Or: <code>curl -LsSf https://astral.sh/uv/install.sh | sh</code>'],
        linux: ['curl -LsSf https://astral.sh/uv/install.sh | sh', 'Or via your package manager: <code>pacman -S uv</code> (Arch), <code>apk add uv</code> (Alpine)'],
        windows: ['winget install --id=astral-sh.uv -e', ''],
      }],
      ['Node.js', needsStdioBridge, {
        macos: ['brew install node', nodeNote],
        linux: ['', `${nodeNote} Distribution packages can be older, so check with <code>node --version</code>.`],
        windows: ['winget install --id=OpenJS.NodeJS.LTS -e', nodeNote],
      }],
    ];
    for (const [tool, needed, byOs] of installs) {
      const [cmd, note] = byOs[platformId] || [];
      if (!needed || cmd === undefined) continue;
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Install ${tool} (${state.platform.name})</h3>
          ${cmd ? `<pre class="code-block"><code>${cmd}</code></pre>` : ''}
          ${note ? `<p class="text-xs text-slate-400 mt-2">${note}</p>` : ''}
        </div>`);
    }
  }
}

function pushDeployment(ctx, instructions) {
  const { isLocal, isNetwork, isRemote, derivedDeployment, platformId } = ctx;
  // Deployment instructions (for network/remote)
  if (derivedDeployment && !isLocal) {
    let deployInstr = '';
    // For remote connections, add secret path for security (not needed for webhook-proxy — handled automatically)
    const secretPathNote = (isRemote && state.remotePath !== 'webhook-proxy') ? `
          <div class="bg-red-900/30 border border-red-700/50 rounded-lg p-3 mt-3">
            <p class="text-red-300 text-sm font-medium">Security Warning</p>
            <p class="text-red-200/80 text-xs mt-1">When exposing to the internet, <strong>always use a secret path</strong> to prevent unauthorized access. Keep this path private!</p>
          </div>` : '';
    // For LAN (network) direct runs, the default /mcp is reachable from other machines at a predictable URL.
    // The local network is trusted by design, so this is a softer "recommended" note, not the red internet warning.
    const lanHardeningNote = (isNetwork && (derivedDeployment === 'uvx' || derivedDeployment === 'docker')) ? `
          <div class="bg-amber-900/30 border border-amber-700/50 rounded-lg p-3 mt-3">
            <p class="text-amber-300 text-sm font-medium">Recommended: secret path on the LAN</p>
            <p class="text-amber-200/80 text-xs mt-1">The local network is treated as trusted, so the default <code>/mcp</code> works for LAN use. For defense-in-depth, set a high-entropy <code>MCP_SECRET_PATH</code> so the endpoint isn't at a predictable URL — or bind <code>MCP_HOST=127.0.0.1</code> if only this machine connects. See the <a href="https://github.com/homeassistant-ai/ha-mcp/blob/master/SECURITY.md#local-network-is-the-trusted-zone-for-standard-mode" target="_blank" rel="noopener noreferrer" class="text-amber-300 hover:underline">threat model</a>.</p>
          </div>` : '';

    if (derivedDeployment === 'uvx') {
      const useSecretPath = isRemote && state.remotePath !== 'webhook-proxy';
      const serverPath = useSecretPath ? '/private_{{RANDOM_STRING}}' : '/mcp';
      const portNote = `<p class="text-xs text-slate-500 mt-2">Optional: Set <code>MCP_PORT</code> to change the default port (8086).</p>`;
      const healthzNote = `<p class="text-xs text-slate-500 mt-2">Optional: Set <code>MCP_HEALTHZ=true</code> to enable an unauthenticated <code>GET /healthz</code> liveness endpoint for uptime monitors and load balancers (off by default; the response never includes the MCP path).</p>`;
      if (platformId === 'windows') {
        const secretPathEnv = useSecretPath ? `$env:MCP_SECRET_PATH="/private_{{RANDOM_STRING}}"  # Keep this secret!\n` : '';
        deployInstr = `<div class="instruction-block">
          <h3 class="instruction-title">Start HTTP Server (uvx) - PowerShell</h3>
          <pre class="code-block"><code>$env:HOMEASSISTANT_URL="{{HOMEASSISTANT_URL}}"
$env:HOMEASSISTANT_TOKEN="{{HOMEASSISTANT_TOKEN}}"
${secretPathEnv}uvx --from ha-mcp@latest ha-mcp-web</code></pre>
          <p class="text-sm text-slate-400 mt-2">Server will be at: <code>http://YOUR_IP:8086${serverPath}</code></p>${portNote}${healthzNote}${secretPathNote}${lanHardeningNote}
        </div>`;
      } else {
        const secretPathEnv = useSecretPath ? `export MCP_SECRET_PATH=/private_{{RANDOM_STRING}}  # Keep this secret!\n` : '';
        deployInstr = `<div class="instruction-block">
          <h3 class="instruction-title">Start HTTP Server (uvx)</h3>
          <pre class="code-block"><code>export HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}
export HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}
${secretPathEnv}uvx --from ha-mcp@latest ha-mcp-web</code></pre>
          <p class="text-sm text-slate-400 mt-2">Server will be at: <code>http://YOUR_IP:8086${serverPath}</code></p>${portNote}${healthzNote}${secretPathNote}${lanHardeningNote}
        </div>`;
      }
    } else if (derivedDeployment === 'docker') {
      const useSecretPath = isRemote && state.remotePath !== 'webhook-proxy';
      const secretPathEnv = useSecretPath ? `-e MCP_SECRET_PATH=/private_{{RANDOM_STRING}}` : '';
      const serverPath = useSecretPath ? '/private_{{RANDOM_STRING}}' : '/mcp';
      const dockerPortNote = `<p class="text-xs text-slate-500 mt-2">Optional: Change <code>-p 8086:8086</code> to use a different port (e.g., <code>-p 9000:8086</code>).</p>`;
      const healthzNote = `<p class="text-xs text-slate-500 mt-2">Optional: Add <code>-e MCP_HEALTHZ=true</code> to enable an unauthenticated <code>GET /healthz</code> liveness endpoint for uptime monitors and load balancers (off by default; the response never includes the MCP path).</p>`;
      const persistenceNote = `<div class="bg-blue-900/20 border border-blue-700/50 rounded-lg p-3 mt-4">
            <p class="text-blue-300 text-sm font-medium">Keep the volume — it's what saves your settings</p>
            <p class="text-blue-200/80 text-xs mt-1">ha-mcp writes its tool configuration, feature flags, backup settings and OAuth client registrations to <code>~/.ha-mcp</code> inside the container. Without <code>-v ha-mcp-data:/home/mcpuser/.ha-mcp</code> that lives only in the container's writable layer, so every settings change is lost as soon as the container is re-created — which is exactly what updating the image requires, and several settings ask you to restart before they take effect.</p>
          </div>`;
      const composeSecretPathLine = useSecretPath
        ? `\n      - MCP_SECRET_PATH=/private_{{RANDOM_STRING}}`
        : '';
      deployInstr = `<div class="instruction-block">
          <h3 class="instruction-title">Start Docker Container</h3>
          <pre class="code-block"><code>docker run -d --name ha-mcp -p 8086:8086 -v ha-mcp-data:/home/mcpuser/.ha-mcp -e HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} -e HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}${secretPathEnv ? ' ' + secretPathEnv : ''} ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web</code></pre>
          <p class="text-sm text-slate-400 mt-2">Server will be at: <code>http://YOUR_IP:8086${serverPath}</code></p>${persistenceNote}${dockerPortNote}${healthzNote}${secretPathNote}${lanHardeningNote}
          <details class="mt-3">
            <summary class="cursor-pointer text-sm text-slate-300 font-medium hover:text-slate-100">Production hardening (docker compose)</summary>
            <p class="text-slate-300 text-sm mt-3 mb-2">A minimum-viable compose paired with an <code>.env</code> file:</p>
            <pre class="code-block"><code>services:
  ha-mcp:
    container_name: ha-mcp
    image: ghcr.io/homeassistant-ai/ha-mcp:latest
    command: ha-mcp-web
    ports:
      - "8086:8086"
    volumes:
      - ha-mcp-data:/home/mcpuser/.ha-mcp
    environment:
      - HOMEASSISTANT_URL=\${HOMEASSISTANT_URL}
      - HOMEASSISTANT_TOKEN=\${HOMEASSISTANT_TOKEN}${composeSecretPathLine}
    restart: unless-stopped

volumes:
  ha-mcp-data:
    name: ha-mcp-data</code></pre>
            <p class="text-slate-400 text-xs mt-2">The <code>name:</code> line matters: without it Compose prefixes the volume with your project name (<code>myproject_ha-mcp-data</code>), so it wouldn't be the same <code>ha-mcp-data</code> the <code>docker run</code> command above and the container-management commands below refer to.</p>
            <p class="text-slate-300 text-sm mt-3 mb-2">Layer these hardening directives onto the service block before exposing on a real network:</p>
            <pre class="code-block"><code>    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    deploy:
      resources:
        limits:
          memory: 256M</code></pre>
            <p class="text-slate-400 text-xs mt-2"><code>read_only: true</code> pairs with <code>tmpfs: /tmp</code> because the Python runtime writes temporary files there; <code>cap_drop: [ALL]</code> drops every Linux kernel capability the container would otherwise inherit; <code>no-new-privileges:true</code> blocks privilege escalation via setuid binaries. See Docker Compose's <a href="https://docs.docker.com/reference/compose-file/services/" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">services reference</a> for the full schema.</p>
            <p class="text-slate-400 text-xs mt-2">Keep the <code>ha-mcp-data</code> volume when you add <code>read_only: true</code> — a volume mount stays writable under a read-only root filesystem, and it's the only reason settings survive. Without it ha-mcp can't write to <code>~/.ha-mcp</code> under <code>read_only</code>, logs a warning, and falls back to a temporary directory that's wiped on every restart. To store the data somewhere other than <code>~/.ha-mcp</code>, mount your path and point <code>HA_MCP_CONFIG_DIR</code> at it. Whatever you mount has to be writable by the UID the container actually runs as: a named volume is initialised as UID 999 (the image's <code>mcpuser</code>) and needs nothing further, a host bind mount needs <code>chown 999:999</code>, and if you override the UID with <code>--user</code> you must chown the mounted directory to that UID instead — the volume stays owned by 999 otherwise, and ha-mcp falls back to temporary storage.</p>
          </details>
          <details class="mt-2">
            <summary class="cursor-pointer text-sm text-slate-300 font-medium hover:text-slate-100">Container management</summary>
            <div class="text-sm text-slate-300 space-y-1 mt-3">
              <div><code class="bg-slate-800 px-1 rounded">docker logs ha-mcp -f</code> <span class="text-slate-400">— follow server logs</span></div>
              <div><code class="bg-slate-800 px-1 rounded">docker stop ha-mcp</code> <span class="text-slate-400">— stop the container</span></div>
              <div><code class="bg-slate-800 px-1 rounded">docker rm ha-mcp</code> <span class="text-slate-400">— remove the stopped container (the <code>ha-mcp-data</code> volume survives)</span></div>
              <div><code class="bg-slate-800 px-1 rounded">docker pull ghcr.io/homeassistant-ai/ha-mcp:latest</code> <span class="text-slate-400">— pull latest image, then re-create the container to update; re-use the same <code>-v ha-mcp-data:/home/mcpuser/.ha-mcp</code> flag so your settings carry over</span></div>
              <div><code class="bg-slate-800 px-1 rounded">docker volume rm ha-mcp-data</code> <span class="text-slate-400">— delete the saved settings and start fresh</span></div>
            </div>
          </details>
        </div>`;
    } else if (derivedDeployment === 'ha-addon') {
      deployInstr = `<div class="instruction-block">
          <h3 class="instruction-title">Home Assistant app (add-on)</h3>
          <p class="text-slate-300 text-sm mb-3">The app auto-discovers the HA connection, generates a secure secret path, and configures the token — no manual setup needed.</p>
          <p class="text-slate-300 mb-3">1. Add the repository to Home Assistant:</p>
          <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-ai%2Fha-mcp" target="_blank" rel="noopener noreferrer" class="inline-block mb-3">
            <img src="https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg" alt="Add Repository" />
          </a>
          <p class="text-xs text-slate-500 mb-3">If that opens the App store without an add-repository dialog (a <a href="https://github.com/home-assistant/my.home-assistant.io/issues/698" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">known HA issue</a>), add it manually: <strong>Settings → Apps → Install app → ⋮ → Repositories</strong>, then paste <code>https://github.com/homeassistant-ai/ha-mcp</code>.</p>
          <p class="text-slate-300 mb-2">2. Install "Home Assistant MCP Server" from <strong>Settings → Apps → Install app</strong></p>
          <p class="text-slate-300 mb-2">3. Start the app, then open <strong>Settings → Apps → Home Assistant MCP Server → Logs</strong> tab (these are the app logs, not the main HA logs)</p>
          <p class="text-xs text-slate-500 mb-2">Home Assistant 2026.2 renamed "Add-ons" to "Apps" — on older versions this is <strong>Settings → Add-ons → Add-on store</strong>.</p>
          <p class="text-slate-300 mb-2">4. Copy the MCP server URL from the log line — it looks like:</p>
          <pre class="code-block text-xs"><code>MCP Server URL: http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw</code></pre>
          <p class="text-xs text-slate-500 mt-2">Defaults: port <code>9583</code>, 128-bit random secret path, persisted across restarts.</p>
          <div class="bg-amber-900/20 border border-amber-700/50 rounded-lg p-3 mt-3">
            <p class="text-amber-200/90 text-sm"><strong class="text-amber-300">No access token here.</strong> The app authenticates behind the secret path in the URL. Don't also configure the local/stdio version for the same client (a <code>uvx ha-mcp@latest</code> entry with <code>HOMEASSISTANT_URL</code> / <code>HOMEASSISTANT_TOKEN</code>) — running the app and the stdio version together is a known cause of connection hangs.</p>
          </div>
        </div>`;
    } else if (derivedDeployment === 'ha-component') {
      const componentUrlGuidance = isRemote
        ? (state.remotePath === 'builtin-webhook'
            ? `<div class="bg-slate-800/50 border border-slate-700/50 rounded-lg p-3 mt-3">
            <p class="text-slate-300 text-sm font-medium">Your connect URL (built-in webhook)</p>
            <p class="text-slate-400 text-xs mt-1">Use the webhook URL from the entry's Configure screen: <code class="bg-slate-800 px-1 rounded">https://&lt;your-ha-domain&gt;/api/webhook/&lt;webhook-id&gt;</code>. It works through Nabu Casa or any reverse proxy already pointed at Home Assistant — no separate tunnel or port forwarding. The webhook is off unless you chose a remote-access mode when adding the server entry; otherwise turn on <strong>Remote access via webhook</strong> in the integration options. To use your own domain, set <strong>External URL</strong> there.</p>
            <p class="text-slate-400 text-xs mt-2"><strong class="text-slate-300">Auth:</strong> <strong>Authentication mode</strong> — at the top of the integration's Options screen, directly under the connect URLs — set to <code class="bg-slate-800 px-1 rounded">ha_auth</code> requires signing in with your Home Assistant account instead of relying on the secret URL. With the secret URL (<code class="bg-slate-800 px-1 rounded">none</code>), an OAuth sign-in is only sent back to a callback on the <strong>Allowed OAuth callback URLs</strong> list, which starts with the common clients' callbacks. A third mode, <code class="bg-slate-800 px-1 rounded">legacy</code>, is available for clients whose OAuth needs <code class="bg-slate-800 px-1 rounded">ha_auth</code> doesn't cover — see the <a href="${base}/faq#copilot-cli-oauth-legacy" class="text-blue-400 hover:underline">FAQ</a>.</p>
          </div>`
            : `<div class="bg-slate-800/50 border border-slate-700/50 rounded-lg p-3 mt-3">
            <p class="text-slate-300 text-sm font-medium">Your connect URL (via tunnel / reverse proxy)</p>
            <p class="text-slate-400 text-xs mt-1">Point the tunnel or reverse proxy below at the component's own port (default <code class="bg-slate-800 px-1 rounded">9584</code>). Your MCP URL is then <code class="bg-slate-800 px-1 rounded">https://&lt;your-host&gt;/private_&lt;random&gt;</code> using the direct-access path from the entry's Configure screen. If the tunnel runs on another machine, set <strong>Network access</strong> to <strong>Local network</strong> in the integration options. For most setups the built-in webhook is simpler.</p>
          </div>`)
        : `<div class="bg-slate-800/50 border border-slate-700/50 rounded-lg p-3 mt-3">
            <p class="text-slate-300 text-sm font-medium">Your connect URL (local)</p>
            <p class="text-slate-400 text-xs mt-1">Use the direct LAN URL from the entry's Configure screen: <code class="bg-slate-800 px-1 rounded">http://&lt;ha-ip&gt;:9584/private_&lt;random&gt;</code>. New server entries listen on the Home Assistant machine only; to reach this URL from another device, set <strong>Network access</strong> to <strong>Local network</strong> in the integration options.</p>
            <p class="text-slate-400 text-xs mt-2"><strong class="text-slate-300">Want local-only?</strong> Turn off <strong>Remote access via webhook</strong> in the entry options — no webhook is registered at all; the direct port and sidebar panel keep working.</p>
          </div>`;
      deployInstr = `<div class="instruction-block">
          <h3 class="instruction-title">HA Custom Component (in-process server)</h3>
          <p class="text-slate-300 text-sm mb-3">Runs the full ha-mcp server inside Home Assistant. Works on every install type — Home Assistant OS, Supervised, Container, and Core.</p>
          <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=homeassistant-ai&repository=ha-mcp&category=integration" target="_blank" rel="noopener noreferrer" class="inline-block mb-3">
            <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open the HA-MCP Custom Component in HACS" />
          </a>
          <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
            <li>Install <strong>HA-MCP Custom Component</strong> from HACS, the Marketplace on Home Assistant 2026.11+ (or copy <code class="bg-slate-800 px-1 rounded">custom_components/ha_mcp_tools/</code> into <code class="bg-slate-800 px-1 rounded">config/custom_components/</code>), then restart Home Assistant.</li>
            <li>Go to <strong>Settings → Devices &amp; Services → Add Integration</strong>, search <strong>HA-MCP Custom Component</strong>, and choose <strong>HA-MCP Server</strong>.</li>
            <li>In the form, paste an <strong>Administrator access token</strong> (your profile → <strong>Security</strong> → <strong>Long-lived access tokens</strong> → <strong>Create token</strong>; your own works from anywhere if it leaks and the server's changes show under your name), or, safer, enter the user name and password of an account made just for HA-MCP under <strong>Settings → People</strong> with <strong>Local access only</strong> and <strong>Administrator</strong> on (never your own login), and HA-MCP creates its token. Then pick how clients may connect (this form is from version 9.0.0 (October 28, 2026); earlier versions show a shorter one). Both options start off, so a client on another device needs one of them: <strong>Remote access through Home Assistant</strong> — start with <strong>Secret URL</strong> (the connect URL is the credential, nothing else to set up; switch to <strong>Home Assistant sign-in</strong> later if you want a login) — and/or <strong>Network access: Local network</strong> for clients on your LAN. Leaving both off keeps the server reachable only from the Home Assistant machine. Submit — creating the entry starts the server (the first start takes a little longer while it installs the package).</li>
            <li>Copy the connect URL from the entry's <strong>Configure</strong> screen (a notification points you there).</li>
            <li>Paste it into ${state.client.name} using the config below.</li>
          </ol>
          ${componentUrlGuidance}
          <div class="bg-slate-800/50 border border-slate-700/50 rounded-lg p-3 mt-3">
            <p class="text-slate-300 text-sm font-medium">Settings: the HA-MCP sidebar panel</p>
            <p class="text-slate-400 text-xs mt-1">While the server runs, an admin-only <strong>HA-MCP</strong> panel appears in the Home Assistant sidebar — open it to enable, disable, and pin tools, toggle feature flags, and manage backups. No URL or secret needed.</p>
          </div>
          <p class="text-xs text-slate-500 mt-3"><a href="https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/in-process-server.md" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Full in-process server documentation →</a></p>
        </div>`;
    }
    instructions.push(deployInstr);
  }
}

function pushRemotePath(ctx, instructions) {
  const { isRemote, serverPort } = ctx;
  // Remote-path instructions (for remote scope). builtin-webhook is covered
  // by the ha-component block above, so it emits no separate block here.
  if (isRemote && state.remotePath) {
    let proxyInstr = '';
    if (state.remotePath === 'cloudflared') {
      proxyInstr = `<div class="instruction-block">
          <h3 class="instruction-title">Cloudflare Tunnel</h3>
          <p class="text-slate-400 text-xs mb-3">No port forwarding required, free tier available, works behind CGNAT. Optional: layer Cloudflare Access on top of the tunnel for SSO/policy-based authentication.</p>
          <p class="text-slate-300 mb-3">Install cloudflared:</p>
          <pre class="code-block"><code># macOS
brew install cloudflared

# Windows
winget install Cloudflare.cloudflared

# Linux (Debian/Ubuntu)
curl -L https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-archive-keyring.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared

# Or use Docker
docker run cloudflare/cloudflared:latest tunnel --url http://host.docker.internal:${serverPort}</code></pre>
          <p class="text-xs text-slate-500 mt-2 mb-3">More options: <a href="https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Cloudflare Downloads</a></p>
          <p class="text-slate-300 mb-2">Quick tunnel (temporary URL - changes on restart):</p>
          <pre class="code-block"><code>cloudflared tunnel --url http://localhost:${serverPort}</code></pre>
          <p class="text-sm text-slate-400 mt-2">This gives you a URL like <code>https://random-words.trycloudflare.com</code></p>
          <p class="text-sm text-brand font-medium">Your MCP URL: <code>https://random-words.trycloudflare.com/private_{{RANDOM_STRING}}</code></p>
          <p class="text-xs text-slate-500 mt-1">Use the secret path you set in the server config above</p>
          <div class="bg-blue-900/20 border border-blue-700/50 rounded-lg p-3 mt-4">
            <p class="text-blue-300 text-sm font-medium">Want a permanent URL?</p>
            <p class="text-blue-200/80 text-xs mt-1">Create a free <a href="https://dash.cloudflare.com/sign-up" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Cloudflare account</a> and set up a <a href="https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Named Tunnel</a> for a stable URL that persists across restarts.</p>
            <pre class="code-block mt-2 text-xs"><code># One-time setup (requires Cloudflare account)
cloudflared tunnel login
cloudflared tunnel create ha-mcp
cloudflared tunnel route dns ha-mcp mcp.yourdomain.com

# Run with permanent URL
cloudflared tunnel run ha-mcp</code></pre>
            <p class="text-blue-200/80 text-xs mt-3">Persistent tunnels also need a config file at <code class="bg-slate-800 px-1 rounded">~/.cloudflared/config.yml</code> mapping the hostname to the local port:</p>
            <pre class="code-block mt-2 text-xs"><code>tunnel: ha-mcp
credentials-file: /path/to/.cloudflared/&lt;tunnel-id&gt;.json

ingress:
  - hostname: ha-mcp.yourdomain.com
    service: http://localhost:${serverPort}
  - service: http_status:404</code></pre>
          </div>
          <details class="mt-4">
            <summary class="cursor-pointer text-sm text-slate-300 font-medium hover:text-slate-100">Running on Home Assistant OS? Use the Cloudflared app</summary>
            <p class="text-slate-300 text-sm mt-3 mb-2">If your ha-mcp is the HA app, install the Cloudflared app alongside it and route the tunnel to the MCP app's port:</p>
            <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbrenner-tobias%2Faddon-cloudflared" target="_blank" rel="noopener noreferrer" class="inline-block mt-1 mb-2">
              <img src="https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg" alt="Add Cloudflared App" />
            </a>
            <p class="text-slate-300 text-sm mb-2">In the Cloudflared app configuration:</p>
            <pre class="code-block text-xs"><code>additional_hosts:
  - hostname: ha-mcp.yourdomain.com
    service: http://homeassistant.local:9583</code></pre>
            <p class="text-xs text-slate-400 mt-2">Replace <code>homeassistant.local</code> with your HA host's IP or hostname — match the IP from the MCP app's log line (e.g. <code>http://192.168.1.100:9583/...</code>). The Cloudflared app tunnels from inside its own container, so <code>localhost</code> won't reach the MCP app.</p>
            <p class="text-xs text-slate-400 mt-2">Or, point the tunnel at the MCP app's container hostname (shown on the MCP Server app's <strong>Information</strong> page, e.g. <code>b37de126-ha-mcp</code>): <code>service: http://&lt;your-prefix&gt;-ha-mcp:9583</code>.</p>
          </details>
          <div class="bg-amber-900/20 border border-amber-700/50 rounded-lg p-3 mt-4">
            <p class="text-amber-300 text-sm font-medium">Disable "Block AI Training Bots"</p>
            <p class="text-amber-200/80 text-xs mt-1">This is the <strong>most common connection issue</strong> for Cloudflare users. If your LLM can't connect but the URL works in your browser, this setting is almost certainly the cause.</p>
            <ol class="text-amber-200/80 text-xs mt-2 space-y-1 list-decimal list-inside">
              <li>Log in to <a href="https://dash.cloudflare.com" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Cloudflare</a></li>
              <li>In the left sidebar, click <strong>Domains</strong>, then <strong>Overview</strong></li>
              <li>Click on the domain you use for connecting to Home Assistant</li>
              <li>On the right side, find <strong>"Control AI Crawlers"</strong></li>
              <li>Under <strong>"Block AI training bots"</strong>, open the dropdown</li>
              <li>Select <strong>"do not block (allow crawlers)"</strong></li>
            </ol>
            <p class="text-amber-200/80 text-xs mt-2"><a href="https://homeassistant-ai.github.io/ha-mcp/images/cloudflare-ai-crawlers-setting.jpg" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Screenshot of the setting</a></p>
            <p class="text-amber-200/80 text-xs mt-2"><strong>Also check geo / country blocking:</strong> most AI/LLM services connect from US-based cloud infrastructure, so blocking US IPs (or allowing only your own country) also blocks your client. Allow your AI provider's IP ranges — Claude.ai connects from Anthropic's network, <code>160.79.104.0/21</code>.</p>
          </div>
        </div>`;
    } else if (state.remotePath === 'webhook-proxy') {
      proxyInstr = `<div class="instruction-block">
          <h3 class="instruction-title">Webhook Proxy App</h3>
          <p class="text-slate-300 mb-3">The Webhook Proxy app routes MCP traffic through your existing Home Assistant reverse proxy (Nabu Casa, Cloudflare, DuckDNS, nginx, etc.) — no separate tunnel to port 9583 needed.</p>
          <div class="mb-4 text-sm text-slate-400">
            <p class="font-medium text-slate-300 mb-1">How It Works</p>
            <p class="font-mono text-xs bg-slate-800/60 rounded p-2 mb-2">AI Client → HTTPS → Your Reverse Proxy → HA (port 8123) → Webhook (<code>/api/webhook/&lt;id&gt;</code>) → MCP App</p>
            <p><strong class="text-slate-300">Prerequisite:</strong> Requires a Nabu Casa subscription with Remote UI enabled, <em>or</em> another reverse proxy already pointing at your HA URL.</p>
          </div>
          <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
            <li><strong>Install the MCP Server app</strong> first (if not already installed):
              <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-ai%2Fha-mcp" target="_blank" rel="noopener noreferrer" class="inline-block mt-2 mb-2">
                <img src="https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg" alt="Add Repository" />
              </a>
            </li>
            <li><strong>Install the "Webhook Proxy for HA MCP"</strong> app from the same store</li>
            <li><strong>Start the app</strong> — it will ask you to restart Home Assistant</li>
            <li><strong>Restart Home Assistant</strong> (Settings &gt; System &gt; Restart)</li>
            <li><strong>Copy the remote URL</strong> from the webhook proxy app logs:
              <pre class="code-block mt-2"><code>MCP Server URL (remote): https://xxxxx.ui.nabu.casa/api/webhook/mcp_xxxxxxxx</code></pre>
            </li>
          </ol>
          <div class="mt-4 overflow-x-auto">
            <p class="text-sm font-medium text-slate-300 mb-2">Webhook Proxy vs Cloudflare Tunnel</p>
            <table class="w-full text-xs text-slate-400">
              <thead><tr class="border-b border-slate-700">
                <th class="text-left py-1 pr-4"></th>
                <th class="text-left py-1 pr-4 text-slate-300">Webhook Proxy</th>
                <th class="text-left py-1 text-slate-300">Cloudflare Tunnel</th>
              </tr></thead>
              <tbody>
                <tr class="border-b border-slate-800"><td class="py-1 pr-4 font-medium text-slate-300">Setup</td><td class="py-1 pr-4">Install app, restart HA</td><td class="py-1">Install cloudflared, configure tunnel</td></tr>
                <tr class="border-b border-slate-800"><td class="py-1 pr-4 font-medium text-slate-300">Cost</td><td class="py-1 pr-4">Existing reverse proxy / Nabu Casa (paid)</td><td class="py-1">Cloudflare account (free)</td></tr>
                <tr class="border-b border-slate-800"><td class="py-1 pr-4 font-medium text-slate-300">Routing</td><td class="py-1 pr-4">Through HA port 8123 (webhook)</td><td class="py-1">Direct to MCP port (9583)</td></tr>
                <tr><td class="py-1 pr-4 font-medium text-slate-300">Best for</td><td class="py-1 pr-4">Already have Nabu Casa / reverse proxy</td><td class="py-1">No existing proxy, want direct connection</td></tr>
              </tbody>
            </table>
          </div>
          <div class="bg-blue-900/20 border border-blue-700/50 rounded-lg p-3 mt-4">
            <p class="text-blue-300 text-sm font-medium">When to use this method</p>
            <p class="text-blue-200/80 text-xs mt-1">If you already have <strong>Nabu Casa</strong> or another reverse proxy pointing at your Home Assistant instance, the Webhook Proxy is the simplest remote setup — no Cloudflare account, no tunnel configuration, no extra ports.</p>
          </div>
          <div class="bg-slate-800/50 border border-slate-700/50 rounded-lg p-3 mt-3">
            <p class="text-slate-300 text-sm font-medium">Non-Nabu Casa users</p>
            <p class="text-slate-400 text-xs mt-1">If you use a custom reverse proxy (Cloudflare, DuckDNS, nginx, etc.), set <code class="bg-slate-800 px-1 rounded">remote_url</code> in the webhook proxy app configuration to your external HA URL (e.g., <code class="bg-slate-800 px-1 rounded">https://ha.example.com</code>).</p>
          </div>
          <div class="bg-amber-900/20 border border-amber-700/50 rounded-lg p-3 mt-3">
            <p class="text-amber-300 text-sm font-medium">Using Cloudflare in front of Home Assistant? Disable "Block AI Training Bots"</p>
            <p class="text-amber-200/80 text-xs mt-1">This is the <strong>most common connection issue</strong> for Cloudflare users. If your LLM can't connect but the URL works in your browser, this setting is almost certainly the cause.</p>
            <ol class="text-amber-200/80 text-xs mt-2 space-y-1 list-decimal list-inside">
              <li>Log in to <a href="https://dash.cloudflare.com" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Cloudflare</a></li>
              <li>In the left sidebar, click <strong>Domains</strong>, then <strong>Overview</strong></li>
              <li>Click on the domain you use for connecting to Home Assistant</li>
              <li>On the right side, find <strong>"Control AI Crawlers"</strong></li>
              <li>Under <strong>"Block AI training bots"</strong>, open the dropdown</li>
              <li>Select <strong>"do not block (allow crawlers)"</strong></li>
            </ol>
            <p class="text-amber-200/80 text-xs mt-2"><a href="https://homeassistant-ai.github.io/ha-mcp/images/cloudflare-ai-crawlers-setting.jpg" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Screenshot of the setting</a></p>
            <p class="text-amber-200/80 text-xs mt-2"><strong>Also check geo / country blocking</strong> (any reverse proxy, not just Cloudflare): most AI/LLM services connect from US-based cloud infrastructure, so blocking US IPs (or allowing only your own country) also blocks your client. Allow your AI provider's IP ranges — Claude.ai connects from Anthropic's network, <code>160.79.104.0/21</code>.</p>
          </div>
          <div class="bg-amber-900/20 border border-amber-700/50 rounded-lg p-3 mt-3">
            <p class="text-amber-300 text-sm font-medium">⚠️ Treat the webhook URL like a password</p>
            <p class="text-amber-200/80 text-xs mt-1">By default the webhook URL is <strong>unauthenticated</strong> — anyone with the full URL can reach your MCP server. Don't share it in screenshots, logs, or chat transcripts. If it leaks, rotate immediately: stop the app, delete <code class="bg-slate-800 px-1 rounded">/data/webhook_id.txt</code> from the app filesystem, restart, and copy the new URL into your client.</p>
          </div>
          <div class="bg-purple-900/20 border border-purple-700/50 rounded-lg p-3 mt-3">
            <p class="text-purple-300 text-sm font-medium">🔒 Optional: OAuth (Beta)</p>
            <p class="text-purple-200/80 text-xs mt-1 mb-2">For a real auth layer on top of the URL secret, enable <strong>OAuth (Beta)</strong> in the app configuration: toggle <em>Show unused optional configuration options</em>, set <strong>Enable OAuth</strong> to on, and restart the app. A first-time enable defaults to <strong>OAuth Mode</strong> <code class="bg-slate-800 px-1 rounded">ha_auth</code>: you sign in with your Home Assistant account and there is no Client ID or Secret — in Claude.ai keep the detected <strong>Always required</strong> + <strong>Use Anthropic's hosted client metadata</strong>. To use the older <code class="bg-slate-800 px-1 rounded">legacy</code> flow instead, set <strong>OAuth Mode</strong> to <code class="bg-slate-800 px-1 rounded">legacy</code> (leave Client ID and Client Secret blank) <em>before</em> restarting, and then also <strong>restart Home Assistant</strong> when the Repair prompts you — legacy mode's OAuth views load with Home Assistant, so restarting only the app is not enough; the app then generates a strong Client ID and Client Secret, prints them in the log, and persists them at <code class="bg-slate-800 px-1 rounded">/data/oauth_creds.json</code> — in Claude.ai choose <strong>Use your own OAuth client</strong>, paste them in, click <strong>Add</strong>, then <strong>Connect</strong>. When the toggle is off, the webhook behaves exactly as before with no auth.</p>
            <p class="text-purple-300 text-xs font-medium mt-2 mb-1">Flow when Claude.ai connects (legacy mode)</p>
            <p class="text-purple-200/80 text-xs mt-1 mb-2">Claude.ai redirects to <code class="bg-slate-800 px-1 rounded">/authorize</code> → you click <strong>Allow</strong> on the consent page → Claude.ai exchanges the auth code at <code class="bg-slate-800 px-1 rounded">/token</code> using your Client ID + Client Secret + PKCE → it gets a 1-hour bearer token (auto-refreshed via a 30-day refresh token). Every MCP request after that carries <code class="bg-slate-800 px-1 rounded">Authorization: Bearer ...</code>.</p>
            <p class="text-purple-300 text-xs font-medium mt-2 mb-1">Keep the Client Secret safe (legacy mode)</p>
            <p class="text-purple-200/80 text-xs mt-1 mb-2">The <code class="bg-slate-800 px-1 rounded">/authorize</code> consent page is reachable without being logged into Home Assistant — that's how OAuth works. The <strong>Client Secret</strong> at the token endpoint is what actually gates access; an attacker can't redeem a code without it. Don't paste it into screenshots/logs/chats. If you suspect it has leaked, rotate immediately.</p>
            <p class="text-purple-300 text-xs font-medium mt-2 mb-1">Rotating credentials (legacy mode)</p>
            <p class="text-purple-200/80 text-xs mt-1">Either set new values in the config fields, or flip <strong>Regenerate OAuth Credentials on Next Start</strong> on and restart — the app auto-clears the toggle after generating fresh values, then update Claude.ai with the new pair. Beta — works with Claude.ai today; other clients may vary.</p>
          </div>
        </div>`;
    } else if (state.remotePath === 'custom') {
      proxyInstr = `<div class="instruction-block">
          <h3 class="instruction-title">Custom Reverse Proxy</h3>
          <p class="text-slate-300 mb-2">Configure your reverse proxy (Caddy, Nginx, Traefik) to:</p>
          <ul class="text-sm text-slate-400 list-disc list-inside space-y-1">
            <li>Proxy requests to <code>http://localhost:${serverPort}</code></li>
            <li>Enable HTTPS with a valid certificate</li>
            <li>Pass through WebSocket connections</li>
          </ul>
          <div class="mt-3 text-xs text-slate-400 space-y-1">
            <p><strong class="text-slate-300">Caddy</strong> — automatic HTTPS via Let's Encrypt out of the box; requires a public IP or dynamic DNS for the ACME challenge.</p>
            <p><strong class="text-slate-300">Nginx + Let's Encrypt</strong> — traditional setup with full control; pair with <a href="https://certbot.eff.org/" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">certbot</a> for cert issuance and renewal.</p>
          </div>
          <div class="bg-amber-900/20 border border-amber-700/50 rounded-lg p-3 mt-3">
            <p class="text-amber-300 text-sm font-medium">Don't block your AI provider</p>
            <p class="text-amber-200/80 text-xs mt-1">Most AI/LLM services connect from US-based cloud infrastructure — geo/country blocking, WAF, or bot-filtering rules that drop US IPs will block your client even though the URL works in your browser. Allow your AI provider's IP ranges — Claude.ai connects from Anthropic's network, <code>160.79.104.0/21</code>. If Cloudflare fronts your proxy, also disable <strong>"Block AI training bots"</strong> (Domains &gt; Overview &gt; Control AI Crawlers).</p>
          </div>
        </div>`;
    }
    if (proxyInstr) instructions.push(proxyInstr);
  }
}

function pushHttpNotes(ctx, instructions) {
  const { isLocal } = ctx;
  // Verify by visiting URL in browser (for all HTTP-based setups)
  if (!isLocal) {
    instructions.push(`<div class="instruction-block border-green-700/50 bg-green-900/10">
        <h3 class="instruction-title text-green-300">Verify Your Server</h3>
        <p class="text-slate-300 text-sm">Visit your MCP server URL directly in your browser. If the server is running correctly, you'll see a message confirming <strong>"HA-MCP server is up and running"</strong> along with setup guidance. If you don't see this message, double-check your server URL, deployment, and proxy settings above.</p>
      </div>`);
  }

  // Transport disclaimer (all HTTP-based setups): Streamable HTTP is the modern standard
  // supported by virtually all current clients; the MCP spec has deprecated HTTP+SSE and
  // nothing here serves it, so an SSE-style client config gets a 405.
  if (!isLocal) {
    instructions.push(`<div class="instruction-block border-blue-700/50 bg-blue-900/10">
        <h3 class="instruction-title text-blue-300">Transport: use Streamable HTTP, not SSE</h3>
        <p class="text-slate-300 text-sm">The ha-mcp app (and <code class="bg-slate-800 px-1 rounded">ha-mcp-web</code>) serve <strong>Streamable HTTP</strong> — the modern default, supported by virtually all current MCP clients (Claude Code, Claude.ai, ChatGPT, Cursor, GitHub Copilot, and more). The config above already uses it.</p>
        <p class="text-slate-300 text-sm mt-2"><strong>SSE is not offered.</strong> The MCP spec has deprecated the HTTP+SSE transport, so an SSE / <code class="bg-slate-800 px-1 rounded">--transport sse</code> config pointed at the app returns a <code class="bg-slate-800 px-1 rounded">405</code> and won't connect — switch any older SSE-style client config over to Streamable HTTP.</p>
      </div>`);
  }
}

function pushStdioBridge(ctx, instructions) {
  const { isNetwork, needsStdioBridge } = ctx;
  // stdio bridge instructions for the stdio-only clients that actually run a
  // bridge. Zed is excluded from needsStdioBridge itself (it takes the URL
  // directly), so this check covers it without a special case here.
  if (needsStdioBridge) {
    const stdioBridgeInstr = `<div class="instruction-block border-yellow-700/50 bg-yellow-900/10">
        <h3 class="instruction-title text-yellow-300">stdio Bridge Required</h3>
        <p class="text-slate-300 mb-2">${state.client.name} only supports stdio. Use mcp-remote to connect to HTTP servers:</p>
        <pre class="code-block"><code># mcp-remote is run automatically via npx in the config below</code></pre>
        <p class="text-xs text-slate-400 mt-2">mcp-remote needs Node.js 18 or newer on this computer.${isNetwork ? ' The <code>--allow-http</code> flag lets it reach a plain <code>http://</code> URL on your local network; drop it if your URL starts with <code>https://</code>.' : ''} If you previously used another bridge, replace the whole server entry with the config below (<a href="${base}/faq#mcp-proxy-request-ctx" class="text-blue-400 hover:underline">migration details</a>).</p>
      </div>`;
    instructions.push(stdioBridgeInstr);
  }
}

function pushGeminiNotes(ctx, instructions) {
  const { isLocal } = ctx;
  // Client-specific notes for HTTP-based connections.
  // Gemini CLI: 405-on-GET caveat (a recurring confusion source — see #1027)
  // and the Home Assistant built-in MCP integration as an alternative to
  // running a separate ha-mcp container.
  if (state.client?.id === 'gemini-cli' && !isLocal) {
    instructions.push(`<div class="instruction-block border-blue-700/50 bg-blue-900/10">
        <h3 class="instruction-title text-blue-300">Gemini CLI Notes</h3>
        <p class="text-slate-300 text-sm mb-2"><strong>If client logs show <code>405 Method Not Allowed</code> on <code>GET /mcp</code>:</strong> the server is running in Streamable HTTP mode — switch from <code>url</code> to <code>httpUrl</code> in <code>~/.gemini/settings.json</code>. The config below already uses <code>httpUrl</code>.</p>
        <p class="text-slate-300 text-sm mb-2 mt-3">Entry point: <code>ha-mcp-web</code> (default) serves Streamable HTTP at <code>/mcp</code> on port 8086 — always pair it with the <code>httpUrl</code> key, not <code>url</code>.</p>
        <p class="text-slate-300 text-sm mb-2 mt-3"><strong>A direct browser/curl probe of <code>http://&lt;host&gt;:8086/mcp</code> also returns 405 by design</strong> — that is the <code>register_browser_landing</code> handler in <code>src/ha_mcp/__main__.py</code> confirming the server is up, not a fault. MCP clients use <code>POST</code>; this only shows up if a curl probe or browser hits the URL directly.</p>
        <p class="text-slate-300 text-sm mt-3"><strong>Alternative: HA built-in MCP integration.</strong> If you have the <a href="https://www.home-assistant.io/integrations/mcp_server/" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">Home Assistant MCP integration</a> enabled, you can skip <code>ha-mcp</code> entirely and point Gemini CLI at HA's <code>/api/mcp</code> endpoint with a long-lived access token in a <code>Bearer</code> header. HA core also serves the legacy SSE endpoint at <code>/mcp_server/sse</code> on the same host/port — same 405-on-GET caveat applies there.</p>
      </div>`);
  }
}

export function pushSettingsHint(ctx, instructions) {
  const { isStdio, isComponent, isAddon, isRemote, platformId } = ctx;
// ha-mcp ships a web settings page (enable/disable/pin tools, feature
// flags, advanced/beta settings, auto-backups, tool-approval prompts).
// Non-app HTTP/Docker users often don't know it exists or where the
// URL is (issue #1458), so surface a "what's next" hint for every
// deployment, with the access method matched to how it's running.
  let settingsHint = '';
  if (isComponent) {
    settingsHint = `<div class="instruction-block border-slate-700/50 bg-slate-800/30">
        <h3 class="instruction-title">After it's running: the HA-MCP sidebar panel</h3>
        <p class="text-slate-300 mb-2">While the in-process server runs, the component adds an admin-only <strong>HA-MCP</strong> panel to the Home Assistant sidebar where you can enable, disable, and pin MCP tools, toggle feature flags and advanced (beta) settings, manage automatic backups, and review tool-approval requests.</p>
        <p class="text-slate-400 text-sm">Open it from the sidebar &mdash; no URL or secret needed. See the <a href="${base}/faq#tool-settings-page" class="text-blue-400 hover:underline">FAQ</a>.</p>
      </div>`;
  } else if (isStdio) {
    const uiUrlPath = platformId === 'windows' ? '%USERPROFILE%\\.ha-mcp\\ui.url' : '~/.ha-mcp/ui.url';
    settingsHint = `<div class="instruction-block border-slate-700/50 bg-slate-800/30">
        <h3 class="instruction-title">After it's running: the settings page</h3>
        <p class="text-slate-300 mb-2">ha-mcp ships a local web settings page where you can enable, disable, and pin MCP tools, toggle feature flags and advanced (beta) settings, manage automatic backups, and review tool-approval requests. To find the URL, ask the AI something like <em>"how do I open the ha-mcp settings page?"</em> — it's included in <code class="bg-slate-800 px-1 rounded">ha_get_overview</code>'s response.</p>
        <p class="text-slate-400 text-sm">The URL is also written to <code class="bg-slate-800 px-1 rounded">${uiUrlPath}</code>. It's bound to <code class="bg-slate-800 px-1 rounded">127.0.0.1</code> and gated by a random secret path. See the <a href="${base}/faq#tool-settings-page" class="text-blue-400 hover:underline">FAQ</a> for all access methods and how to disable the sidecar.</p>
      </div>`;
  } else if (isAddon || state.remotePath === 'webhook-proxy') {
    settingsHint = `<div class="instruction-block border-slate-700/50 bg-slate-800/30">
        <h3 class="instruction-title">After it's running: the settings page</h3>
        <p class="text-slate-300 mb-2">The MCP Server app ships a web settings page where you can enable, disable, and pin MCP tools, toggle feature flags and advanced (beta) settings, manage automatic backups, and review tool-approval requests.</p>
        <p class="text-slate-400 text-sm">Open it with the <strong>"Open Web UI"</strong> button on the app page (Home Assistant ingress &mdash; no secret needed). For direct access <strong>on your trusted LAN</strong> it's at <code class="bg-slate-800 px-1 rounded">http://&lt;home-assistant-ip&gt;:9583/&lt;secret-path&gt;/settings</code> (find the secret path in the app's Configuration tab or <code class="bg-slate-800 px-1 rounded">/data/secret_path.txt</code>). That URL is plain HTTP and the secret path is the credential, so don't use it across an untrusted network &mdash; reach it remotely through ingress or an HTTPS reverse proxy. See the <a href="${base}/faq#tool-settings-page" class="text-blue-400 hover:underline">FAQ</a>.</p>
      </div>`;
  } else if (isRemote) {
    settingsHint = `<div class="instruction-block border-slate-700/50 bg-slate-800/30">
        <h3 class="instruction-title">After it's running: the settings page</h3>
        <p class="text-slate-300 mb-2">ha-mcp ships a web settings page where you can enable, disable, and pin MCP tools, toggle feature flags and advanced (beta) settings, manage automatic backups, and review tool-approval requests.</p>
        <p class="text-slate-400 text-sm">It's served under the same secret path as your MCP endpoint &mdash; append <code class="bg-slate-800 px-1 rounded">/settings</code> to your public server URL using the secret path you set above, e.g. <code class="bg-slate-800 px-1 rounded">https://&lt;your-host&gt;/&lt;secret-path&gt;/settings</code>. See the <a href="${base}/faq#tool-settings-page" class="text-blue-400 hover:underline">FAQ</a>.</p>
      </div>`;
  } else {
    settingsHint = `<div class="instruction-block border-slate-700/50 bg-slate-800/30">
        <h3 class="instruction-title">After it's running: the settings page</h3>
        <p class="text-slate-300 mb-2">ha-mcp ships a web settings page where you can enable, disable, and pin MCP tools, toggle feature flags and advanced (beta) settings, manage automatic backups, and review tool-approval requests.</p>
        <p class="text-slate-400 text-sm">It's served by your MCP server under the same secret path as the MCP endpoint &mdash; append <code class="bg-slate-800 px-1 rounded">/settings</code> to your server URL (e.g. <code class="bg-slate-800 px-1 rounded">http://YOUR_IP:8086/mcp/settings</code> with the default secret path). See the <a href="${base}/faq#tool-settings-page" class="text-blue-400 hover:underline">FAQ</a>.</p>
      </div>`;
  }
  instructions.push(settingsHint);
}
