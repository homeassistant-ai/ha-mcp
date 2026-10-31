import { base, state } from './state';

// Clients configured through their own settings UI get step-by-step instruction
// blocks instead of a config file; the code box stays hidden for them.
export function pushUiClientSteps(ctx, instructions) {
  const { isStdio, isComponent } = ctx;
  let uiSteps = '';
  // Legacy OAuth (Gemini Spark, Copilot CLI HTTP) is served only by the
  // HA-MCP Server component's built-in webhook — its /authorize + /token
  // live on the Home Assistant host. The direct-port deployments (Docker,
  // uvx, app) and the component's cloudflared/custom tunnels target the
  // MCP port instead, so they cannot complete the flow. state.remotePath is
  // guaranteed set here (a remote client can't reach this step without one).
  const legacyOauthReady = isComponent && state.remotePath === 'builtin-webhook';
  const legacyMethodNotice = (clientName) => `<div class="instruction-block">
          <h3 class="instruction-title">${clientName} Configuration</h3>
          <div class="bg-amber-900/20 border border-amber-700/50 rounded-lg p-3">
            <p class="text-amber-300 text-sm font-medium">Pick the HA-MCP Server component with its built-in webhook first</p>
            <p class="text-amber-200/80 text-xs mt-1">${clientName} is OAuth-only and works only through the HA-MCP Server <strong>custom component</strong> in <code class="bg-slate-800 px-1 rounded">legacy</code> mode: its <code class="bg-slate-800 px-1 rounded">/authorize</code> and <code class="bg-slate-800 px-1 rounded">/token</code> endpoints are served on your Home Assistant host via the <strong>built-in webhook</strong>. The Docker, uvx, app, and cloudflared/custom deployments target the MCP port directly and don't expose that OAuth surface, so ${clientName} can't complete its flow with them. Go back to <strong>Choose your setup</strong>, select <strong>HA-MCP Server (custom component)</strong>, then the <strong>Built-in webhook</strong> remote path — the ${clientName} steps will appear here.</p>
          </div>
        </div>`;
  if (state.client.id === 'chatgpt') {
    uiSteps = `<div class="instruction-block">
          <h3 class="instruction-title">ChatGPT Configuration</h3>
          <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
            <li>Open ChatGPT → <strong>Settings</strong> → <strong>Connectors</strong></li>
            <li>Click <strong>Advanced settings</strong> → Enable <strong>Developer mode</strong></li>
            <li>Create custom connector:
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li>Name: <code class="bg-slate-800 px-1 rounded">Home Assistant</code></li>
                <li>URL: <code class="bg-slate-800 px-1 rounded">{{MCP_SERVER_URL}}</code></li>
                <li>Auth: None</li>
              </ul>
            </li>
            <li>To use in a conversation: click <strong>+</strong> → <strong>More</strong> → <strong>Developer Mode</strong> → enable your connector
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li>The connector must be enabled each new conversation (current UX limitation)</li>
                <li>MCP endpoint must be at the root path <code class="bg-slate-800 px-1 rounded">/</code></li>
                <li>Server must accept UUID-format client IDs</li>
              </ul>
            </li>
          </ol>
          <p class="text-slate-400 text-sm mt-3"><strong class="text-slate-300">Behind a firewall?</strong> ChatGPT connects from OpenAI's servers, so the URL above must be publicly reachable. If you can't (or don't want to) expose one, the community <a href="https://github.com/norpol/hass-codex-tunnel-mcp" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">OpenAI Tunnel for HA-MCP</a> integration connects your local ha-mcp URL to an OpenAI-hosted tunnel over an outbound-only connection. In that case the connector is set up with your tunnel ID instead of the URL in the steps above — follow the integration's README for the ChatGPT-side steps. See the <a href="${base}/faq#openai-tunnel" class="text-blue-400 hover:underline">FAQ</a>.</p>
        </div>`;
  } else if (state.client.id === 'claude-ai') {
    // Which sign-in the connector wizard should land on depends on the
    // deployment: only the component's built-in webhook and the Webhook
    // Proxy app can run OAuth (ha_auth / legacy); every other remote path
    // is secret-URL only, which the wizard detects as "None".
    const oauthCapable = (isComponent && state.remotePath === 'builtin-webhook') || state.remotePath === 'webhook-proxy';
    const modeOption = isComponent
      ? `the integration's <strong>Authentication mode</strong> option`
      : `the Webhook Proxy app's <strong>OAuth Mode</strong> option`;
    const modeChoices = oauthCapable ? `
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li><strong>Secret URL</strong> (${isComponent ? '<code class="bg-slate-800 px-1 rounded">none</code>, the simplest start' : 'OAuth off, the default'}): Authentication <strong>None</strong> (Detected). The URL is the credential and no OAuth client section appears. Keep the detected setting; manually forcing a different authentication setting may not help.</li>
                <li><strong>Home Assistant sign-in</strong> (<code class="bg-slate-800 px-1 rounded">ha_auth</code>): Authentication <strong>Always required</strong>, OAuth client <strong>Use Anthropic's hosted client metadata</strong> (both Detected). No Client ID or Secret.</li>
                <li><strong>Legacy</strong> (<code class="bg-slate-800 px-1 rounded">legacy</code>): Authentication <strong>Always required</strong>, OAuth client <strong>Use your own OAuth client</strong>, then paste the Client ID and Client Secret from ${isComponent ? "the entry's Configure screen" : 'the app log'}.</li>
              </ul>` : `
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li>Authentication <strong>None</strong> (Detected). The secret path in the URL is the credential; this deployment has no OAuth client section.</li>
              </ul>`;
    uiSteps = `<div class="instruction-block">
          <h3 class="instruction-title">Claude.ai Configuration</h3>
          <p class="text-slate-300 text-sm mb-3">Claude.ai connector setup is known to be flaky, but usually works after repeated attempts. If you installed the embedded HA-MCP server, make sure you have <strong>restarted Home Assistant</strong> after installation so everything registers properly.</p>
          <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
            <li>Open Claude.ai → <strong>Customize</strong> (left sidebar) → <strong>Connectors</strong> → <strong>Add</strong></li>
            <li>Enter a name (e.g. <code class="bg-slate-800 px-1 rounded">Home Assistant</code>) and the URL <code class="bg-slate-800 px-1 rounded">{{MCP_SERVER_URL}}</code>, then click <strong>Continue</strong>. Claude checks the server and pre-fills the next step; choices it worked out are tagged <em>Detected</em> — keep them.</li>
            <li>On the second step, confirm <strong>Authentication</strong> and <strong>OAuth client</strong> match your setup:${modeChoices}
            </li>
            <li>Click <strong>Add</strong>. With no sign-in the connector is live immediately; otherwise click its <strong>Connect</strong> button.${oauthCapable ? ` With <code class="bg-slate-800 px-1 rounded">ha_auth</code> you sign in with ${isComponent ? 'a Home Assistant <strong>administrator</strong> account (non-admin logins are refused)' : 'your Home Assistant account (any account that can sign in gets the same admin-equivalent tool access)'}; with <code class="bg-slate-800 px-1 rounded">legacy</code> you click <strong>Allow</strong> on the consent page.` : ''}</li>
            <li>If <em>Checking server</em> reports it couldn't determine the settings, the connection may not be working. Re-check the URL for typos and public reachability: Claude.ai connects from Anthropic's servers, so open the URL on your <strong>phone with Wi-Fi off</strong> — if it doesn't load there, check DNS / port-forwarding / TLS / proxy settings. <strong>Delete any existing connector and create a new one, then let Claude.ai try auto-detection again.</strong> You can try clicking <strong>Next</strong> and selecting the settings above manually, but connections typically only work when the settings are auto-detected, so forcing them may not help. In particular, keep a detected <strong>None / No login</strong> setting rather than manually forcing it.</li>
            <li>Once connected, the connector page lists every tool with a per-tool permission (allow / needs approval / block). If it says <em>no tools available</em>, use the <strong>⋮</strong> menu → <strong>Refresh tools list</strong>.</li>
            <li>You can also start a new conversation and ask Claude if it can see your Home Assistant via the MCP connection to confirm</li>${oauthCapable ? `
            <li>Claude binds the sign-in choice to the connector: after changing ${modeOption}, <strong>remove and re-add</strong> the connector.</li>` : ''}
          </ol>
        </div>`;
  } else if (state.client.id === 'gemini-spark') {
    uiSteps = legacyOauthReady ? `<div class="instruction-block">
          <h3 class="instruction-title">Gemini Spark Configuration</h3>
          <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
            <li>Set up the <strong>HA-MCP Server</strong> custom component (in-process server + built-in webhook) — see the Quick Setup box above.</li>
            <li>On the entry's <strong>Configure</strong> screen, set <strong>Authentication mode</strong> to <code class="bg-slate-800 px-1 rounded">legacy</code>.</li>
            <li>When Home Assistant shows a repair prompt, <strong>restart Home Assistant</strong> — legacy mode needs a restart to take effect.</li>
            <li>Back on the entry's <strong>Configure</strong> screen, copy the <strong>Client ID</strong> and <strong>Client Secret</strong>.</li>
            <li>In Gemini, choose <strong>Set up a custom connected app</strong> and paste your <strong>built-in webhook URL</strong> (<code class="bg-slate-800 px-1 rounded">https://&lt;your-ha-domain&gt;/api/webhook/&lt;webhook-id&gt;</code>, shown on the entry's Configure screen) as the server URL. Legacy OAuth's <code class="bg-slate-800 px-1 rounded">/authorize</code> and <code class="bg-slate-800 px-1 rounded">/token</code> are served on your Home Assistant host, so a direct-port or tunnel URL (cloudflared/custom) will not complete the OAuth flow with Spark.</li>
            <li>Expand <strong>Advanced settings</strong> and paste in the Client ID and Client Secret.</li>
            <li>Click <strong>Next</strong> and complete Google's account-linking flow.</li>
          </ol>
          <div class="bg-purple-900/20 border border-purple-700/50 rounded-lg p-3 mt-3">
            <p class="text-purple-300 text-sm font-medium">Why legacy OAuth mode</p>
            <p class="text-purple-200/80 text-xs mt-1"><strong>legacy</strong> with pasted credentials is the verified path for Spark. As of component 2.0.0 the component accepts Spark's automatic registration (its two-origin redirect set) and validates its Client ID Metadata Document itself, so <strong>Home Assistant sign-in</strong> and <strong>secret-URL</strong> modes are expected to connect without any pasted credentials — live verification is in progress, and this page will lead with that path once confirmed. One limitation either way: Spark-shaped registrations cannot refresh and re-authorize when tokens expire. See the <a href="${base}/faq#google-spark-oauth" class="text-purple-300 hover:underline">FAQ</a> for details.</p>
          </div>
        </div>` : legacyMethodNotice('Gemini Spark');
  } else if (state.client.id === 'raycast') {
    uiSteps = `<div class="instruction-block">
          <h3 class="instruction-title">Raycast Configuration</h3>
          <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
            <li>Open Raycast → <strong>Settings</strong> → <strong>Extensions</strong> → <strong>AI</strong></li>
            <li>Scroll to <strong>Model Context Protocol</strong></li>
            <li>Click <strong>Add Server</strong>:
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li>Name: <code class="bg-slate-800 px-1 rounded">Home Assistant</code></li>
                <li>Command: <code class="bg-slate-800 px-1 rounded">uvx</code></li>
                <li>Arguments: <code class="bg-slate-800 px-1 rounded">ha-mcp@latest</code></li>
              </ul>
            </li>
            <li>Add environment variables:
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li><code class="bg-slate-800 px-1 rounded">HOMEASSISTANT_URL</code>: {{HOMEASSISTANT_URL}}</li>
                <li><code class="bg-slate-800 px-1 rounded">HOMEASSISTANT_TOKEN</code>: {{HOMEASSISTANT_TOKEN}}</li>
              </ul>
            </li>
          </ol>
          <div class="mt-3 text-sm text-slate-400 space-y-1">
            <p><strong class="text-slate-300">Usage:</strong> In Quick AI and AI Chat, type <code class="bg-slate-800 px-1 rounded">@home-assistant</code> to invoke the server.</p>
            <p><strong class="text-slate-300">Deeplink install:</strong> You can also install via <code class="bg-slate-800 px-1 rounded">raycast://mcp/install?&lt;config-json-percent-encoded&gt;</code> as an alternative to the UI.</p>
            ${!isStdio ? '<p><strong class="text-slate-300">HTTP transport:</strong> Enable HTTP in AI Settings. Use <code class="bg-slate-800 px-1 rounded">httpHeaders: { Authorization: "Bearer &lt;token&gt;" }</code> for authenticated servers.</p>' : ''}
            <p><a href="https://manual.raycast.com/model-context-protocol" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">Raycast MCP docs →</a></p>
          </div>
        </div>`;
  } else if (state.client.id === 'open-webui') {
    uiSteps = `<div class="instruction-block">
          <h3 class="instruction-title">Open WebUI Configuration</h3>
          <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
            <li>Navigate to <strong>Admin Panel</strong> → <strong>Settings</strong> → <strong>Tools</strong> → <strong>Manage Tool Servers</strong>
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li>Note: There is also "External Tools" in user settings — use the <strong>Admin</strong> one here</li>
              </ul>
            </li>
            <li>Click <strong>+ (Add Server)</strong></li>
            <li>Enter:
              <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                <li>Server URL: <code class="bg-slate-800 px-1 rounded">{{MCP_SERVER_URL}}</code></li>
                <li>Auth: (configure if needed)</li>
              </ul>
            </li>
            <li>Click <strong>Save</strong></li>
          </ol>
          <div class="mt-3 text-sm text-slate-400 space-y-1">
            <p><strong class="text-slate-300">Finding your URL:</strong> Common patterns:</p>
            <ul class="ml-4 list-disc list-inside space-y-1">
              <li>Docker HA on same host: <code class="bg-slate-800 px-1 rounded">http://host.docker.internal:8086/mcp</code></li>
              <li>Local network: <code class="bg-slate-800 px-1 rounded">http://192.168.1.100:8086/mcp</code></li>
              <li>Cloudflare tunnel: <code class="bg-slate-800 px-1 rounded">https://your-tunnel.trycloudflare.com/secret_abc123</code></li>
            </ul>
            <p class="mt-1">For stdio-based servers, use <a href="https://github.com/open-webui/mcpo" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">mcpo</a> proxy to bridge stdio to HTTP.</p>
            <p><a href="https://docs.openwebui.com/features/mcp/" class="text-blue-400 hover:underline" target="_blank" rel="noopener noreferrer">Open WebUI MCP docs →</a></p>
          </div>
          <div class="bg-amber-900/20 border border-amber-700/50 rounded-lg p-3 mt-4">
            <p class="text-amber-300 text-sm font-medium">Using a smaller or local LLM (Ollama, etc.)?</p>
            <p class="text-amber-200/80 text-xs mt-1">If the model can't see the tools or your Home Assistant, it may be getting handed all 87 tools at once. It's recommended to try the following to see if it helps:</p>
            <ul class="text-amber-200/80 text-xs mt-1 space-y-1 list-disc list-inside">
              <li>Enable tool search (app option <code class="bg-amber-900/30 px-1 rounded">enable_tool_search</code>, or <code class="bg-amber-900/30 px-1 rounded">ENABLE_TOOL_SEARCH=true</code>): the full catalog is deferred behind a search interface so tools load on demand instead of all at once.</li>
              <li>Raise your model's context window above the default (in Ollama, <code class="bg-amber-900/30 px-1 rounded">num_ctx</code>) so the tool definitions and your conversation fit.</li>
            </ul>
            <p class="text-amber-200/80 text-xs mt-1"><a href="https://github.com/homeassistant-ai/ha-mcp#smaller-or-local-llms-ollama-etc" target="_blank" rel="noopener noreferrer" class="text-amber-300 hover:underline">Smaller or local LLM setup →</a></p>
          </div>
        </div>`;
  } else if (state.client.id === 'copilot-cli') {
    if (isStdio) {
      uiSteps = `<div class="instruction-block">
            <h3 class="instruction-title">Copilot CLI Configuration (STDIO)</h3>
            <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
              <li>Start Copilot CLI: <code class="bg-slate-800 px-1 rounded">copilot</code></li>
              <li>Run the <code class="bg-slate-800 px-1 rounded">/mcp add</code> slash command</li>
              <li>Fill in the form (use <strong>Tab</strong> to navigate, <strong>Ctrl+S</strong> to save):
                <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                  <li>Server Name: <code class="bg-slate-800 px-1 rounded">home-assistant</code></li>
                  <li>Server Type: Press <strong>2</strong> for STDIO</li>
                  <li>Command: <code class="bg-slate-800 px-1 rounded">uvx ha-mcp@latest</code></li>
                  <li>Environment Variables: <code class="bg-slate-800 px-1 rounded">HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}</code></li>
                  <li>Tools: <code class="bg-slate-800 px-1 rounded">*</code></li>
                </ul>
              </li>
            </ol>
            <div class="mt-4 text-sm text-slate-400 space-y-1">
              <p><strong class="text-slate-300">Notes &amp; extras:</strong></p>
              <ul class="ml-4 list-disc list-inside space-y-1">
                <li>Server Type legend: <strong>1</strong> = Local cmd, <strong>2</strong> = STDIO, <strong>3</strong> = HTTP, <strong>4</strong> = SSE</li>
                <li>Environment variables format: <code class="bg-slate-800 px-1 rounded">KEY=VALUE</code> pairs separated by spaces</li>
                <li>Tools field: use <code class="bg-slate-800 px-1 rounded">*</code> for all tools, or a comma-separated list</li>
                <li>Set <code class="bg-slate-800 px-1 rounded">COPILOT_HOME</code> to change the config directory (default: <code class="bg-slate-800 px-1 rounded">~/.copilot/</code>)</li>
                <li>Inside an interactive <code class="bg-slate-800 px-1 rounded">copilot</code> session: <code class="bg-slate-800 px-1 rounded">/mcp</code> shows server status</li>
                <li>The GitHub MCP server is included by default — you don't need to configure it</li>
              </ul>
            </div>
          </div>`;
    } else {
      uiSteps = legacyOauthReady ? `<div class="instruction-block">
            <h3 class="instruction-title">Copilot CLI Configuration (HTTP)</h3>
            <ol class="text-sm text-slate-300 space-y-2 list-decimal list-inside">
              <li>Start Copilot CLI: <code class="bg-slate-800 px-1 rounded">copilot</code></li>
              <li>Run the <code class="bg-slate-800 px-1 rounded">/mcp add</code> slash command</li>
              <li>Fill in the form (use <strong>Tab</strong> to navigate, <strong>Ctrl+S</strong> to save):
                <ul class="ml-6 mt-1 text-slate-400 list-disc list-inside space-y-1">
                  <li>Server Name: <code class="bg-slate-800 px-1 rounded">home-assistant</code></li>
                  <li>Server Type: Press <strong>3</strong> for HTTP</li>
                  <li>URL: <code class="bg-slate-800 px-1 rounded">{{MCP_SERVER_URL}}</code></li>
                  <li>Tools: <code class="bg-slate-800 px-1 rounded">*</code></li>
                  <li>Client ID: required field (Copilot CLI won't accept it blank) — see the callout below for where to get one that works</li>
                </ul>
              </li>
            </ol>
            <div class="mt-4 text-sm text-slate-400 space-y-1">
              <p><strong class="text-slate-300">Notes &amp; extras:</strong></p>
              <ul class="ml-4 list-disc list-inside space-y-1">
                <li>Server Type legend: <strong>1</strong> = Local cmd, <strong>2</strong> = STDIO, <strong>3</strong> = HTTP, <strong>4</strong> = SSE</li>
                <li>Tools field: use <code class="bg-slate-800 px-1 rounded">*</code> for all tools, or a comma-separated list</li>
                <li>Set <code class="bg-slate-800 px-1 rounded">COPILOT_HOME</code> to change the config directory (default: <code class="bg-slate-800 px-1 rounded">~/.copilot/</code>)</li>
                <li>Inside an interactive <code class="bg-slate-800 px-1 rounded">copilot</code> session: <code class="bg-slate-800 px-1 rounded">/mcp</code> shows server status</li>
                <li>The GitHub MCP server is included by default — you don't need to configure it</li>
              </ul>
            </div>
            <div class="bg-purple-900/20 border border-purple-700/50 rounded-lg p-3 mt-3">
              <p class="text-purple-300 text-sm font-medium">Client ID required: ha_auth registers one automatically (2.0.0+); legacy as fallback</p>
              <p class="text-purple-200/80 text-xs mt-1">Copilot CLI's remote MCP setup requires a Client ID and performs dynamic client registration. As of component 2.0.0 the <code class="bg-slate-800 px-1 rounded">ha_auth</code> mode serves a registration endpoint, so keep <strong>Authentication mode</strong> on <code class="bg-slate-800 px-1 rounded">ha_auth</code> — Copilot CLI registers a client automatically and signs in with your Home Assistant account.</p>
              <p class="text-purple-200/80 text-xs mt-2">On older component versions (or if registration fails): on the HA-MCP Server entry's <strong>Configure</strong> screen, set <strong>Authentication mode</strong> to <code class="bg-slate-800 px-1 rounded">legacy</code> and restart Home Assistant when the repair prompts you. Then copy the generated <strong>Client ID</strong> and <strong>Client Secret</strong> from that screen into Copilot CLI's Client ID and secret fields. Use your <strong>built-in webhook URL</strong> (<code class="bg-slate-800 px-1 rounded">https://&lt;your-ha-domain&gt;/api/webhook/&lt;webhook-id&gt;</code>) as the server URL — legacy OAuth is served on your Home Assistant host, so a direct-port or tunnel URL won't complete the OAuth flow. See the <a href="${base}/faq#copilot-cli-oauth-legacy" class="text-purple-300 hover:underline">FAQ</a> for details.</p>
            </div>
          </div>` : legacyMethodNotice('Copilot CLI');
    }
  }
  instructions.push(uiSteps);
}
