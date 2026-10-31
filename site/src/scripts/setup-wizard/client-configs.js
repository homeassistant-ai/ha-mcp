import { base, state } from './state';
import { pushUiClientSteps } from './ui-clients';

// PowerShell has no `\` line continuation, so Windows gets the command on
// one line; other shells get one argument group per line.
function shellCommand(parts, platformId) {
  return platformId === 'windows' ? parts.join(' ') : parts.join(' \\\n  ');
}

// The copy-pasteable client config (JSON / YAML / CLI) for the current wizard
// state, plus any instruction blocks a client needs next to it. Returns the
// config text; UI-driven clients return an empty string.
export function buildClientConfig(ctx, instructions) {
  const { isStdio, isNetwork, isZed, needsStdioBridge, isDocker, mcpUrl, platformId } = ctx;
  let config = '';
  // Docker config for local stdio. `--rm` discards the container after every
  // client session, so the ha-mcp-data volume is the only thing keeping tool
  // config and feature flags across restarts (issue #2078).
  const dockerConfig = {
    command: "docker",
    args: [
      "run", "--rm", "-i",
      "-v", "ha-mcp-data:/home/mcpuser/.ha-mcp",
      "-e", "HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}",
      "-e", "HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}",
      "ghcr.io/homeassistant-ai/ha-mcp:latest"
    ]
  };

  // uvx config for local stdio
  const uvxConfig = {
    command: "uvx",
    args: ["ha-mcp@latest"],
    env: {
      HOMEASSISTANT_URL: "{{HOMEASSISTANT_URL}}",
      HOMEASSISTANT_TOKEN: "{{HOMEASSISTANT_TOKEN}}"
    }
  };

  const stdioConfig = isDocker ? dockerConfig : uvxConfig;

  if (state.client.configFormat === 'json') {
    // VS Code uses different structure: mcp.servers instead of mcpServers
    const isVSCode = state.client.id === 'vscode';
    const isOpenCode = state.client.id === 'opencode';

    if (isStdio) {
      if (isVSCode) {
        config = JSON.stringify({
          mcp: {
            servers: {
              "home-assistant": stdioConfig
            }
          }
        }, null, 2);
      } else if (isZed) {
        // Zed uses context_servers key
        config = JSON.stringify({
          context_servers: {
            "home-assistant": stdioConfig
          }
        }, null, 2);
      } else if (isOpenCode) {
        // OpenCode-specific stdio shape: type:"local", single command array, "environment" not "env".
        // The Docker branch below intentionally omits the top-level `environment` key (env vars are
        // inlined as `-e KEY=VAL` in the command array), so the asymmetry is deliberate.
        const opencodeStdio = isDocker
          ? {
              type: "local",
              command: [
                "docker", "run", "--rm", "-i",
                "-v", "ha-mcp-data:/home/mcpuser/.ha-mcp",
                "-e", "HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}",
                "-e", "HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}",
                "ghcr.io/homeassistant-ai/ha-mcp:latest"
              ],
              enabled: true
            }
          : {
              type: "local",
              command: ["uvx", "ha-mcp@latest"],
              enabled: true,
              environment: {
                HOMEASSISTANT_URL: "{{HOMEASSISTANT_URL}}",
                HOMEASSISTANT_TOKEN: "{{HOMEASSISTANT_TOKEN}}"
              }
            };
        config = JSON.stringify({
          "$schema": "https://opencode.ai/config.json",
          mcp: {
            "home-assistant": opencodeStdio
          }
        }, null, 2);
      } else if (state.client.id === 'github-copilot-agents') {
        // GitHub Copilot Agents reads .vscode/mcp.json with a bare top-level "servers" key
        // (no "mcpServers" wrapper, no "mcp" wrapper — those are different config files).
        config = JSON.stringify({
          servers: {
            "home-assistant": stdioConfig
          }
        }, null, 2);
      } else if (state.client.id === 'antigravity') {
        // Antigravity: add FASTMCP_SHOW_SERVER_BANNER=false to suppress the startup banner
        // that triggers "Unexpected server output" errors in Antigravity.
        let antigravityStdio;
        if (isDocker) {
          antigravityStdio = {
            ...dockerConfig,
            args: [
              ...dockerConfig.args.slice(0, -1),
              "-e", "FASTMCP_SHOW_SERVER_BANNER=false",
              dockerConfig.args[dockerConfig.args.length - 1]
            ]
          };
        } else {
          antigravityStdio = {
            ...uvxConfig,
            env: {
              ...uvxConfig.env,
              FASTMCP_SHOW_SERVER_BANNER: "false"
            }
          };
        }
        config = JSON.stringify({
          mcpServers: {
            "home-assistant": antigravityStdio
          }
        }, null, 2);
      } else {
        config = JSON.stringify({
          mcpServers: {
            "home-assistant": stdioConfig
          }
        }, null, 2);
      }
    } else if (needsStdioBridge) {
      // mcp-remote refuses plain HTTP to anything but loopback unless
      // --allow-http is passed. LAN endpoints are http://; every remote
      // path ends in an HTTPS front-end, so the flag stays off there.
      config = JSON.stringify({
        mcpServers: {
          "home-assistant": {
            command: "npx",
            args: ["-y", "mcp-remote", mcpUrl, ...(isNetwork ? ["--allow-http"] : [])]
          }
        }
      }, null, 2);
    } else {
      // HTTP-native client
      if (state.client.id === 'cline') {
        config = JSON.stringify({
          mcpServers: {
            "home-assistant": {
              url: mcpUrl,
              type: "streamableHttp"
            }
          }
        }, null, 2);
      } else if (state.client.id === 'windsurf' || state.client.id === 'antigravity') {
        config = JSON.stringify({
          mcpServers: {
            "home-assistant": {
              serverUrl: mcpUrl
            }
          }
        }, null, 2);
      } else if (state.client.id === 'gemini-cli') {
        // Gemini CLI uses 'httpUrl' for HTTP streaming
        config = JSON.stringify({
          mcpServers: {
            "home-assistant": {
              httpUrl: mcpUrl
            }
          }
        }, null, 2);
      } else if (isVSCode) {
        // VS Code settings.json uses an "mcp.servers" block; each server uses a "type"
        // field ("http" for Streamable HTTP) — confirmed against VS Code MCP docs.
        config = JSON.stringify({
          mcp: {
            servers: {
              "home-assistant": {
                type: "http",
                url: mcpUrl
              }
            }
          }
        }, null, 2);
      } else if (isZed) {
        // Zed uses context_servers with url key
        config = JSON.stringify({
          context_servers: {
            "home-assistant": {
              url: mcpUrl
            }
          }
        }, null, 2);
      } else if (isOpenCode) {
        // OpenCode: type:"remote" under mcp key
        config = JSON.stringify({
          "$schema": "https://opencode.ai/config.json",
          mcp: {
            "home-assistant": {
              type: "remote",
              url: mcpUrl,
              enabled: true
            }
          }
        }, null, 2);
      } else if (state.client.id === 'github-copilot-agents') {
        // GitHub Copilot Agents reads .vscode/mcp.json with a bare top-level "servers" key;
        // each server uses a "type" field ("http" for Streamable HTTP, "sse" for legacy SSE)
        // — confirmed against VS Code + GitHub coding-agent MCP docs. The app serves
        // Streamable HTTP only, so use type "http" (an sse config against it returns 405).
        config = JSON.stringify({
          servers: {
            "home-assistant": {
              type: "http",
              url: mcpUrl
            }
          }
        }, null, 2);
      } else if (state.client.id === 'cursor' || state.client.id === 'jetbrains') {
        // Both take a bare url under mcpServers, no type/transport field.
        // Cursor's .cursor/mcp.json auto-detects Streamable HTTP; JetBrains'
        // MCP Servers panel reads this shape directly for its HTTP transport
        // option. Confirmed against Cursor MCP docs and
        // jetbrains.com/help/ai-assistant/mcp.html (that page does not state
        // a minimum version for HTTP support specifically).
        config = JSON.stringify({
          mcpServers: {
            "home-assistant": {
              url: mcpUrl
            }
          }
        }, null, 2);
      } else {
        // Generic fallback for any JSON client without a dedicated branch
        // (e.g. Raycast): mcpServers + a "type" field set to "http" for Streamable
        // HTTP (the documented key for these clients — not "transport").
        config = JSON.stringify({
          mcpServers: {
            "home-assistant": {
              type: "http",
              url: mcpUrl
            }
          }
        }, null, 2);
      }
    }

    // VS Code: alternative secure-input-prompts config (stdio path only — HTTP path uses a different shape)
    if (state.client.id === 'vscode' && isStdio) {
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Alternative: Secure Input Prompts</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <p class="text-slate-400">VS Code can prompt you for credentials at first use instead of storing them in the config file. Replace the config above with:</p>
            <pre class="bg-slate-800 rounded p-2 text-xs overflow-x-auto">{
  "mcp": {
    "inputs": [
      {
        "type": "promptString",
        "id": "ha-url",
        "description": "Home Assistant URL",
        "default": "http://homeassistant.local:8123"
      },
      {
        "type": "promptString",
        "id": "ha-token",
        "description": "Long-lived access token",
        "password": true
      }
    ],
    "servers": {
      "home-assistant": {
        "command": "uvx",
        "args": ["ha-mcp@latest"],
        "env": {
          "HOMEASSISTANT_URL": "\${input:ha-url}",
          "HOMEASSISTANT_TOKEN": "\${input:ha-token}"
        }
      }
    }
  }
}</pre>
            <p class="text-slate-400">VS Code will prompt for the URL and token on first use and store them securely.</p>
            <p class="text-slate-400 mt-2">VS Code also accepts a one-click install deep-link of the form <code class="bg-slate-800 px-1 rounded">vscode:mcp/install?&lt;url-encoded-config-json&gt;</code> — useful for sharing configs in docs.</p>
          </div>
        </div>`);
    }

    // GitHub Copilot Agents: alternative secure-input-prompts config — only the stdio shape uses inputs[]
    // (HTTP/SSE config is a url + transport pair with no command/env to substitute into).
    if (state.client.id === 'github-copilot-agents' && isStdio) {
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Alternative: Secure Input Prompts</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <p class="text-slate-400">GitHub Copilot Agents supports an <code class="bg-slate-800 px-1 rounded">inputs</code> array that prompts for credentials at first use instead of storing them in the file. Replace the config above with:</p>
            <pre class="bg-slate-800 rounded p-2 text-xs overflow-x-auto">{
  "inputs": [
    {
      "type": "promptString",
      "id": "ha-url",
      "description": "Home Assistant URL",
      "default": "http://homeassistant.local:8123"
    },
    {
      "type": "promptString",
      "id": "ha-token",
      "description": "Long-lived access token",
      "password": true
    }
  ],
  "servers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "\${input:ha-url}",
        "HOMEASSISTANT_TOKEN": "\${input:ha-token}"
      }
    }
  }
}</pre>
            <p class="text-slate-400"><strong>Note:</strong> The wizard above emits the <code class="bg-slate-800 px-1 rounded">.vscode/mcp.json</code> (repository) shape with a bare top-level <code class="bg-slate-800 px-1 rounded">servers</code> key. If you prefer to configure this in your personal VS Code <code class="bg-slate-800 px-1 rounded">settings.json</code> instead, wrap the entire object in an additional <code class="bg-slate-800 px-1 rounded">"mcp": { ... }</code> key.</p>
          </div>
        </div>`);
    }

    // GitHub Copilot Agents: organization-deployment notes (apply to any transport)
    if (state.client.id === 'github-copilot-agents') {
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Org &amp; Repository Deployment</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <p class="text-slate-400"><strong class="text-slate-300">Repository-wide config:</strong> on GitHub, navigate to <strong>Settings → Copilot → Coding agent → MCP configuration</strong> (the menu node is sometimes labeled <em>Cloud agent</em> depending on UI version). The MCP config you set there applies to all users who have access to the repo, instead of (or in addition to) the per-user <code class="bg-slate-800 px-1 rounded">.vscode/mcp.json</code> shown above.</p>
            <p class="text-slate-400"><strong class="text-slate-300">Org / enterprise prerequisite:</strong> the <em>"MCP servers in Copilot"</em> policy must be enabled for your organization or enterprise. If config changes don't take effect for some users, check the org-level Copilot policy first — your admin may have it scoped or disabled.</p>
          </div>
        </div>`);
    }

    // Antigravity: UI nav steps + HTTP caveat
    if (state.client.id === 'antigravity') {
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Accessing the MCP Config Editor</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <p class="text-slate-400">To open the raw config editor in Antigravity:</p>
            <ol class="list-decimal list-inside space-y-1 text-slate-300">
              <li>Click the <strong>...</strong> menu in the Agent pane</li>
              <li>Select <strong>MCP Servers</strong> to open the MCP Store</li>
              <li>Click <strong>Manage MCP Servers</strong> at the top</li>
              <li>Click <strong>View raw config</strong> to edit <code class="bg-slate-800 px-1 rounded">mcp_config.json</code></li>
            </ol>
            ${!isStdio ? `<p class="mt-2 text-amber-400"><strong>Note:</strong> Antigravity's built-in HTTP client can fail with <code class="bg-slate-800 px-1 rounded">sending "subscriptions/listen": … session not found</code>, "connection closed", or "SSE stream failed to reconnect" errors — if you hit those, connect through the <code class="bg-slate-800 px-1 rounded">mcp-remote</code> stdio bridge instead (<a href="${base}/faq#antigravity-troubleshooting" class="text-blue-400 hover:underline">details</a>).</p>` : ''}
          </div>
        </div>`);
    }

    // OpenCode: management commands + corrective notes for users coming from other clients
    if (state.client.id === 'opencode') {
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Management Commands &amp; Quirks</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <div><code class="bg-slate-800 px-1 rounded">opencode mcp list</code> <span class="text-slate-400">— list configured servers and their auth status</span></div>
            <div><code class="bg-slate-800 px-1 rounded">opencode mcp auth home-assistant</code> <span class="text-slate-400">— trigger or refresh authentication</span></div>
            <div><code class="bg-slate-800 px-1 rounded">opencode mcp logout home-assistant</code> <span class="text-slate-400">— clear stored credentials</span></div>
            <div><code class="bg-slate-800 px-1 rounded">opencode mcp debug home-assistant</code> <span class="text-slate-400">— diagnose connection issues</span></div>
            <div class="mt-2 text-slate-400"><strong>OpenCode config quirks:</strong> The top-level key is <code class="bg-slate-800 px-1 rounded">mcp</code> (not <code class="bg-slate-800 px-1 rounded">mcpServers</code>). Local servers use a single <code class="bg-slate-800 px-1 rounded">command</code> array — there is no separate <code class="bg-slate-800 px-1 rounded">args</code> field. Environment variables go under <code class="bg-slate-800 px-1 rounded">environment</code> (not <code class="bg-slate-800 px-1 rounded">env</code>).</div>
            <div class="mt-2 text-slate-400"><strong>Config file path override:</strong> Set <code class="bg-slate-800 px-1 rounded">OPENCODE_CONFIG=/path/to/file.json</code> to load a custom config file location.</div>
            <div class="mt-2 text-slate-400"><strong>Config precedence:</strong> A project-level <code class="bg-slate-800 px-1 rounded">opencode.json</code> overrides the global <code class="bg-slate-800 px-1 rounded">~/.config/opencode/opencode.json</code>. See the <a href="https://opencode.ai/docs/config/#precedence-order" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">OpenCode precedence docs</a> for full details.</div>
            <div class="mt-2 text-slate-400"><strong>Custom headers &amp; secret interpolation:</strong> Use <code class="bg-slate-800 px-1 rounded">{env:VAR}</code> in header values — OpenCode reads the env var at runtime without storing it in the file. Example: <code class="bg-slate-800 px-1 rounded">"Authorization": "Bearer {env:HA_TOKEN}"</code>.</div>
            <div class="mt-2 text-slate-400"><strong>Suppressing auto-OAuth:</strong> OpenCode handles OAuth via Dynamic Client Registration automatically on remote endpoints. Add <code class="bg-slate-800 px-1 rounded">"oauth": false</code> to the server entry to opt out.</div>
            <div class="mt-2 text-slate-400"><strong>Tool namespacing:</strong> ha-mcp tools appear as <code class="bg-slate-800 px-1 rounded">home-assistant_*</code> in OpenCode. You can enable or disable the namespace per-agent via OpenCode's per-agent namespace toggles.</div>
            <div class="mt-2 text-slate-400"><strong>Context size:</strong> ha-mcp adds 87 tools to the prompt context, which is non-trivial. Consider using a dedicated OpenCode agent for HA-heavy workflows to keep other agents lean.</div>
          </div>
        </div>`);
    }

  } else if (state.client.configFormat === 'yaml' && state.client.id === 'hermes') {
    // Hermes keeps its servers in ~/.hermes/config.yaml under `mcp_servers`,
    // a mapping keyed by server name — not Continue's list of
    // {name, type, url} entries, so it needs its own YAML shape here.
    if (isStdio) {
      if (isDocker) {
        config = `mcp_servers:
  home-assistant:
    command: "docker"
    args:
      - "run"
      - "--rm"
      - "-i"
      - "-v"
      - "ha-mcp-data:/home/mcpuser/.ha-mcp"
      - "-e"
      - "HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}"
      - "-e"
      - "HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}"
      - "ghcr.io/homeassistant-ai/ha-mcp:latest"`;
      } else {
        config = `mcp_servers:
  home-assistant:
    command: "uvx"
    args:
      - "ha-mcp@latest"
    env:
      HOMEASSISTANT_URL: "{{HOMEASSISTANT_URL}}"
      HOMEASSISTANT_TOKEN: "{{HOMEASSISTANT_TOKEN}}"`;
      }
    } else {
      config = `mcp_servers:
  home-assistant:
    url: "${mcpUrl}"`;
    }
    if (!isStdio) {
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Alternative: With Authentication Headers</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <p class="text-slate-400">If your endpoint is behind a Bearer token, add a <code class="bg-slate-800 px-1 rounded">headers</code> block. Replace the config above with:</p>
            <pre class="bg-slate-800 rounded p-2 text-xs overflow-x-auto">mcp_servers:
  home-assistant:
    url: "${mcpUrl}"
    headers:
      Authorization: "Bearer \${env:HA_TOKEN}"</pre>
            <p class="text-slate-400">Hermes resolves <code class="bg-slate-800 px-1 rounded">\${env:VAR}</code> (and <code class="bg-slate-800 px-1 rounded">\${VAR}</code>) from <code class="bg-slate-800 px-1 rounded">~/.hermes/.env</code>, so the token stays out of the config file. For an endpoint that speaks OAuth instead, drop the headers and set <code class="bg-slate-800 px-1 rounded">auth: oauth</code> on the entry.</p>
            <p class="text-slate-400">Leave the transport at its default. Hermes can switch a server to SSE, but the ha-mcp endpoint is Streamable HTTP only — it is POST-only and answers an SSE-style pre-flight with <code class="bg-slate-800 px-1 rounded">405</code>.</p>
          </div>
        </div>`);
    }
    instructions.push(`<div class="instruction-block">
        <h3 class="instruction-title">Hermes Notes</h3>
        <div class="text-sm text-slate-400 space-y-2">
          <p><strong class="text-slate-300">Tool names:</strong> ha-mcp tools show up as <code class="bg-slate-800 px-1 rounded">mcp__home_assistant__&lt;tool&gt;</code> (Hermes replaces the hyphen in the server name with an underscore).</p>
          <p><strong class="text-slate-300">Trimming the catalog:</strong> <code class="bg-slate-800 px-1 rounded">tools.include</code> / <code class="bg-slate-800 px-1 rounded">tools.exclude</code> on the server entry take exact tool names or globs. Use the <strong>original</strong> ha-mcp names (e.g. <code class="bg-slate-800 px-1 rounded">ha_get_state</code>), not the prefixed ones.</p>
          <p><a href="https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">Hermes MCP docs →</a></p>
        </div>
      </div>`);
  } else if (state.client.configFormat === 'yaml') {
    // Continue uses YAML format
    if (isStdio) {
      if (isDocker) {
        config = `name: Home Assistant MCP
version: 0.0.1
schema: v1
mcpServers:
  - name: home-assistant
    command: docker
    args:
      - "run"
      - "--rm"
      - "-i"
      - "-v"
      - "ha-mcp-data:/home/mcpuser/.ha-mcp"
      - "-e"
      - "HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}"
      - "-e"
      - "HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}"
      - "ghcr.io/homeassistant-ai/ha-mcp:latest"`;
      } else {
        config = `name: Home Assistant MCP
version: 0.0.1
schema: v1
mcpServers:
  - name: home-assistant
    command: uvx
    args:
      - "ha-mcp@latest"
    env:
      HOMEASSISTANT_URL: "{{HOMEASSISTANT_URL}}"
      HOMEASSISTANT_TOKEN: "{{HOMEASSISTANT_TOKEN}}"`;
      }
    } else {
      config = `name: Home Assistant MCP
version: 0.0.1
schema: v1
mcpServers:
  - name: home-assistant
    type: streamable-http
    url: "${mcpUrl}"`;
    }
    // Continue: auth-headers alternative for authenticated remote servers
    if (!isStdio) {
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Alternative: With Authentication Headers</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <p class="text-slate-400">If your MCP server requires a Bearer token, add a <code class="bg-slate-800 px-1 rounded">requestOptions.headers</code> block. Replace the config above with:</p>
            <pre class="bg-slate-800 rounded p-2 text-xs overflow-x-auto">mcpServers:
  - name: home-assistant
    type: streamable-http
    url: "${mcpUrl}"
    requestOptions:
      headers:
        Authorization: "Bearer YOUR_TOKEN"</pre>
          </div>
        </div>`);
    }
  } else if (state.client.configFormat === 'cli') {
    if (state.client.id === 'gemini-cli') {
      // Gemini CLI
      if (isStdio) {
        if (isDocker) {
          config = shellCommand([
            'gemini mcp add --scope user home-assistant docker --',
            'run --rm -i',
            '-v ha-mcp-data:/home/mcpuser/.ha-mcp',
            '-e HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}',
            '-e HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}',
            'ghcr.io/homeassistant-ai/ha-mcp:latest',
          ], platformId);
        } else {
          config = shellCommand([
            'gemini mcp add --scope user home-assistant',
            '-e HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}',
            '-e HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}',
            'uvx -- ha-mcp@latest',
          ], platformId);
        }
      } else {
        config = `gemini mcp add --transport http home-assistant ${mcpUrl}`;
      }
      // Gemini CLI management commands
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Management Commands</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <div><code class="bg-slate-800 px-1 rounded">gemini mcp list</code> <span class="text-slate-400">— list configured servers</span></div>
            <div><code class="bg-slate-800 px-1 rounded">gemini mcp remove home-assistant</code> <span class="text-slate-400">— remove this server</span></div>
            <div class="mt-2 text-slate-400">Inside an interactive Gemini session: <code class="bg-slate-800 px-1 rounded">/mcp</code> shows status, <code class="bg-slate-800 px-1 rounded">/mcp refresh</code> reconnects after config changes.</div>
          </div>
        </div>`);
      // Gemini CLI: auth-headers alternative — manual settings.json edit
      if (!isStdio) {
        instructions.push(`<div class="instruction-block">
            <h3 class="instruction-title">Alternative: With Authentication Headers</h3>
            <div class="text-sm text-slate-300 space-y-2">
              <p class="text-slate-400">For Bearer-token auth, edit <code class="bg-slate-800 px-1 rounded">~/.gemini/settings.json</code> (or project-level <code class="bg-slate-800 px-1 rounded">.gemini/settings.json</code>) directly:</p>
              <pre class="bg-slate-800 rounded p-2 text-xs overflow-x-auto">{
  "mcpServers": {
    "home-assistant": {
      "httpUrl": "${mcpUrl}",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}</pre>
            </div>
          </div>`);
      }
    } else if (state.client.id === 'codex') {
      // Codex CLI
      if (isStdio) {
        if (isDocker) {
          config = `codex mcp add home-assistant -- docker run --rm -i -v ha-mcp-data:/home/mcpuser/.ha-mcp -e HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} -e HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}} ghcr.io/homeassistant-ai/ha-mcp:latest`;
        } else {
          config = `codex mcp add home-assistant --env HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} --env HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}} -- uvx ha-mcp@latest`;
        }
      } else {
        config = `codex mcp add home-assistant --url ${mcpUrl}`;
      }
      // Codex CLI management commands
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Management Commands</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <div><code class="bg-slate-800 px-1 rounded">codex mcp list</code> <span class="text-slate-400">— list configured servers</span></div>
            <div><code class="bg-slate-800 px-1 rounded">codex mcp get home-assistant</code> <span class="text-slate-400">— show server config</span></div>
            <div><code class="bg-slate-800 px-1 rounded">codex mcp remove home-assistant</code> <span class="text-slate-400">— remove this server</span></div>
            <div class="mt-2 text-slate-400">Codex stores config in TOML at <code class="bg-slate-800 px-1 rounded">~/.codex/config.toml</code> — shared between the CLI and the Codex Desktop app.</div>
            <div class="mt-2 text-slate-400"><strong class="text-slate-300">Codex Desktop UI:</strong> Go to <strong>Settings</strong> → <strong>MCP</strong> → <strong>Add Server</strong>, then choose type <strong>STDIO</strong> for local setup (or <strong>HTTP</strong> for remote). Fill in Name (<code class="bg-slate-800 px-1 rounded">home-assistant</code>), Command (<code class="bg-slate-800 px-1 rounded">uvx</code>), Args (<code class="bg-slate-800 px-1 rounded">ha-mcp@latest</code>), and Env vars (<code class="bg-slate-800 px-1 rounded">HOMEASSISTANT_URL</code> / <code class="bg-slate-800 px-1 rounded">HOMEASSISTANT_TOKEN</code>) — or paste the URL for HTTP. The desktop UI writes the same TOML as the CLI.</div>
            <div class="mt-2 text-slate-400"><strong class="text-slate-300">Codex cloud tasks with a firewalled server:</strong> local Codex reaches ha-mcp over your LAN, but OpenAI's cloud surfaces (Codex web/cloud tasks, ChatGPT connectors) need the server reachable from OpenAI. Instead of exposing a public URL, the community <a href="https://github.com/norpol/hass-codex-tunnel-mcp" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">OpenAI Tunnel for HA-MCP</a> integration connects your local ha-mcp URL to an OpenAI-hosted tunnel (outbound-only). See the <a href="${base}/faq#openai-tunnel" class="text-blue-400 hover:underline">FAQ</a>.</div>
            ${!isStdio ? `<div class="mt-2 text-slate-400">OAuth 2.0 authentication is available for remote (HTTP) servers.</div>` : ''}
          </div>
        </div>`);
      // Codex: auth-headers TOML alternative for authenticated remote servers
      if (!isStdio) {
        instructions.push(`<div class="instruction-block">
            <h3 class="instruction-title">Alternative: With Authentication Headers</h3>
            <div class="text-sm text-slate-300 space-y-2">
              <p class="text-slate-400">For Bearer-token auth, edit <code class="bg-slate-800 px-1 rounded">~/.codex/config.toml</code> directly:</p>
              <pre class="bg-slate-800 rounded p-2 text-xs overflow-x-auto">[mcp_servers.home-assistant]
url = "${mcpUrl}"
[mcp_servers.home-assistant.headers]
Authorization = "Bearer YOUR_TOKEN"</pre>
            </div>
          </div>`);
      }
    } else {
      // Claude Code
      if (isStdio) {
        if (isDocker) {
          config = shellCommand([
            'claude mcp add home-assistant',
            '-- docker run --rm -i',
            '-v ha-mcp-data:/home/mcpuser/.ha-mcp',
            '-e HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}',
            '-e HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}',
            'ghcr.io/homeassistant-ai/ha-mcp:latest',
          ], platformId);
        } else {
          config = shellCommand([
            'claude mcp add home-assistant',
            '--env HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}',
            '--env HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}',
            '-- uvx ha-mcp@latest',
          ], platformId);
        }
      } else {
        config = `claude mcp add --transport http home-assistant ${mcpUrl}`;
      }
      // Claude Code management commands
      instructions.push(`<div class="instruction-block">
          <h3 class="instruction-title">Management Commands</h3>
          <div class="text-sm text-slate-300 space-y-2">
            <div><code class="bg-slate-800 px-1 rounded">claude mcp list</code> <span class="text-slate-400">— list all configured MCP servers</span></div>
            <div><code class="bg-slate-800 px-1 rounded">claude mcp remove home-assistant</code> <span class="text-slate-400">— remove this server</span></div>
            <div class="mt-2 text-slate-400">Use <code class="bg-slate-800 px-1 rounded">--scope user</code> to share the server across all projects (default scope is local/project-only).</div>
          </div>
        </div>`);
    }
  } else if (state.client.configFormat === 'ui') {
    pushUiClientSteps(ctx, instructions);
  }
  return config;
}
