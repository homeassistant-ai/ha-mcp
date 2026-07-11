
// Catch top-level / async script errors and write them into the #status
// ARIA live region (now visually hidden — see settings.css). This still
// announces a script-eval failure to screen readers, but is NOT visible to
// sighted users and does not toast (showToast isn't defined yet this early
// in script eval). For visible diagnosis, use the browser console. Without
// this, a script-evaluation error in any of the function definitions below
// would abort the script before loadTools() is even called.
window.addEventListener('error', (e) => {
  const el = document.getElementById('status');
  if (!el) return;
  const where = e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : 'inline';
  setStatusAlert(el, true);
  el.textContent = `JS error: ${e.message} @ ${where}`;
});
window.addEventListener('unhandledrejection', (e) => {
  const el = document.getElementById('status');
  if (!el) return;
  setStatusAlert(el, true);
  el.textContent = `Async error: ${e.reason && e.reason.message ? e.reason.message : String(e.reason)}`;
});

let toolData = [];
let toolStates = {};
// Map of tool name → "disabled" | "pinned" for env-var-pinned tools.
// Populated from data.env_pinned in loadTools(); read by render() to
// lock rows and show the env-var name banner.
let toolEnvPinned = {};
// Conversation-agent LLM API exposure (#1745). toolLlm mirrors the
// server-computed EFFECTIVE value per tool (user override, else the
// deny-by-default for beta/dev/restart tools); toolLlmOverrides holds
// only the user-set overrides and is what saveConfig persists — tools
// never flipped keep tracking their defaults across releases.
let toolLlm = {};
let toolLlmOverrides = {};
let saveTimer = null;
let openGroups = new Set();

// Per-tool "security gated" toggle state mirrors policy.rules from
// /api/policy/config. A tool is gated iff there's any rule with a
// matching tool_name (with or without conditions). The Tools tab
// uses this set to render the third toggle alongside enabled/pinned.
// `enabled` is tri-state: true/false from the addon-config flag, or
// null when the features fetch failed — downstream branches need to
// distinguish "definitively off" from "couldn't determine" so they
// don't false-confidently tell the user the feature is off.
const policyState = {
  enabled: false,
  enabledKnown: false,
  gatedTools: new Set(),
};

// Read Only Mode (read_only_mode feature flag) — same tri-state shape
// as policyState. Mirrors the toggle above the Tools-tab search box and
// the Server Settings row. While enabled, render() forces write-capable
// tools off (visually, without rewriting toolStates — same
// non-destructive semantics as the beta master gate) except the
// server-provided READ_ONLY_EXEMPT mixed read/write tools.
const readOnlyState = {
  enabled: false,
  enabledKnown: false,
};

// Mixed read/write tools that stay enabled in Read Only Mode (their
// write operations are blocked server-side at call time). Populated
// from data.read_only_exempt in loadTools().
let READ_ONLY_EXEMPT = new Set();

async function loadPolicyState() {
  // policyState.enabled mirrors the addon-config flag
  // (enable_tool_security_policies) — the single source of truth for
  // whether the middleware is active. Read it from /api/settings/features
  // where it appears via FEATURE_FLAG_FIELDS. readOnlyState piggybacks
  // on the same fetch (read_only_mode is in the same flags payload).
  try {
    const fresp = await fetch('./api/settings/features');
    if (fresp.ok) {
      const fdata = await fresp.json();
      const flag = (fdata.flags || {})['enable_tool_security_policies'];
      policyState.enabled = !!(flag && flag.value);
      policyState.enabledKnown = true;
      const roFlag = (fdata.flags || {})['read_only_mode'];
      readOnlyState.enabled = !!(roFlag && roFlag.value);
      readOnlyState.enabledKnown = true;
    } else {
      policyState.enabled = false;
      policyState.enabledKnown = false;
      readOnlyState.enabled = false;
      readOnlyState.enabledKnown = false;
    }
  } catch (_e) {
    policyState.enabled = false;
    policyState.enabledKnown = false;
    readOnlyState.enabled = false;
    readOnlyState.enabledKnown = false;
  }
  try {
    const r = await fetch('./api/policy/config');
    if (!r.ok) {
      // Transient failure — keep the previously-loaded gatedTools (a Set)
      // rather than clobbering it to empty, so a blip doesn't make the
      // Tools tab falsely claim nothing is gated.
      console.warn('[ha-mcp] /api/policy/config returned HTTP ' + r.status + '; keeping prior gated-tools state');
      return;
    }
    const p = await r.json();
    policyState.gatedTools = new Set((p.rules || []).map(rule => rule.tool_name));
  } catch (e) {
    // Policy endpoint unavailable (sidecar stub) or network blip — keep the
    // prior gatedTools rather than resetting it to empty. On first load it
    // is already an empty Set, so the default still holds.
    console.warn('[ha-mcp] failed to load policy config', e);
  }
}

// Wrap PUT /api/policy/config so every caller gets identical handling of
// the 409 (optimistic-concurrency) and other failure paths. The full
// policy round-trips through every caller, so the version GET'd here
// goes back out in the PUT body and the server can reject stale writes.
async function policyPut(policy, opLabel) {
  const w = await fetch('./api/policy/config', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(policy),
  });
  if (w.status === 409) {
    throw new Error(opLabel + ' failed: policy was modified in another tab/session. Reload the page, then re-apply your changes.');
  }
  if (!w.ok) throw new Error(opLabel + ' failed: ' + w.status + ' ' + await w.text());
  return await w.json();
}

async function syncPolicyRule(toolName, gated) {
  const r = await fetch('./api/policy/config');
  if (!r.ok) throw new Error('Could not load policy: ' + r.status);
  const policy = await r.json();
  policy.rules = policy.rules || [];
  if (gated) {
    if (!policy.rules.some(rule => rule.tool_name === toolName)) {
      policy.rules.push({tool_name: toolName, when: [], remember_minutes: 0});
    }
  } else {
    policy.rules = policy.rules.filter(rule => rule.tool_name !== toolName);
  }
  await policyPut(policy, 'Sync gated toggle');
}

async function loadTools() {
  let resp;
  try {
    resp = await fetch('./api/settings/tools');
  } catch (e) {
    updateStatus('Network error reaching /api/settings/tools: ' + e.message, false, true);
    return;
  }
  if (!resp.ok) {
    updateStatus(`/api/settings/tools returned HTTP ${resp.status} ${resp.statusText}`, false, true);
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (e) {
    updateStatus('Failed to parse /api/settings/tools response as JSON: ' + e.message, false, true);
    return;
  }
  toolData = data.tools || [];
  toolStates = data.states || {};
  toolEnvPinned = data.env_pinned || {};
  toolLlm = data.llm_api || {};
  toolLlmOverrides = data.llm_api_overrides || {};
  READ_ONLY_EXEMPT = new Set(data.read_only_exempt || []);
  // Load policy state before the first render so the "security gated"
  // toggle reflects current policy.rules. loadPolicyState() never throws
  // — it keeps the prior gatedTools on failure.
  await loadPolicyState();
  syncReadOnlyToggle();
  // /api/settings/info drives the restart-button mode, restart-notice
  // copy, and the version footer. Fetch it BEFORE the empty-tools
  // early return so a sidecar misconfig (toolData=[]) still gets the
  // build version shown at the bottom of the page.
  await applyInfoChrome();
  if (toolData.length === 0) {
    // Empty tool list is a sidecar misconfiguration — usually the
    // parent stdio process couldn't dump the metadata cache. Tell
    // the user where to look instead of leaving them on "Loading".
    updateStatus(
      'No tools found. The sidecar reads ~/.ha-mcp/tool_metadata.json. ' +
      'If it is missing or empty, restart your MCP client. See ~/.ha-mcp/sidecar.log for details.',
      false, true
    );
    return;
  }
  try {
    render();
  } catch (e) {
    updateStatus('Render failed: ' + e.message + ' (open browser devtools for the stack)', false, true);
    throw e;
  }
  updateStatus('Loaded');
}

async function applyInfoChrome() {
  // Show restart button if running as add-on; show Stop Sidecar
  // button only when this page is served by the stdio sidecar
  // (HTTP modes serve the same HTML but is_sidecar=false there, so
  // clicking Stop wouldn't make sense — it would kill the MCP server).
  // Also tailor the restart-notice copy to the install mode so the
  // user is told exactly what action they need to take ("close and
  // reopen Claude Desktop" vs "click Restart Add-on" vs "restart
  // your Docker container") instead of a generic "restart the add-on"
  // that only matches one of three real deployment surfaces.
  try {
    const infoResp = await fetch('./api/settings/info');
    const info = await infoResp.json();
    const noticeEl = document.getElementById('restartNoticeText');
    if (info.is_addon) {
      document.getElementById('restartBtn').style.display = '';
      if (noticeEl) {
        noticeEl.textContent =
          '⚠ Changes saved. Click "Restart App (add-on)" for them to take ' +
          'effect. Disabled tools will be fully removed from the MCP ' +
          'tool list on next startup. Then refresh the tool list in your ' +
          'AI client — e.g. refresh tool list on claude.ai, re-add or ' +
          'refresh the connector in ChatGPT, or close and reopen Claude ' +
          'Desktop. Restarting the App (add-on) alone does not refresh your ' +
          'client\'s cached tool list.';
      }
    } else if (info.deployment_mode === 'embedded') {
      // In-process (custom component) server: the restart endpoint reloads
      // the server config entry, which reinstalls-if-newer and swaps the
      // worker onto the freshly installed code.
      const rbtn = document.getElementById('restartBtn');
      rbtn.style.display = '';
      rbtn.textContent = 'Restart HA-MCP Server';
      if (noticeEl) {
        noticeEl.textContent =
          '⚠ Changes saved. Click "Restart HA-MCP Server" (reloads the ' +
          'in-process server integration) for them to take effect. ' +
          'Disabled tools will be fully removed from the MCP tool list on ' +
          'next startup. Then refresh the tool list in your AI client — ' +
          'e.g. refresh tool list on claude.ai, re-add or refresh the ' +
          'connector in ChatGPT, or close and reopen Claude Desktop.';
      }
    } else if (info.is_sidecar) {
      if (noticeEl) {
        noticeEl.textContent =
          '⚠ Changes saved. Fully quit and reopen your MCP client ' +
          '(Claude Desktop: right-click the tray icon → Quit, then ' +
          'relaunch; Claude Code: close the terminal session) for them ' +
          'to take effect. Disabled tools will be fully removed from the ' +
          'MCP tool list on next startup.';
      }
      document.getElementById('sidecarStopRow').style.display = '';
    } else if (noticeEl) {
      // HTTP / Docker / standalone — no button we can wire to a restart,
      // so describe the action in process terms, then the client-refresh
      // step (remote connectors cache the tool list, same as add-on mode).
      noticeEl.textContent =
        '⚠ Changes saved. Restart your ha-mcp process (Docker ' +
        'container, systemd service, or however you launch it) for them ' +
        'to take effect. Disabled tools will be fully removed from the ' +
        'MCP tool list on next startup. Then refresh the tool list in ' +
        'your AI client — e.g. refresh tool list on claude.ai, re-add or ' +
        'refresh the connector in ChatGPT, or close and reopen Claude ' +
        'Desktop. Restarting ha-mcp alone does not refresh your client\'s ' +
        'cached tool list.';
    }
    // Version footer — show the running ha-mcp build at the bottom
    // of every page. ``info.version`` is whatever
    // ``HA_MCP_BUILD_VERSION`` the addon's Dockerfile set (e.g.
    // ``7.5.0`` on stable, ``7.5.0.dev355`` on dev), with a fallback
    // to package metadata in standalone deployments.
    if (info.version) {
      const fEl = document.getElementById('versionFooterText');
      if (fEl) fEl.textContent = 'ha-mcp ' + info.version;
    }
  } catch (e) {
    // A transient /api/settings/info failure must not leave the restart
    // button hidden / the restart notice unset silently — log it so the
    // missing chrome is diagnosable from the console.
    console.warn('[ha-mcp] failed to apply settings info', e);
  }
}

async function stopSidecar() {
  const btn = document.getElementById('stopSidecarBtn');
  // Two-part confirm wording: lead with the *permanence* (this is not a
  // routine "stop now, autostart later" — the server will refuse to
  // restart on every future ha-mcp launch until the user manually
  // intervenes), then spell out the exact re-enable steps. The button
  // is right-aligned near the top of a list of toggle controls, so
  // accidental clicks are easy; the dialog needs to read like a
  // commitment, not a soft prompt.
  if (!confirm(
    '⚠ PERMANENTLY disable the settings server?\n\n' +
    'This stops the running server AND writes a disable marker so it ' +
    'will NOT respawn on future ha-mcp launches. Every restart of ' +
    'Claude Desktop / Docker / however you launch HA-MCP will continue ' +
    'to skip it until you manually re-enable.\n\n' +
    'To restore access later you must:\n' +
    '  1. Delete  ~/.ha-mcp/settings_ui_disabled  (the marker file), AND\n' +
    '  2. Unset  HA_MCP_DISABLE_SETTINGS_UI  if that env var was set.\n\n' +
    'You will lose the in-browser tool-configuration UI until both ' +
    'conditions are met. Continue?'
  )) return;
  btn.disabled = true;
  btn.textContent = 'Stopping...';
  try {
    const resp = await fetch('./api/settings/shutdown', {method: 'POST'});
    if (resp.ok) {
      btn.textContent = 'Stopped. This page will go offline';
    } else {
      let msg = 'Stop failed';
      try {
        const err = await resp.json();
        if (err.error && err.error.message) msg = 'Failed: ' + err.error.message;
      } catch (_e) {}
      btn.textContent = msg;
      btn.disabled = false;
      alert(msg);
    }
  } catch (_e) {
    // Connection drop is expected — the sidecar process is exiting.
    btn.textContent = 'Stopped (connection dropped)';
  }
}

// Restart-readiness probe tunables. The grace period gives supervisor
// time to actually kill the addon (so a too-eager first probe doesn't
// hit the OLD instance and reload before the new one is up). The poll
// interval is short enough to feel responsive on a fast restart, long
// enough to not hammer ingress. The cap is the user-visible upper
// bound; HAOS addon restarts are typically 15-25s but cold-start +
// image pull can stretch further, so 60s gives genuine breathing room
// before we tell the user the auto-reload failed.
const RESTART_PROBE_INITIAL_GRACE_MS = 3000;
const RESTART_PROBE_INTERVAL_MS = 2000;
const RESTART_PROBE_MAX_TOTAL_MS = 60000;

// Cross-tab restart broadcast channel. When any tab saves a setting
// that needs a restart, it posts ``restart-required`` so the other
// tabs surface the same banner. When any tab fires the supervisor
// restart, it posts ``restart-initiated`` so the other tabs run the
// same poll-then-reload cycle — that way ALL tabs come back to the
// fresh addon instead of leaving stale ones spinning.
const restartChannel =
  typeof BroadcastChannel === 'function'
    ? new BroadcastChannel('ha-mcp-settings')
    : null;

// Surface the cross-tab restart-required banner and tell every other open
// settings tab to surface it too, so the user can click Restart from
// whichever tab they are on. Used by every save path that persists a
// restart-gated change (Tools, backups, feature flags, advanced settings).
function markRestartRequired() {
  document.getElementById('restartNotice').classList.add('show');
  if (typeof restartChannel !== 'undefined' && restartChannel) {
    restartChannel.postMessage({type: 'restart-required'});
  }
}

// Module-level concurrency guard. The button's ``disabled`` attribute
// blocks normal clicks, but a second invocation via DevTools / a
// keyboard accessibility tool / a cross-tab broadcast would otherwise
// queue a second supervisor restart + a second auto-reload. Cleared
// only on a 4xx genuine config error (so the user can reload and try
// again); otherwise stays true through the restart cycle until the
// page reloads.
let restartInProgress = false;

async function _fetchSettingsInfo() {
  // Read ``/api/settings/info`` once; return the parsed JSON or null
  // on any failure. ``cache: 'no-store'`` so the browser can't serve
  // a stale 200 from before the restart.
  try {
    const resp = await fetch('./api/settings/info', {cache: 'no-store'});
    if (!resp.ok) return null;
    return await resp.json();
  } catch (_e) {
    return null;
  }
}

async function _probeAddonRestarted(previousInstanceId) {
  // Resolve true when ``/api/settings/info`` returns a different
  // ``instance_id`` than the one captured before the restart —
  // proves a NEW process is serving, not the same OLD one (which
  // would happen if supervisor silently failed to restart and the
  // probe just saw the still-running upstream answer 200). When
  // ``previousInstanceId`` is null (couldn't capture pre-restart,
  // or server is on an older build that doesn't expose the field)
  // fall back to "any 200 means it's back" — same behavior as
  // before this fix landed, so we degrade gracefully.
  const deadline = Date.now() + RESTART_PROBE_MAX_TOTAL_MS;
  while (Date.now() < deadline) {
    const info = await _fetchSettingsInfo();
    if (info) {
      if (previousInstanceId) {
        if (info.instance_id && info.instance_id !== previousInstanceId) {
          return true;
        }
        // Same instance_id (or field missing on the response) — keep
        // polling; do NOT reload yet because the restart hasn't
        // actually happened yet.
      } else {
        // No baseline to compare against — best we can do is the
        // old "200 = up" check.
        return true;
      }
    }
    await new Promise(r => setTimeout(r, RESTART_PROBE_INTERVAL_MS));
  }
  return false;
}

async function _runRestartReloadCycle(previousInstanceId) {
  const btn = document.getElementById('restartBtn');
  // Initial grace lets supervisor actually kill the addon before we
  // start probing — otherwise the first probe may hit the OLD
  // instance and we reload before the new one is up.
  btn.textContent = 'Restarting…';
  await new Promise(r => setTimeout(r, RESTART_PROBE_INITIAL_GRACE_MS));
  btn.textContent = 'Waiting for App (add-on) to come back online…';
  const restarted = await _probeAddonRestarted(previousInstanceId);
  if (restarted) {
    window.location.reload();
  } else {
    // Probe gave up after RESTART_PROBE_MAX_TOTAL_MS. Restart either
    // never actually fired (silent supervisor failure → instance_id
    // never flipped) OR supervisor is genuinely slower than the cap.
    // Surface a clear next-step instead of silently doing nothing.
    btn.textContent = 'App (add-on) did not come back online. Reload manually';
    btn.disabled = false;
    restartInProgress = false;
  }
}

async function restartAddon() {
  if (restartInProgress) return;
  const btn = document.getElementById('restartBtn');
  if (!confirm('Restart HA-MCP now? The page will reload automatically once it is back online.')) return;
  restartInProgress = true;
  btn.disabled = true;
  btn.textContent = 'Restarting…';
  // Capture the current process's ``instance_id`` BEFORE firing the
  // restart so the poll cycle has a baseline to compare against.
  // null is fine — the probe degrades to the old "any 200 means up"
  // mode rather than refusing to reload.
  const info = await _fetchSettingsInfo();
  const previousInstanceId = info?.instance_id ?? null;
  try {
    const resp = await fetch('./api/settings/restart', {method: 'POST'});
    if (!resp.ok && resp.status < 500) {
      // 4xx is a genuine config error (e.g. SUPERVISOR_TOKEN unset).
      // The restart was NOT initiated — surface the error and let the
      // user fix the underlying cause. Keep button enabled so they
      // can retry once the issue is resolved. Don't broadcast (other
      // tabs would only see a misleading "restart in progress").
      let msg = 'Restart failed';
      try {
        const err = await resp.json();
        if (err?.error?.message) msg = 'Failed: ' + err.error.message;
      } catch (_e) { /* leave default msg */ }
      btn.textContent = msg;
      btn.disabled = false;
      restartInProgress = false;
      alert(msg);
      return;
    }
    // 200 OK → background task scheduled. 5xx → ingress upstream
    // drop, restart IS in flight. Both fall through to the reload
    // cycle.
  } catch (_e) {
    // Network error mid-request — supervisor killed our upstream.
    // Restart in flight; fall through. Log for debug, suppress the
    // unused-binding lint.
    console.warn('restartAddon fetch dropped (expected during self-restart):', _e);
  }
  // Other tabs need to run the same cycle so they reload to the fresh
  // addon, not stay on a stale view. Broadcast the baseline so each
  // tab compares against the same pre-restart ``instance_id``.
  if (restartChannel) {
    restartChannel.postMessage({
      type: 'restart-initiated',
      previousInstanceId,
    });
  }
  await _runRestartReloadCycle(previousInstanceId);
}

// Listener: when ANY tab broadcasts a save that needs a restart, all
// open tabs surface the banner. When ANY tab fires the restart, all
// open tabs run their own poll-then-reload cycle so none of them are
// left holding a stale connection to a now-dead addon.
if (restartChannel) {
  restartChannel.addEventListener('message', (e) => {
    const data = e.data || {};
    if (data.type === 'restart-required') {
      document.getElementById('restartNotice').classList.add('show');
    } else if (data.type === 'restart-initiated' && !restartInProgress) {
      restartInProgress = true;
      const btn = document.getElementById('restartBtn');
      if (btn) btn.disabled = true;
      // Use the originating tab's baseline ``instance_id`` so every
      // tab waits for the SAME ``instance_id`` flip before reloading.
      // Falls back to null → "any 200 = ready" mode if the originator
      // couldn't capture one.
      _runRestartReloadCycle(data.previousInstanceId ?? null);
    }
  });
}

const DEFAULT_PINNED = __HA_MCP_DEFAULT_PINNED__;
const MANDATORY = __HA_MCP_MANDATORY__;

function getState(name) {
  if (toolStates[name]) return toolStates[name];
  return DEFAULT_PINNED.includes(name) ? 'pinned' : 'enabled';
}

// True when Read Only Mode forces this tool's row off: write-capable
// (readOnlyHint !== true, the same fail-closed rule the server applies),
// not an exempt mixed read/write tool, not mandatory. Saved toolStates
// are deliberately NOT rewritten — turning the mode off restores the
// user's prior selections (beta-master semantics).
function isReadOnlyForcedOff(t) {
  if (!readOnlyState.enabled) return false;
  const ann = t.annotations || {};
  if (ann.readOnlyHint === true) return false;
  if (READ_ONLY_EXEMPT.has(t.name)) return false;
  if (MANDATORY.includes(t.name)) return false;
  return true;
}

function syncReadOnlyToggle() {
  const cb = document.getElementById('read-only-mode-toggle');
  if (cb) {
    cb.checked = !!readOnlyState.enabled;
  } else {
    // The toggle is part of the Tools-tab template; a missing element
    // means template drift (or this surface lacks the row). Warn once so
    // the desync is debuggable instead of silently no-op.
    console.warn('syncReadOnlyToggle: #read-only-mode-toggle not found');
  }
  // When the features fetch failed, readOnlyState.enabledKnown is false
  // and render() paints write tools as enabled even though the server may
  // still block them. Surface that uncertainty. Function-scope lookup
  // (guarded) so this id need not be a top-level handler binding.
  const notice = document.getElementById('roUnknownNotice');
  if (notice) notice.classList.toggle('show', !readOnlyState.enabledKnown);
}

// Escape HTML special characters before interpolating into innerHTML.
// All interpolated values come from the server (tool docstrings, names,
// FEATURE_GATED_TOOLS metadata) so this is defense-in-depth — but a
// docstring containing literal '<' or '&' would otherwise break the
// page silently.
function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function render() {
  const groups = {};
  toolData.forEach(t => {
    const tag = t.primary_tag || (t.tags && t.tags[0]) || 'Other';
    if (!groups[tag]) groups[tag] = [];
    groups[tag].push(t);
  });

  const container = document.getElementById('groups');
  container.innerHTML = '';

  let total = 0, enabledCount = 0, pinnedCount = 0, disabledCount = 0;

  Object.keys(groups).sort().forEach(tag => {
    const tools = groups[tag];
    const group = document.createElement('div');
    group.className = 'group';

    // Per-group toggle state: enabled if ANY non-mandatory/non-gated/non-env-pinned
    // tool is enabled. Tools forced off by Read Only Mode are excluded so the
    // group master switch can't fight the mode.
    const toggleable = tools.filter(t =>
      !MANDATORY.includes(t.name) && !t.disabled_by && !toolEnvPinned[t.name] &&
      !isReadOnlyForcedOff(t));
    const anyEnabled = toggleable.some(t => getState(t.name) !== 'disabled');
    const groupEnabled = tools.filter(t => {
      if (isReadOnlyForcedOff(t)) return false;
      if (toolEnvPinned[t.name]) return toolEnvPinned[t.name] !== 'disabled';
      const s = getState(t.name);
      return MANDATORY.includes(t.name) || (!t.disabled_by && s !== 'disabled');
    }).length;

    const header = document.createElement('div');
    header.className = 'group-header';
    header.innerHTML = `<div class="group-header-left">` +
      `<span class="group-chevron">&#9654;</span>` +
      `<span class="group-name">${escapeHtml(tag)}</span>` +
      `<span class="group-count">${groupEnabled}/${tools.length} enabled</span>` +
      `</div>` +
      `<label class="switch group-master" title="Enable/disable all tools in this group">` +
        `<input type="checkbox" name="tool-group:${escapeHtml(tag)}" ${anyEnabled ? 'checked' : ''} ${toggleable.length === 0 ? 'disabled' : ''}>` +
        `<span class="slider"></span>` +
      `</label>`;

    const chevron = header.querySelector('.group-chevron');
    const masterInput = header.querySelector('.group-master input');

    header.addEventListener('click', (e) => {
      // Ignore clicks on the master toggle itself
      if (e.target.closest('.group-master')) return;
      if (openGroups.has(tag)) openGroups.delete(tag);
      else openGroups.add(tag);
      const toolsDiv = group.querySelector('.group-tools');
      toolsDiv.classList.toggle('open');
      chevron.classList.toggle('open');
    });

    if (masterInput) {
      masterInput.addEventListener('click', (e) => e.stopPropagation());
      masterInput.addEventListener('change', (e) => {
        const target = e.target.checked ? 'enabled' : 'disabled';
        toggleable.forEach(t => {
          if (target === 'enabled') {
            // Restore to pinned if it was pinned by default, else enabled
            toolStates[t.name] = DEFAULT_PINNED.includes(t.name) ? 'pinned' : 'enabled';
          } else {
            toolStates[t.name] = 'disabled';
          }
        });
        scheduleSave();
        render();
      });
    }

    const toolsDiv = document.createElement('div');
    toolsDiv.className = 'group-tools';
    if (openGroups.has(tag)) {
      toolsDiv.classList.add('open');
      chevron.classList.add('open');
    }

    tools.forEach(t => {
      const state = getState(t.name);
      const isMandatory = MANDATORY.includes(t.name);
      const disabledBy = t.disabled_by || null;
      const isFeatureGated = disabledBy !== null;
      // env_pinned: "disabled" | "pinned" | undefined — operator-level lock
      // via DISABLED_TOOLS / PINNED_TOOLS env vars. When set, all inputs are
      // disabled and a banner names the env var. Takes precedence over
      // isMandatory / isFeatureGated for the lock calculation.
      const envPinKind = toolEnvPinned[t.name]; // "disabled" | "pinned" | undefined
      const isEnvPinned = !!envPinKind;
      const envPinVar = envPinKind === 'disabled' ? 'DISABLED_TOOLS' :
                        envPinKind === 'pinned'   ? 'PINNED_TOOLS'   : '';
      // Capability tier from the server (read | write | delete), derived
      // from the MCP readOnlyHint/destructiveHint annotations by
      // categorize_capability() — the same classifier the ha_call_*_tool
      // proxies use, so the badge and the proxy routing never disagree.
      const category = t.category || '';
      // Read Only Mode force-off wins over every other state source —
      // the server hides these tools from the catalog regardless of
      // saved state or env pins while the mode is on.
      const roForcedOff = isReadOnlyForcedOff(t);
      const roExemptActive = readOnlyState.enabled && READ_ONLY_EXEMPT.has(t.name);

      total++;
      if (roForcedOff) disabledCount++;
      else if (isEnvPinned) {
        if (envPinKind === 'disabled') disabledCount++;
        else { enabledCount++; pinnedCount++; }
      } else if (isFeatureGated) disabledCount++;
      else if (state === 'disabled') disabledCount++;
      else if (state === 'pinned') { enabledCount++; pinnedCount++; }
      else enabledCount++;

      const isEnabled = roForcedOff ? false : (isEnvPinned
        ? (envPinKind !== 'disabled')
        : (isFeatureGated ? false : (isMandatory || state !== 'disabled')));
      const isPinned = roForcedOff ? false : (isEnvPinned
        ? (envPinKind === 'pinned')
        : (isFeatureGated ? false : (isMandatory || state === 'pinned' || DEFAULT_PINNED.includes(t.name))));
      const lockEnabled = roForcedOff || isEnvPinned || isMandatory || isFeatureGated;
      const lockPinned = roForcedOff || isEnvPinned || isMandatory || isFeatureGated || !isEnabled;

      const div = document.createElement('div');
      div.className = isEnvPinned ? 'tool env-pinned' : 'tool';
      div.dataset.name = t.name.toLowerCase();
      div.dataset.title = (t.title || '').toLowerCase();

      let badges = '';
      if (isMandatory) badges += '<span class="badge mandatory">mandatory</span>';
      if (category === 'read') badges += '<span class="badge readonly">read-only</span>';
      else if (category === 'write') badges += '<span class="badge write">writes</span>';
      else if (category === 'delete') badges += '<span class="badge destructive">deletes</span>';
      // A missing/unknown category must still render a visible badge — a
      // destructive tool showing no tier badge would understate its risk.
      else badges += `<span class="badge unknown">${escapeHtml(category) || '?'}</span>`;

      const title = t.title || t.name;
      const desc = (t.description || '').split('\n')[0].slice(0, 120);
      const gatedNote = disabledBy
        ? `<div class="disabled-by-note">Beta. Set <code>${escapeHtml(disabledBy)}</code> in the dev App (add-on) config or the matching env var (see docs/beta.md).</div>`
        : '';
      const envPinnedNote = isEnvPinned
        ? `<div class="feature-locked-note">env-pinned via <code>${envPinVar}</code>. Unset the env var to edit here.</div>`
        : '';
      const readOnlyNote = roForcedOff
        ? '<div class="disabled-by-note">Off. Read Only Mode is on; write tools are disabled.</div>'
        : (roExemptActive
          ? '<div class="feature-locked-note">Read Only Mode: write operations of this tool are blocked; read operations stay available.</div>'
          : '');

      div.innerHTML = `<div class="tool-info">` +
        `<div class="tool-name">${escapeHtml(title)}${badges}</div>` +
        `<div class="tool-meta">${escapeHtml(t.name)}</div>` +
        (desc ? `<div class="tool-desc">${escapeHtml(desc)}</div>` : '') +
        gatedNote +
        envPinnedNote +
        readOnlyNote +
        `</div>` +
        `<div class="tool-toggles">` +
          `<div class="toggle-group">` +
            `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:enabled" data-tool="${escapeHtml(t.name)}" data-field="enabled" ` +
              `aria-label="${escapeHtml(title)} enabled" ` +
              `${isEnabled ? 'checked' : ''} ${lockEnabled ? 'disabled' : ''}>` +
              `<span class="slider"></span></label>` +
            `<span>enabled</span>` +
          `</div>` +
          `<div class="toggle-group ${!isEnabled ? 'disabled-toggle' : ''}">` +
            `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:pinned" data-tool="${escapeHtml(t.name)}" data-field="pinned" ` +
              `aria-label="${escapeHtml(title)} pinned" ` +
              `${isPinned ? 'checked' : ''} ${lockPinned ? 'disabled' : ''}>` +
              `<span class="slider"></span></label>` +
            `<span>pinned</span>` +
          `</div>` +
          `<div class="toggle-group ${(policyState.enabled && isEnabled) ? '' : 'disabled-toggle'}" ` +
               `title="${policyState.enabled ? '' : 'Enable Tool Security Policies in App (add-on) config first.'}">` +
            `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:gated" data-tool="${escapeHtml(t.name)}" data-field="gated" ` +
              `aria-label="${escapeHtml(title)} security gated" ` +
              `${policyState.gatedTools.has(t.name) ? 'checked' : ''} ` +
              `${(policyState.enabled && isEnabled) ? '' : 'disabled'}>` +
              `<span class="slider"></span></label>` +
            `<span>security gated</span>` +
          `</div>` +
          `<div class="toggle-group ${isEnabled ? '' : 'disabled-toggle'}" ` +
               `title="Offer this tool to Home Assistant conversation agents through the LLM API. Applies on the agent's next message - no restart. A tool disabled above is unavailable to agents regardless.">` +
            `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:llm" data-tool="${escapeHtml(t.name)}" data-field="llm" ` +
              `aria-label="${escapeHtml(title)} exposed to the conversation-agent LLM API" ` +
              `${(toolLlm[t.name] !== false) ? 'checked' : ''} ${isEnabled ? '' : 'disabled'}>` +
              `<span class="slider"></span></label>` +
            `<span>LLM API</span>` +
          `</div>` +
        `</div>`;

      const inputs = div.querySelectorAll('input[type="checkbox"]');
      inputs.forEach(input => {
        if (input.disabled) return;
        input.addEventListener('change', async (e) => {
          const field = e.target.dataset.field;
          if (field === 'gated') {
            // Optimistic UI: flip local state, sync to server, rollback on failure.
            // Gated lives in policy.rules (not tool_config), so we skip scheduleSave().
            const wasGated = policyState.gatedTools.has(t.name);
            const nowGated = e.target.checked;
            if (nowGated) policyState.gatedTools.add(t.name);
            else policyState.gatedTools.delete(t.name);
            try {
              await syncPolicyRule(t.name, nowGated);
            } catch (err) {
              if (wasGated) policyState.gatedTools.add(t.name);
              else policyState.gatedTools.delete(t.name);
              e.target.checked = wasGated;
              alert('Failed to update tool security policy: ' + err.message);
            }
            render();
            return;
          }
          if (field === 'llm') {
            // LLM-API exposure lives in its own overrides map (persisted
            // alongside states by saveConfig); the effective map mirrors it
            // immediately so the re-render shows the new value.
            toolLlm[t.name] = e.target.checked;
            toolLlmOverrides[t.name] = e.target.checked;
            scheduleSave();
            render();
            return;
          }
          const currentState = getState(t.name);
          let newState = currentState;
          if (field === 'enabled') {
            if (!e.target.checked) newState = 'disabled';
            else newState = (currentState === 'pinned') ? 'pinned' : 'enabled';
          } else if (field === 'pinned') {
            newState = e.target.checked ? 'pinned' : 'enabled';
          }
          toolStates[t.name] = newState;
          scheduleSave();
          render();
        });
      });
      toolsDiv.appendChild(div);
    });

    group.appendChild(header);
    group.appendChild(toolsDiv);
    container.appendChild(group);
  });

  document.getElementById('summary').innerHTML =
    `<span>${total} total</span>` +
    `<span style="color:var(--success)">${enabledCount} enabled</span>` +
    `<span style="color:var(--accent)">${pinnedCount} pinned</span>` +
    `<span style="color:var(--danger)">${disabledCount} disabled</span>`;

  // ``render()`` rebuilds the entire ``.tool`` DOM, so any
  // ``hidden`` class previously applied by ``applyToolSearch`` is
  // wiped. The search ``<input>`` is a separate element and keeps
  // its value across the rebuild — re-apply the filter so the
  // visible list matches what the user has typed. Otherwise
  // toggling a setting on a filtered tool snaps the full list back
  // even though the search box still shows the query.
  applyToolSearch();
}

function scheduleSave() {
  clearTimeout(saveTimer);
  updateStatus('Unsaved changes...');
  saveTimer = setTimeout(saveConfig, 800);
}

async function saveConfig() {
  updateStatus('Saving...');
  let resp;
  try {
    resp = await fetch('./api/settings/tools', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({states: toolStates, llm_api: toolLlmOverrides}),
    });
  } catch (e) {
    // Auto-save is fire-and-forget (scheduleSave -> setTimeout); without
    // this catch a network rejection would be unhandled and only reach the
    // visually-hidden #status region, leaving a sighted user with no signal.
    updateStatus('Save failed: ' + e.message, false, true);
    return;
  }
  if (resp.ok) {
    // LLM-API-exposure-only saves apply live (stamped per tools/list);
    // enable/disable/pin changes still need a restart. The server tells
    // us which happened.
    let restartRequired = true;
    try {
      const saved = await resp.json();
      restartRequired = saved.restart_required !== false;
    } catch (e) { /* keep the conservative default */ }
    if (restartRequired) {
      updateStatus('Saved. Restart required.', true);
      markRestartRequired();
    } else {
      updateStatus('Saved. LLM API exposure applies on the next agent message.', true);
    }
  } else {
    // Surface the server's structured error when present (mirrors
    // saveAdvancedSettings / saveFeatureFlag) instead of a generic
    // "Save failed!" that hides why the write was rejected.
    let msg = 'Save failed!';
    try {
      const data = await resp.json();
      if (data?.error?.message) msg = 'Save failed: ' + data.error.message;
    } catch (_e) { /* non-JSON body — keep the generic message */ }
    updateStatus(msg, false, true);
  }
}

// Reflect success/error semantics on a status span for assistive tech:
// failures switch to role=alert/assertive so screen readers interrupt; all
// other updates stay role=status/polite (matching the static markup). (#1596)
function setStatusAlert(el, isError) {
  if (!el) return;
  el.setAttribute('role', isError ? 'alert' : 'status');
  el.setAttribute('aria-live', isError ? 'assertive' : 'polite');
}

function updateStatus(text, saved, isError) {
  const el = document.getElementById('status');
  // Terminal outcomes (a successful save or an error) are announced by the
  // toast, which is itself an ARIA live region (the .ha-toast element inside
  // #ha-toast-region).
  // Writing the same text to the #status live region too would make screen
  // readers announce it twice, so for toast cases route the announcement
  // solely through showToast and leave #status for the transient progress
  // states ("Saving…", "Unsaved changes…", "Loading…", "Loaded") that never
  // toast.
  if (saved || isError) {
    showToast(text, {isError: !!isError});
    return;
  }
  // Past the early return, ``saved`` and ``isError`` are always false —
  // only the transient progress states reach here — so the status span is
  // always the neutral role=status/polite variant.
  setStatusAlert(el, false);
  el.className = 'status';
  el.textContent = text;
}

// HA-style toast (mirrors ha-toast / showToast): one snackbar at a time,
// bottom-center, auto-dismiss after 4s (errors persist longer and get a
// dismiss button). Replace-on-new rather than stacking, so flipping several
// toggles in a row doesn't pile up a column of identical toasts.
let _toastTimer = null;
// Tracks the 200ms leave-animation removal so a toast reused inside that
// window (replace-on-new) isn't yanked from the DOM by the prior removal.
let _toastRemoveTimer = null;
function showToast(message, opts) {
  opts = opts || {};
  const isError = !!opts.isError;
  if (!message) return;
  let region = document.getElementById('ha-toast-region');
  if (!region) {
    // #ha-toast-region is a static positioned container in settings.html;
    // this create-if-missing path is just a harmless fallback.
    region = document.createElement('div');
    region.id = 'ha-toast-region';
    document.body.appendChild(region);
  }
  let toast = region.querySelector('.ha-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'ha-toast';
    const msg = document.createElement('span');
    msg.className = 'ha-toast-msg';
    toast.appendChild(msg);
    region.appendChild(toast);
  }
  // Cancel a pending leave-removal so reusing this element keeps it onscreen.
  clearTimeout(_toastRemoveTimer);
  toast.classList.remove('leaving');
  toast.classList.toggle('ha-toast-error', isError);
  // The toast element is itself the ARIA live region (its #ha-toast-region
  // parent is just a positioned container). Set role + aria-live before the
  // message text: assertive interrupts for errors, polite for routine
  // outcomes. updateStatus() routes terminal outcomes here only (not also to
  // the #status region), so each is announced exactly once.
  toast.setAttribute('role', isError ? 'alert' : 'status');
  toast.setAttribute('aria-live', isError ? 'assertive' : 'polite');
  toast.querySelector('.ha-toast-msg').textContent = message;
  // Dismiss button only on errors/persistent toasts — HA's auto-dismiss
  // "Saved" snackbar has none.
  let dismiss = toast.querySelector('.ha-toast-dismiss');
  if (isError && !dismiss) {
    dismiss = document.createElement('button');
    dismiss.className = 'ha-toast-dismiss';
    dismiss.setAttribute('aria-label', 'Dismiss');
    dismiss.textContent = '×';
    dismiss.addEventListener('click', () => _removeToast(toast));
    toast.appendChild(dismiss);
  } else if (!isError && dismiss) {
    dismiss.remove();
  }
  clearTimeout(_toastTimer);
  const duration = opts.duration || (isError ? 8000 : 4000);
  _toastTimer = setTimeout(() => _removeToast(toast), duration);
}
function _removeToast(toast) {
  if (!toast) return;
  clearTimeout(_toastTimer);
  clearTimeout(_toastRemoveTimer);
  toast.classList.add('leaving');
  _toastRemoveTimer = setTimeout(() => { if (toast.parentNode) toast.remove(); }, 200);
}

function applyToolSearch() {
  // Read the current search query directly from the DOM rather than
  // taking it as a parameter — ``render()`` calls this after rebuilding
  // the tool DOM and needs to use whatever the user currently has
  // typed without coordinating with the input event.
  const q = (document.getElementById('search').value || '').toLowerCase();
  document.querySelectorAll('.tool').forEach(el => {
    const match = !q || el.dataset.name.includes(q) || el.dataset.title.includes(q);
    el.classList.toggle('hidden', !match);
  });
  document.querySelectorAll('.group').forEach(g => {
    const tools = g.querySelector('.group-tools');
    const visible = tools.querySelectorAll('.tool:not(.hidden)').length;
    g.style.display = visible ? '' : 'none';
    if (q && visible) {
      tools.classList.add('open');
      g.querySelector('.group-chevron').classList.add('open');
    }
  });
}

document.getElementById('search').addEventListener('input', applyToolSearch);

document.getElementById('restartBtn').addEventListener('click', restartAddon);

// ===== Backups tab =====
let backupEntries = [];
let backupConfigFields = [];

const BACKUP_FIELD_LABELS = {
  enable_auto_backup: {
    label: 'Auto-backup edits',
    help: 'Capture a snapshot before every wrapped write/destructive tool call.',
  },
  auto_backup_throttle_minutes: {
    label: 'Throttle (minutes)',
    help: 'Per-entity throttle. 0 = backup every write; N>0 = at most one per N minutes per entity. Range 0–1440.',
  },
  auto_backup_retain_per_entity: {
    label: 'Retain per entity',
    help: 'Maximum snapshots kept per entity (1–10000). Older ones rotate out.',
  },
  auto_backup_dir: {
    label: 'Backup directory override',
    help: 'Empty = default (/data/ha_mcp_backups in the App (add-on), $XDG_DATA_HOME/ha_mcp/backups otherwise). Override with an absolute path.',
  },
  auto_backup_calendar_lookahead_days: {
    label: 'Calendar lookahead (days)',
    help: 'How far ahead to query for calendar events when capturing pre-edit snapshots. Range 1–365.',
  },
};

const BACKUP_ORIGIN_LABELS = {
  addon: 'Synced to Supervisor. Restart required after save.',
  env: null,  // banner generated dynamically with the env var name
  file: 'Persisted locally; takes effect immediately.',
  default: 'Using default; first save creates a local override file.',
};

async function loadBackupConfig() {
  const formEl = document.getElementById('backupConfigForm');
  const actionsEl = document.getElementById('backupConfigActions');
  try {
    const resp = await fetch('./api/settings/backup-config');
    if (!resp.ok) {
      formEl.innerHTML = '<div class="backup-empty">Could not load backup settings.</div>';
      actionsEl.style.display = 'none';
      return;
    }
    const data = await resp.json();
    backupConfigFields = data.fields || [];
    if (typeof data.is_addon === 'boolean') {
      IS_ADDON_MODE = data.is_addon;
    }
  } catch (_e) {
    formEl.innerHTML = '<div class="backup-empty">Backup settings unavailable.</div>';
    actionsEl.style.display = 'none';
    return;
  }
  renderBackupConfig();
  actionsEl.style.display = backupConfigFields.some(f => f.editable) ? '' : 'none';
}

function renderBackupConfig() {
  const formEl = document.getElementById('backupConfigForm');
  formEl.innerHTML = '';
  backupConfigFields.forEach(f => {
    const meta = BACKUP_FIELD_LABELS[f.field] || { label: f.field, help: '' };
    const row = document.createElement('div');
    row.className = 'backup-field';
    let controlHtml;
    if (typeof f.value === 'boolean') {
      controlHtml = `<input type="checkbox" name="backup:${escapeHtml(f.field)}" data-field="${escapeHtml(f.field)}" aria-labelledby="label-backup-${escapeHtml(f.field)}" ${f.value ? 'checked' : ''} ${f.editable ? '' : 'disabled'}>`;
    } else if (typeof f.value === 'string') {
      // Path / freeform string fields (auto_backup_dir).
      controlHtml = `<input type="text" name="backup:${escapeHtml(f.field)}" data-field="${escapeHtml(f.field)}" aria-labelledby="label-backup-${escapeHtml(f.field)}" value="${escapeHtml(String(f.value ?? ''))}" ${f.editable ? '' : 'disabled'}>`;
    } else {
      let min = 1;
      let max = 10000;
      if (f.field === 'auto_backup_throttle_minutes') { min = 0; max = 1440; }
      else if (f.field === 'auto_backup_calendar_lookahead_days') { min = 1; max = 365; }
      controlHtml = `<input type="number" name="backup:${escapeHtml(f.field)}" data-field="${escapeHtml(f.field)}" aria-labelledby="label-backup-${escapeHtml(f.field)}" value="${Number(f.value)}" min="${min}" max="${max}" ${f.editable ? '' : 'disabled'}>`;
    }
    let originMsg;
    if (f.origin === 'env') {
      originMsg = envLockedNoteHtml(f.env_var, f.field);
    } else {
      originMsg = BACKUP_ORIGIN_LABELS[f.origin] || '';
    }
    const lockedBadge = f.editable ? '' : `<span class="backup-field-locked">env-locked</span>`;
    row.innerHTML =
      `<span class="backup-field-label" id="label-backup-${escapeHtml(f.field)}">${escapeHtml(meta.label)}</span>` +
      `<span class="backup-field-control">${controlHtml}</span>` +
      lockedBadge +
      `<span class="backup-field-help">${escapeHtml(meta.help)}${originMsg ? '. ' + originMsg : ''}</span>`;
    formEl.appendChild(row);
  });
}

async function saveBackupConfig() {
  const btn = document.getElementById('backupConfigSave');
  const statusEl = document.getElementById('backupConfigStatus');
  const payload = {};
  backupConfigFields.forEach(f => {
    if (!f.editable) return;
    const input = document.querySelector(`#backupConfigForm input[data-field="${f.field}"]`);
    if (!input) return;
    if (input.type === 'checkbox') {
      payload[f.field] = input.checked;
    } else if (input.type === 'text') {
      payload[f.field] = input.value;
    } else {
      const n = parseInt(input.value, 10);
      if (!isNaN(n)) payload[f.field] = n;
    }
  });
  if (Object.keys(payload).length === 0) {
    statusEl.textContent = 'Nothing editable.';
    return;
  }
  btn.disabled = true;
  setStatusAlert(statusEl, false);
  statusEl.textContent = 'Saving…';
  try {
    const resp = await fetch('./api/settings/backup-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      btn.disabled = false;
      let msg = 'Save failed';
      if (data && data.error) {
        if (typeof data.error === 'string') msg = data.error;
        else if (data.error.message) msg = data.error.message;
      }
      setStatusAlert(statusEl, true);
      statusEl.textContent = msg;
      showToast(msg, {isError: true});
      return;
    }
    btn.disabled = false;
    if (data.restart_required) {
      // Unified restart flow — save persists but does NOT auto-restart.
      // Surface the cross-tab restart-required banner; user picks the
      // moment via the global Restart Add-on button.
      //
      // Don't reload the form here. In addon mode the GET reads
      // env-derived ``get_global_settings()`` values which are still
      // stale (Supervisor has the new options but ``start.py``
      // doesn't re-derive env vars until the next addon boot). Reloading
      // would snap the form back to old values, look like the save
      // reverted, and clobber any further edits the user wanted to
      // bundle before clicking Restart.
      statusEl.textContent = 'Saved. Restart required.';
      showToast('Saved. Restart required.');
      markRestartRequired();
    } else {
      statusEl.textContent = 'Saved.';
      showToast('Saved.');
      // Refresh display so origins update (default → file, etc.).
      loadBackupConfig();
      loadBackups();
    }
  } catch (err) {
    btn.disabled = false;
    setStatusAlert(statusEl, true);
    statusEl.textContent = 'Network error: ' + String(err);
    showToast('Network error: ' + String(err), {isError: true});
  }
}

// ---- Custom filesystem directories (issue #1567) ---------------------------
// The list lives in the ha_mcp_tools custom component; the GET/POST endpoints
// proxy to it via authenticated HA service calls. Cached so the sub-form
// re-renders synchronously when the beta master / filesystem-tools toggle
// flips, without re-fetching.
// Consumed fields of the GET response: {available, paths, deny_floor, reason}
// (the endpoint also returns builtin_read_dirs/builtin_write_dirs, unused here).
let _fsCustomPathsData = null;

async function loadFsCustomPaths() {
  try {
    const resp = await fetch('./api/settings/fs-custom-paths');
    if (!resp.ok) {
      _fsCustomPathsData = {
        available: false,
        reason: `HTTP ${resp.status}`,
        paths: [],
        deny_floor: [],
      };
    } else {
      _fsCustomPathsData = await resp.json();
    }
  } catch (err) {
    _fsCustomPathsData = {
      available: false,
      reason: 'Network error: ' + String(err),
      paths: [],
      deny_floor: [],
    };
  }
  // Re-render the feature panel so the sub-form reflects the loaded data.
  if (Object.keys(_lastFeatureFlags).length) renderFeatureFlags(_lastFeatureFlags);
}

// Injected beneath the enable_filesystem_tools row in renderFeatureFlags.
// Second-level nested (under filesystem tools, itself beta-sub-nested under
// the master), dimmed when either the master beta or filesystem tools is off.
function renderFsCustomPathsSubForm(parentEl, masterOn, fsOn) {
  const lockedByGate = !masterOn || !fsOn;
  const d = _fsCustomPathsData;
  const row = document.createElement('div');
  row.className =
    'feature-row fs-custom-paths-sub' + (lockedByGate ? ' dimmed' : '');

  const info = document.createElement('div');
  info.className = 'feature-info';
  const denyList =
    d && Array.isArray(d.deny_floor) && d.deny_floor.length
      ? d.deny_floor.join(', ')
      : '.storage, secrets.yaml';
  info.innerHTML =
    `<div class="feature-name">Custom filesystem directories (advanced)</div>` +
    `<div class="feature-help">Extra directories (one per line) that the file tools may READ and WRITE, either relative to your config dir (e.g. <code>pyscript</code>, <code>python_scripts</code>) or an absolute HAOS sibling volume <code>/share</code>, <code>/media</code>, <code>/ssl</code>, <code>/backup</code> (or a subdirectory of one). Each entry grants both read and write. Applies immediately; no restart needed.</div>` +
    `<div class="feature-help">Always blocked (cannot be added): <code>${escapeHtml(denyList)}</code>, path traversal (<code>..</code>), and any absolute path outside the HAOS sibling volumes.</div>`;

  const control = document.createElement('div');
  control.className = 'feature-control';

  if (lockedByGate) {
    const note = document.createElement('div');
    note.className = 'feature-locked-note';
    note.textContent =
      'Enable beta features and filesystem tools above to edit.';
    control.appendChild(note);
  } else if (!d) {
    const note = document.createElement('div');
    note.className = 'feature-help';
    note.textContent = 'Loading…';
    control.appendChild(note);
  } else if (!d.available) {
    const note = document.createElement('div');
    note.className = 'feature-locked-note';
    note.textContent =
      d.reason || 'Custom directories are currently unavailable.';
    control.appendChild(note);
  } else {
    const ta = document.createElement('textarea');
    ta.id = 'fsCustomPathsInput';
    ta.rows = 4;
    ta.value = (d.paths || []).join('\n');
    const btn = document.createElement('button');
    btn.id = 'fsCustomPathsSave';
    btn.className = 'adv-save-btn';
    btn.textContent = 'Save directories';
    btn.addEventListener('click', saveFsCustomPaths);
    const status = document.createElement('div');
    status.id = 'fsCustomPathsStatus';
    status.className = 'feature-help';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    control.appendChild(ta);
    control.appendChild(btn);
    control.appendChild(status);
  }

  row.appendChild(info);
  row.appendChild(control);
  parentEl.appendChild(row);
}

async function saveFsCustomPaths() {
  const ta = document.getElementById('fsCustomPathsInput');
  const btn = document.getElementById('fsCustomPathsSave');
  const statusEl = document.getElementById('fsCustomPathsStatus');
  if (!ta || !btn || !statusEl) return;
  const paths = ta.value
    .split('\n')
    .map(s => s.trim())
    .filter(s => s.length);
  btn.disabled = true;
  setStatusAlert(statusEl, false);
  statusEl.textContent = 'Saving…';
  try {
    const resp = await fetch('./api/settings/fs-custom-paths', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    });
    const data = await resp.json();
    btn.disabled = false;
    if (!resp.ok || !data.success) {
      let msg = 'Save failed';
      if (data && data.error) {
        if (typeof data.error === 'string') msg = data.error;
        else if (data.error.message) msg = data.error.message;
      }
      setStatusAlert(statusEl, true);
      statusEl.textContent = msg;
      return;
    }
    // Reflect the component's canonical normalized list; drop any rejected.
    _fsCustomPathsData = {
      ...(_fsCustomPathsData || {}),
      available: true,
      paths: data.paths || [],
    };
    ta.value = (data.paths || []).join('\n');
    const rejected = data.rejected || [];
    statusEl.textContent = rejected.length
      ? `Saved. Rejected (blocked or invalid): ${rejected.join(', ')}`
      : 'Saved.';
  } catch (err) {
    btn.disabled = false;
    setStatusAlert(statusEl, true);
    statusEl.textContent = 'Network error: ' + String(err);
  }
}

async function loadBackups() {
  const params = new URLSearchParams();
  const d = document.getElementById('backupDomain').value.trim();
  const e = document.getElementById('backupEntity').value.trim();
  if (d) params.set('domain', d);
  if (e) params.set('entity_id', e);
  const stateEl = document.getElementById('backupState');
  const listEl = document.getElementById('backupList');
  try {
    const resp = await fetch('./api/settings/backups?' + params.toString());
    const data = await resp.json();
    if (!resp.ok || !data.success) {
      stateEl.innerHTML = '<span class="diff-rem">Error loading backups</span>';
      listEl.innerHTML = '';
      return;
    }
    backupEntries = data.backups || [];
    stateEl.innerHTML =
      `<span>Status: <strong>${data.enabled ? 'enabled' : 'disabled'}</strong></span>` +
      `<span>Throttle: <strong>${data.throttle_minutes} min</strong></span>` +
      `<span>Retain per entity: <strong>${data.retain_per_entity}</strong></span>` +
      `<span>Directory: <strong>${escapeHtml(data.backup_dir)}</strong></span>` +
      `<span>Total: <strong>${data.count}</strong></span>`;
    renderBackups();
  } catch (err) {
    stateEl.innerHTML = '<span class="diff-rem">Network error: ' + escapeHtml(String(err)) + '</span>';
    listEl.innerHTML = '';
  }
}

function renderBackups() {
  const listEl = document.getElementById('backupList');
  if (!backupEntries.length) {
    listEl.innerHTML = '<div class="backup-empty">No backups yet. Enable auto-backup in the App (add-on) config and edit an entity to create one.</div>';
    return;
  }
  listEl.innerHTML = '';
  backupEntries.forEach(b => {
    const row = document.createElement('div');
    row.className = 'backup-row';
    const ts = b.timestamp || '';
    const tsFmt = ts.length === 15
      ? ts.slice(0,4)+'-'+ts.slice(4,6)+'-'+ts.slice(6,8)+' '+ts.slice(9,11)+':'+ts.slice(11,13)+':'+ts.slice(13,15)
      : ts;
    row.innerHTML =
      `<div class="backup-row-info">` +
        `<div class="backup-row-name">${escapeHtml(b.name)}</div>` +
        `<div class="backup-row-meta">` +
          `<strong>${escapeHtml(b.domain)}</strong> · ` +
          `${escapeHtml(b.entity_id)} · ${tsFmt} · ${b.size} bytes` +
        `</div>` +
      `</div>` +
      `<div class="backup-row-actions">` +
        `<button data-act="view">View</button>` +
        `<button data-act="diff" class="secondary">Diff</button>` +
        `<button data-act="restore">Restore</button>` +
        `<button data-act="delete" class="danger">Delete</button>` +
      `</div>`;
    row.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', () => backupAction(btn.dataset.act, b.name));
    });
    listEl.appendChild(row);
  });
}

async function backupAction(act, name) {
  // Each branch wraps its fetch+json in try/catch so a network drop or an
  // HTML error body (json() throwing) surfaces a visible toast instead of
  // silently no-opping a destructive action — the bare rejection would only
  // reach the visually-hidden #status region. Mirrors loadBackups().
  if (act === 'view') {
    try {
      const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name));
      const data = await resp.json();
      if (!resp.ok) { alert(JSON.stringify(data)); return; }
      showModal('View: ' + name, '<pre>' + escapeHtml(yamlStringify(data.data)) + '</pre>');
    } catch (err) {
      showToast('Could not load backup "' + name + '": ' + String(err), {isError: true});
    }
  } else if (act === 'diff') {
    try {
      const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name) + '/diff');
      const data = await resp.json();
      if (!resp.ok) { alert(JSON.stringify(data)); return; }
      const html = (data.diff || '(identical)').split('\n').map(line => {
        let cls = '';
        if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) cls = 'diff-hdr';
        else if (line.startsWith('+')) cls = 'diff-add';
        else if (line.startsWith('-')) cls = 'diff-rem';
        return `<span class="${cls}">${escapeHtml(line)}</span>`;
      }).join('\n');
      showModal('Diff: ' + name, '<pre>' + html + '</pre>');
    } catch (err) {
      showToast('Could not diff backup "' + name + '": ' + String(err), {isError: true});
    }
  } else if (act === 'restore') {
    if (!confirm('Restore ' + name + '?\n\nThis will overwrite the current entity state. A safety backup of the current state is taken first.')) return;
    try {
      const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name) + '/restore', {method: 'POST'});
      const data = await resp.json();
      if (!resp.ok) { alert('Restore failed: ' + JSON.stringify(data)); return; }
      alert('Restored. Safety backup: ' + (data.data && data.data.safety_backup ? data.data.safety_backup : '(none)'));
      loadBackups();
    } catch (err) {
      showToast('Restore of "' + name + '" failed: ' + String(err), {isError: true});
    }
  } else if (act === 'delete') {
    if (!confirm('Delete ' + name + '? This cannot be undone.')) return;
    try {
      const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name), {method: 'DELETE'});
      if (!resp.ok) { const d = await resp.json(); alert('Delete failed: ' + JSON.stringify(d)); return; }
      loadBackups();
    } catch (err) {
      showToast('Delete of "' + name + '" failed: ' + String(err), {isError: true});
    }
  }
}

async function bulkDeleteBackups() {
  const d = document.getElementById('backupDomain').value.trim();
  const e = document.getElementById('backupEntity').value.trim();
  const days = prompt('Delete backups older than N days (leave blank to use current filters only):', '');
  const params = new URLSearchParams();
  if (d) params.set('domain', d);
  if (e) params.set('entity_id', e);
  if (days) params.set('older_than_days', days);
  if (!params.toString()) { alert('Set at least one filter (Domain, Entity, or age in days).'); return; }
  if (!confirm('Delete all backups matching: ' + params.toString() + '?')) return;
  const resp = await fetch('./api/settings/backups?' + params.toString(), {method: 'DELETE'});
  const data = await resp.json();
  if (!resp.ok) { alert('Bulk delete failed: ' + JSON.stringify(data)); return; }
  alert('Deleted ' + (data.count || 0) + ' backup(s)');
  loadBackups();
}

// Focus management for the snapshot modal (WAI-ARIA APG dialog pattern):
// remember the opener, move focus into the dialog on open, trap Tab inside
// it, close on Escape, and restore focus to the opener on close. This is a
// separate keydown handler from the tablist navigation handler below — it is
// added on open and removed on close so it never fires while the modal is
// shut.
let _modalOpener = null;
let _modalKeydownHandler = null;

function showModal(title, html) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = html;
  const backdrop = document.getElementById('modalBackdrop');
  // Defensive: if showModal is called while a modal is already open (a stale
  // handler still bound), drop the prior keydown listener first so trap
  // handlers don't accumulate on the backdrop.
  if (_modalKeydownHandler) {
    backdrop.removeEventListener('keydown', _modalKeydownHandler);
    _modalKeydownHandler = null;
  }
  _modalOpener = document.activeElement;
  backdrop.classList.add('show');
  const modal = backdrop.querySelector('.modal');
  const closeBtn = document.getElementById('modalClose');
  if (closeBtn) closeBtn.focus();
  _modalKeydownHandler = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeModal();
      return;
    }
    if (e.key !== 'Tab' || !modal) return;
    const focusable = modal.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
    if (!focusable.length) { e.preventDefault(); return; }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };
  backdrop.addEventListener('keydown', _modalKeydownHandler);
}
function closeModal() {
  const backdrop = document.getElementById('modalBackdrop');
  backdrop.classList.remove('show');
  if (_modalKeydownHandler) {
    backdrop.removeEventListener('keydown', _modalKeydownHandler);
    _modalKeydownHandler = null;
  }
  if (_modalOpener && typeof _modalOpener.focus === 'function') _modalOpener.focus();
  _modalOpener = null;
}

// Pretty-print the snapshot envelope for the view modal. The server
// returns the parsed YAML as JSON; indented JSON is the simplest
// readable form for the modal without pulling in a JS YAML library.
function yamlStringify(obj) { return JSON.stringify(obj, null, 2); }

document.getElementById('backupRefresh').addEventListener('click', loadBackups);
document.getElementById('backupBulkDelete').addEventListener('click', bulkDeleteBackups);
document.getElementById('backupConfigSave').addEventListener('click', saveBackupConfig);
document.getElementById('modalClose').addEventListener('click', closeModal);
document.getElementById('modalBackdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modalBackdrop') closeModal();
});

document.getElementById('stopSidecarBtn').addEventListener('click', stopSidecar);

// Feature-flag metadata (display labels + help text). Keyed by the
// Settings field name. The strings are intentionally copied verbatim
// from ``homeassistant-addon-dev/translations/en.yaml`` so the web
// UI and the add-on Configuration tab read identically — a user who
// flips between the two surfaces never wonders if the option name
// or warning text shifted meaning. Keep them in sync when one side
// changes; the addon-dev translations file is the source of truth.
const FEATURE_META = {
  enable_tool_search: {
    label: "Enable tool search",
    help: "Replace the full tool catalog with search-based discovery. Reduces idle context from ~46K to ~5K tokens. ⚠️ Do NOT enable this if you use Claude in Sonnet or Opus modes. Those models have their own built-in tool search / deferred tools, which conflicts with ours. To use ha-mcp's tool search with Claude, disable Claude's built-in tool search first; otherwise leave this off. Use this only with LLMs that lack native deferred tools (e.g. Claude Haiku, local OpenAI-compatible models) or with smaller context windows. Tools are found via ha_search_tools and executed via categorized proxies (read/write/delete). Requires restart to take effect.",
  },
  tool_search_max_results: {
    label: "Tool search max results",
    help: "Maximum number of tools returned by ha_search_tools when tool search is enabled. Lower values (2-3) save context tokens but may miss relevant tools. Range: 2-10. Requires restart.",
  },
  enable_tool_security_policies: {
    label: "Enable Tool Security Policies",
    help: "Opt-in middleware that gates high-stakes MCP tool calls behind user approval. When enabled, tools that match a rule in the Tool Security Policies tab require you to click Approve in the web UI before they run. Off by default. Per-tool rules with optional argument conditions are configured in the Tool Security Policies tab. Requires restart to take effect.",
  },
  read_only_mode: {
    label: "Read Only Mode",
    help: "Turns all write tools off and blocks tools from making any write or destructive calls. Mixed read/write tools (backups, Apps (add-ons), energy preferences, voice pipelines, and code mode when enabled) stay listed with their write operations blocked server-side. The AI gets a clear READ_ONLY_MODE error if it tries. Mirrors the toggle at the top of the Tools tab. Off by default. Requires restart to take effect (applies live in standalone HTTP mode).",
  },
  enable_mandatory_bps: {
    label: "Attach best-practice skills on writes",
    help: "Master switch for the write-tool skill content delivery feature (issue #1182). When enabled (default), the six config write tools (automations, scripts, scenes, helpers, dashboards, raw YAML) attach the canonical Home Assistant best-practice reference files under skill_content on every successful write, plus auto-embed any reference sections cited by best-practice warnings. Each tool also exposes a per-call MandatoryBPS parameter the agent can set to false on subsequent calls once it has the content. When this master switch is off, NO skill_content goes out regardless of the per-call parameter or BP warnings. Leave on if your LLM benefits from inline guidance; turn off to minimise tokens when using an LLM that has the best-practice files indexed via skills or another retrieval path. Requires restart to take effect.",
  },
  enable_strict_mandatory_bps: {
    label: "Strict best-practices mode",
    help: "Strict mode: prevents the client from using the tool until it can prove that it read the best practices. While on, the six best-practice write tools (automations, scripts, scenes, helpers, dashboards, raw YAML) are blocked and return an error directing the client to read the best-practices skill via ha_get_skill_guide and pass back the acknowledgment key it obtains there. Nested under \"Attach best-practice skills on writes\" above and inert while that parent toggle is off. Requires restart to take effect.",
  },
  // Master beta toggle — gates the 5 sub-flags below at runtime
  // (see config.py:_apply_feature_flag_overrides master gate). UI
  // dims sub-rows when this is off and re-renders live on flip.
  enable_beta_features: {
    label: "Enable beta features",
    help: "⚠ DANGER. These tools can PERMANENTLY DAMAGE your Home Assistant installation. They write to your YAML config, write to your filesystem, install custom components, run arbitrary sandboxed Python, and edit tool docstrings the AI sees. There is no warranty and no support guarantee. You enable them at your OWN RISK. Take a Home Assistant backup before turning this on, and never enable in production without one. Master toggle for the 5 experimental tools below; sub-toggles are dimmed and ignored at runtime while this is off (even a sub-flag set via env var is forced off until the master is on). Requires restart to take effect.",
  },
  enable_yaml_config_editing: {
    label: "Enable YAML config editing (beta)",
    help: "Beta feature, disabled by default. Allows AI assistants to add, replace, or remove top-level keys in configuration.yaml and packages/*.yaml. Only whitelisted keys are allowed (e.g., template, sensor, command_line, mqtt, knx); core keys like homeassistant, http, and recorder are blocked. Each edit validates YAML syntax, runs a config check, and creates an automatic backup. Changes to most keys require a full HA restart to take effect. See docs/beta.md for known limitations. Dedicated tools (automations, scripts, scenes, helpers, template sensors) should be preferred when available.",
  },
  enable_yaml_edit_confirm: {
    label: "Require confirmation for YAML edits (diff preview)",
    help: "Sub-toggle of YAML config editing, ON by default (recommended). The first ha_config_set_yaml call returns a unified diff of exactly what would change on disk plus a confirm token and writes nothing; the edit only lands when the call is repeated with that token. Exists so unintended changes outside the requested edit are caught before they reach disk (issue #1720). Turn off only to save one round-trip per edit.",
  },
  enable_yaml_packages_automation: {
    label: "Allow automation in packages/*.yaml",
    help: "Sub-toggle of YAML config editing. When on, ha_config_set_yaml accepts yaml_path='automation' inside packages/*.yaml. When off, the call is rejected both client-side and server-side. The storage-mode tool (ha_config_set_automation) is unaffected. Default OFF; only takes effect when \"Enable YAML config editing\" above is also on.",
  },
  enable_yaml_packages_script: {
    label: "Allow script in packages/*.yaml",
    help: "Sub-toggle of YAML config editing. When on, ha_config_set_yaml accepts yaml_path='script' inside packages/*.yaml. When off, the call is rejected both client-side and server-side. The storage-mode tool (ha_config_set_script) is unaffected. Default OFF; only takes effect when \"Enable YAML config editing\" above is also on.",
  },
  enable_yaml_packages_scene: {
    label: "Allow scene in packages/*.yaml",
    help: "Sub-toggle of YAML config editing. When on, ha_config_set_yaml accepts yaml_path='scene' inside packages/*.yaml. When off, the call is rejected both client-side and server-side. The storage-mode tool (ha_config_set_scene) is unaffected. Default OFF; only takes effect when \"Enable YAML config editing\" above is also on.",
  },
  enable_filesystem_tools: {
    label: "Enable filesystem tools (beta)",
    help: "Sets HAMCP_ENABLE_FILESYSTEM_TOOLS=true. Enables direct file read/write access to your Home Assistant filesystem. WARNING: This gives the MCP server sensitive direct file access to your system. Only enable if you trust the AI assistant with file operations. Requires restart to take effect.",
  },
  enable_custom_component_integration: {
    label: "Enable custom component integration (beta)",
    help: "Sets HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true. Enables the ha_install_mcp_tools installer tool, which can help install the ha_mcp_tools custom component. This setting does not control whether the MCP server loads or interacts with the custom component, and it is not required for filesystem tools to function. Only enable if you want to allow the AI assistant to use the installer tool. Requires restart to take effect.",
  },
  enable_code_mode: {
    label: "Enable code-mode sandbox (beta)",
    help: "Beta feature, disabled by default. Enables ha_manage_custom_tool, a sandboxed Python interpreter (pydantic-monty) that lets AI assistants write/run/save/delete custom tools when no built-in tool covers the request. Sandbox cannot touch the filesystem or arbitrary network, but CAN call any registered MCP tool, hit the HA REST API, or send HA WebSocket commands, effectively 'do whatever existing tools allow you to do, in any combination'. Saved tools persist and are visible to any client that can connect to ha-mcp. See docs/beta.md for known limitations. Requires restart to take effect.",
  },
  enable_lite_docstrings: {
    label: "Enable lite tool docstrings (beta)",
    help: "Beta feature, disabled by default. Replaces the docstrings on a handful of heavy ha-mcp tools (automations, scripts, scenes, helpers, dashboards, ha_call_service, ha_config_set_yaml) with shorter variants that defer schema and example detail to the ha_get_skill_guide tool (or its skill:// resource). WARNING: this reduces idle token usage, but may degrade LLM performance. The trimmed descriptions rely on the LLM actually calling the skill tool or reading the skill resource for detail, which is not guaranteed (some models will skip the extra tool call and end up with less guidance than they had before). Best paired with a client that supports MCP resources or with enable_tool_search. Requires restart to take effect.",
  },
  enable_dashboard_screenshot: {
    label: "Enable dashboard screenshot mode (beta)",
    help: "Beta feature, disabled by default. Adds the ha_get_dashboard_screenshot tool plus include_screenshot / return_screenshot options on the dashboard get/set tools, so AI assistants can see a rendered PNG of a Lovelace dashboard (e.g. to verify one they just created). Rendering runs in a separate, opt-in engine, balloob's \"Puppet\" App (add-on) (headless Chromium), which you install once (add balloob's App (add-on) repository, then install \"Puppet\") and give a long-lived access token; on Docker/Container deployments you run that engine as a sidecar and set HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL. Nothing heavy is installed unless you both enable this and install the engine. Requires restart to take effect. REQUIRES the master \"Enable beta features\" toggle above (and in the web UI) to be on. Otherwise this sub-flag is ignored at runtime regardless of its value here.",
  },
};

// The beta sub-flag fields gated by the master beta toggle. Populated
// from the ``beta_sub_flags`` array in the /api/settings/features
// response so the JS stays in sync with Python's
// ``config.BETA_FEATURE_FIELDS`` without duplicating the name list here.
let BETA_SUB_FLAGS = new Set();

// Sub-flags of ``enable_yaml_config_editing`` (confirm-flow toggle +
// per-key packages gates). They ARE in BETA_SUB_FLAGS (it mirrors
// config.BETA_FEATURE_FIELDS verbatim), but the main render pass skips
// them via the includes() guard below, and renderYamlPackagesSubRows
// re-renders them nested beneath their parent — so they never appear
// as standalone beta-sub rows.
const YAML_PACKAGES_SUB_FLAGS = [
  'enable_yaml_edit_confirm',
  'enable_yaml_packages_automation',
  'enable_yaml_packages_script',
  'enable_yaml_packages_scene',
];

// Sub-flag of ``enable_mandatory_bps`` (strict best-practices mode).
// Unlike YAML_PACKAGES_SUB_FLAGS this is NON-beta — it is NOT in
// BETA_SUB_FLAGS and its only gate is the parent toggle. The main
// render pass skips it via the includes() guard below, and
// renderMandatoryBpsSubRows re-renders it nested beneath its parent so
// it never appears as a standalone top-level row.
const MANDATORY_BPS_SUB_FLAGS = [
  'enable_strict_mandatory_bps',
];

// Cached add-on flag. Each settings endpoint (/api/settings/features,
// /api/settings/advanced, /api/settings/backup-config) returns
// ``is_addon`` so the env-locked banner copy can adapt — the addon
// Configuration UI cannot "unset env vars," so the standalone-mode
// "unset env var" copy is actively misleading there.
let IS_ADDON_MODE = false;

const ORIGIN_LOCKED_NOTE = {
  env: 'Set via environment variable; unset it to edit here.',
  // addon-origin fields are editable: save POSTs through Supervisor
  // /addons/self/options and triggers a restart so both surfaces stay
  // in sync. No locked note needed.
};

const ORIGIN_INFO_NOTE = {
  addon: 'Synced to the App (add-on) Configuration tab. Restart required after save.',
};

// Compose the env-locked banner text for one field. Addon-mode copy
// avoids the misleading "unset it to edit here" — operators in HA
// addon mode have no env-var surface to unset; the var was set
// either by start.py from /data/options.json or by Supervisor itself
// (and in either case the addon Configuration tab is the place to
// change it). The master `enable_beta_features` row uses different
// copy because it's now schema-bound on dev — origin='env' there
// only fires on the legacy-bridge path (an older install whose
// options.json doesn't carry the master key yet, where start.py's
// truthy-sub-flag fallback wrote ENABLE_BETA_FEATURES=true).
function envLockedNoteHtml(envVar, fieldName) {
  const envVarTag = `<code>${escapeHtml(envVar)}</code>`;
  if (!IS_ADDON_MODE) {
    return `Set via env var ${envVarTag}; unset it to edit here.`;
  }
  if (fieldName === 'enable_beta_features') {
    return (
      `Auto-enabled in App (add-on) mode (legacy bridge; your options.json ` +
      `predates the master toggle schema entry). Set ` +
      `<code>enable_beta_features</code> explicitly in the App (add-on) ` +
      `Configuration tab to take direct control. (env: ${envVarTag})`
    );
  }
  return (
    `Set by the App (add-on) runtime environment, managed by Home Assistant ` +
    `Supervisor; cannot be changed from this web UI. (env: ${envVarTag})`
  );
}

async function loadFeatureFlags() {
  let resp;
  try {
    resp = await fetch('./api/settings/features');
  } catch (err) {
    console.error('loadFeatureFlags fetch failed:', err);
    // Surface as a row inside the panel rather than the page status —
    // the panel is collapsible and the user can ignore this if they
    // do not care about feature flags right now.
    document.getElementById('featuresBody').innerHTML =
      '<div class="feature-row"><div class="feature-help">' +
      'Feature flags unavailable (network error reaching ' +
      '/api/settings/features).</div></div>';
    return;
  }
  if (!resp.ok) {
    document.getElementById('featuresBody').innerHTML =
      `<div class="feature-row"><div class="feature-help">` +
      `Feature flags unavailable (HTTP ${resp.status}).</div></div>`;
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (err) {
    console.error('loadFeatureFlags JSON parse failed:', err);
    document.getElementById('featuresBody').innerHTML =
      '<div class="feature-row"><div class="feature-help">' +
      'Feature flags response was not valid JSON.</div></div>';
    return;
  }
  if (Array.isArray(data.beta_sub_flags)) {
    BETA_SUB_FLAGS = new Set(data.beta_sub_flags);
  }
  if (typeof data.is_addon === 'boolean') {
    IS_ADDON_MODE = data.is_addon;
  }
  renderFeatureFlags(data.flags || {});
}

// Cache of last-fetched flags so we can re-render synchronously when
// the user flips the master beta toggle (without round-tripping to the
// server). Server-side master-off rejection still applies on save.
let _lastFeatureFlags = {};

function renderFeatureFlags(flags) {
  _lastFeatureFlags = flags;
  const body = document.getElementById('featuresBody');
  const betaBody = document.getElementById('betaBody');
  body.innerHTML = '';
  if (betaBody) betaBody.innerHTML = '';
  // Master beta state — drives the .dimmed class on sub-rows. Read
  // from the live cache so we get the post-flip value if the user
  // just toggled the master.
  const masterOn = !!(flags.enable_beta_features && flags.enable_beta_features.value);
  // Render in the order FEATURE_META declares — gives consistent
  // grouping (Tool Search rows together, master then beta sub-rows
  // together) regardless of dict iteration order returned by the
  // server.
  Object.keys(FEATURE_META).forEach(fieldName => {
    const f = flags[fieldName];
    if (!f) return;
    // Skip yaml-packages sub-rows in the main pass — they're rendered
    // by renderYamlPackagesSubRows below right after their parent so
    // the nesting reads in source order.
    if (YAML_PACKAGES_SUB_FLAGS.includes(fieldName)) return;
    // Same for the strict-mode sub-row — rendered by
    // renderMandatoryBpsSubRows right after its enable_mandatory_bps parent.
    if (MANDATORY_BPS_SUB_FLAGS.includes(fieldName)) return;
    const meta = FEATURE_META[fieldName];
    const isMaster = fieldName === 'enable_beta_features';
    const isBetaSub = BETA_SUB_FLAGS.has(fieldName);
    // Beta rows render into the dedicated bottom-of-panel betaBody
    // container so the dangerous block sits below the safer
    // settings. Fallback to the main body if the dedicated container
    // is missing (tests that don't include it in MIN_DOM).
    const targetBody = (isMaster || isBetaSub) && betaBody ? betaBody : body;
    const row = document.createElement('div');
    let cls = 'feature-row' + (f.editable ? '' : ' locked');
    if (isMaster) cls += ' beta-master-row';
    if (isBetaSub) cls += ' beta-sub' + (masterOn ? '' : ' dimmed');
    row.className = cls;

    const info = document.createElement('div');
    info.className = 'feature-info';
    const lockedNote = !f.editable
      ? `<div class="feature-locked-note">` +
        (f.origin === 'env'
          ? envLockedNoteHtml(f.env_var, fieldName)
          : escapeHtml(ORIGIN_LOCKED_NOTE[f.origin] || '')) +
        `</div>`
      : '';
    const infoNote = f.editable && ORIGIN_INFO_NOTE[f.origin]
      ? `<div class="feature-locked-note">` +
        `${escapeHtml(ORIGIN_INFO_NOTE[f.origin])}</div>`
      : '';
    info.innerHTML =
      `<div class="feature-name" id="label-feature-${fieldName}">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(meta.help)}</div>` +
      lockedNote + infoNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    // Beta sub-flags are disabled at the input level when the master
    // is off, in addition to the .dimmed class on the row. Server-
    // side rejection (409 in _save_feature_flags) is the
    // authoritative guard; this is UX feedback.
    const lockedByMaster = isBetaSub && !masterOn;
    if (f.type === 'bool') {
      const label = document.createElement('label');
      label.className = 'switch';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'feature:' + fieldName;
      input.checked = !!f.value;
      input.disabled = !f.editable || lockedByMaster;
      input.setAttribute('aria-labelledby', 'label-feature-' + fieldName);
      input.addEventListener('change', () => {
        // Master flip → re-render the panel synchronously so the
        // sub-row dimming reflects the new state immediately. The
        // save POST still proceeds in the background.
        //
        // Sub-flag VALUES are intentionally NOT flipped here. Neither
        // is the server's persisted state — the runtime gate in
        // ``_apply_feature_flag_overrides`` is the only thing that
        // forces sub-flags off when master is off, and it does so
        // without mutating the saved file values. Result: turning the
        // master off then back on restores the user's prior sub-flag
        // selections automatically, which is the intended UX for an
        // opt-in beta surface.
        if (isMaster) {
          if (_lastFeatureFlags[fieldName]) {
            _lastFeatureFlags[fieldName] = {
              ..._lastFeatureFlags[fieldName],
              value: input.checked,
            };
            renderFeatureFlags(_lastFeatureFlags);
          }
        }
        // Re-render on enable_yaml_config_editing flip so the 3
        // packages sub-rows dim/undim immediately. Same pattern as the
        // master flip above — value is mutated in the live cache and
        // the panel re-renders synchronously while the save POST runs
        // in the background.
        if (fieldName === 'enable_yaml_config_editing') {
          if (_lastFeatureFlags[fieldName]) {
            _lastFeatureFlags[fieldName] = {
              ..._lastFeatureFlags[fieldName],
              value: input.checked,
            };
            renderFeatureFlags(_lastFeatureFlags);
          }
        }
        // Re-render on enable_mandatory_bps flip so the strict-mode
        // sub-row dims/undims immediately. Same live-cache pattern as
        // the master and yaml-config flips above.
        if (fieldName === 'enable_mandatory_bps') {
          if (_lastFeatureFlags[fieldName]) {
            _lastFeatureFlags[fieldName] = {
              ..._lastFeatureFlags[fieldName],
              value: input.checked,
            };
            renderFeatureFlags(_lastFeatureFlags);
          }
        }
        saveFeatureFlag(fieldName, input.checked);
      });
      const slider = document.createElement('span');
      slider.className = 'slider';
      label.appendChild(input);
      label.appendChild(slider);
      control.appendChild(label);
    } else if (f.type === 'int') {
      const input = document.createElement('input');
      input.type = 'number';
      input.name = 'feature:' + fieldName;
      input.value = f.value;
      if (typeof f.min === 'number') input.min = f.min;
      if (typeof f.max === 'number') input.max = f.max;
      input.disabled = !f.editable;
      input.setAttribute('aria-labelledby', 'label-feature-' + fieldName);
      input.addEventListener('change', () => {
        const parsed = parseInt(input.value, 10);
        if (Number.isFinite(parsed)) saveFeatureFlag(fieldName, parsed);
      });
      control.appendChild(input);
    }

    row.appendChild(info);
    row.appendChild(control);
    targetBody.appendChild(row);

    // Chunk 3b — after rendering the enable_code_mode row, inject the
    // 5 code_mode_* sub-numeric rows from the advanced cache. These
    // are second-level-nested (under enable_code_mode, which is itself
    // beta-sub-nested under the master), dimmed when either the master
    // is off or code_mode itself is off. Sub-rows go into the same
    // target body as the parent so the beta block stays grouped at
    // bottom.
    if (fieldName === 'enable_code_mode') {
      const codeModeOn = !!f.value;
      renderCodeModeSubRows(targetBody, masterOn, codeModeOn);
    }
    // After rendering the enable_yaml_config_editing parent, inject
    // its 3 per-key sub-rows (automation/script/scene). Dimmed when
    // either the master beta is off (parent forced off) or the parent
    // itself is off.
    if (fieldName === 'enable_yaml_config_editing') {
      const parentOn = !!f.value;
      renderYamlPackagesSubRows(flags, targetBody, masterOn, parentOn);
    }
    // After the enable_mandatory_bps parent row, inject its strict-mode
    // sub-row. This whole group is non-beta, so the only gate is the
    // parent toggle (no master beta involved).
    if (fieldName === 'enable_mandatory_bps') {
      const parentOn = !!f.value;
      renderMandatoryBpsSubRows(flags, targetBody, parentOn);
    }
    // After the enable_filesystem_tools row, inject the custom-directories
    // editor (issue #1567). Dimmed when either the master beta is off or
    // filesystem tools itself is off. The list is component-owned and fetched
    // separately via loadFsCustomPaths(); this renders from that cache.
    if (fieldName === 'enable_filesystem_tools') {
      const fsOn = !!f.value;
      renderFsCustomPathsSubForm(targetBody, masterOn, fsOn);
    }
  });
}

function renderYamlPackagesSubRows(flags, parentEl, masterOn, parentOn) {
  YAML_PACKAGES_SUB_FLAGS.forEach(fieldName => {
    const f = flags[fieldName];
    if (!f) return;
    const meta = FEATURE_META[fieldName] || { label: fieldName, help: '' };
    const lockedByGate = !masterOn || !parentOn;
    const row = document.createElement('div');
    row.className = 'feature-row yaml-packages-sub' + (lockedByGate ? ' dimmed' : '');

    const info = document.createElement('div');
    info.className = 'feature-info';
    const lockedNote = !f.editable
      ? `<div class="feature-locked-note">` +
        (f.origin === 'env'
          ? envLockedNoteHtml(f.env_var, fieldName)
          : escapeHtml(ORIGIN_LOCKED_NOTE[f.origin] || '')) +
        `</div>`
      : '';
    const infoNote = f.editable && ORIGIN_INFO_NOTE[f.origin]
      ? `<div class="feature-locked-note">` +
        `${escapeHtml(ORIGIN_INFO_NOTE[f.origin])}</div>`
      : '';
    info.innerHTML =
      `<div class="feature-name">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(meta.help)}</div>` +
      lockedNote + infoNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    const label = document.createElement('label');
    label.className = 'switch';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'feature:' + fieldName;
    input.checked = !!f.value;
    input.disabled = !f.editable || lockedByGate;
    input.addEventListener('change', () => {
      // Keep the cached flag value in sync (parity with the parent/master
      // row handlers) so a later parent flip — which re-renders from
      // _lastFeatureFlags — reflects this sub-row's current state rather
      // than a stale value.
      if (_lastFeatureFlags[fieldName]) {
        _lastFeatureFlags[fieldName] = {
          ..._lastFeatureFlags[fieldName],
          value: input.checked,
        };
      }
      saveFeatureFlag(fieldName, input.checked);
    });
    const slider = document.createElement('span');
    slider.className = 'slider';
    label.appendChild(input);
    label.appendChild(slider);
    control.appendChild(label);

    row.appendChild(info);
    row.appendChild(control);
    parentEl.appendChild(row);
  });
}

function renderMandatoryBpsSubRows(flags, parentEl, parentOn) {
  MANDATORY_BPS_SUB_FLAGS.forEach(fieldName => {
    const f = flags[fieldName];
    if (!f) return;
    const meta = FEATURE_META[fieldName] || { label: fieldName, help: '' };
    // Strict mode is a plain child of enable_mandatory_bps — no beta
    // master in play, so the only gate is the parent toggle.
    const lockedByGate = !parentOn;
    const row = document.createElement('div');
    row.className = 'feature-row mandatory-bps-sub' + (lockedByGate ? ' dimmed' : '');

    const info = document.createElement('div');
    info.className = 'feature-info';
    const lockedNote = !f.editable
      ? `<div class="feature-locked-note">` +
        (f.origin === 'env'
          ? envLockedNoteHtml(f.env_var, fieldName)
          : escapeHtml(ORIGIN_LOCKED_NOTE[f.origin] || '')) +
        `</div>`
      : '';
    const infoNote = f.editable && ORIGIN_INFO_NOTE[f.origin]
      ? `<div class="feature-locked-note">` +
        `${escapeHtml(ORIGIN_INFO_NOTE[f.origin])}</div>`
      : '';
    info.innerHTML =
      `<div class="feature-name" id="label-feature-${fieldName}">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(meta.help)}</div>` +
      lockedNote + infoNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    const label = document.createElement('label');
    label.className = 'switch';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'feature:' + fieldName;
    input.checked = !!f.value;
    input.disabled = !f.editable || lockedByGate;
    input.setAttribute('aria-labelledby', 'label-feature-' + fieldName);
    input.addEventListener('change', () => {
      // Keep the cached flag value in sync (parity with the parent row
      // handler) so a later parent flip — which re-renders from
      // _lastFeatureFlags — reflects this sub-row's current state.
      if (_lastFeatureFlags[fieldName]) {
        _lastFeatureFlags[fieldName] = {
          ..._lastFeatureFlags[fieldName],
          value: input.checked,
        };
      }
      saveFeatureFlag(fieldName, input.checked);
    });
    const slider = document.createElement('span');
    slider.className = 'slider';
    label.appendChild(input);
    label.appendChild(slider);
    control.appendChild(label);

    row.appendChild(info);
    row.appendChild(control);
    parentEl.appendChild(row);
  });
}

function renderCodeModeSubRows(parentEl, masterOn, codeModeOn) {
  const cmRows = (_advancedFields || []).filter(x => x.section === 'beta_codemode');
  cmRows.forEach(f => {
    const meta = ADVANCED_FIELD_META[f.field] || { label: f.field, help: '' };
    const row = document.createElement('div');
    const lockedByGate = !masterOn || !codeModeOn;
    const dimmed = lockedByGate;
    row.className = 'feature-row codemode-sub' + (dimmed ? ' dimmed' : '');

    const info = document.createElement('div');
    info.className = 'feature-info';
    // code_mode_saved_tools_path needs honest, field-specific copy:
    //  - add-on mode: hardcoded by start.py (setdefault to /data); not
    //    Supervisor-managed and absent from the addon schema, so it
    //    genuinely can't be changed — don't imply a lever exists.
    //  - standalone with the env var set: the "unset it" hint IS
    //    actionable (the operator controls the env var), so keep it.
    //  - standalone with no path: a blank path disables persistence —
    //    warn that saved tools live in memory only.
    // Other env-locked code-mode sub-fields keep the shared helper.
    let lockedNote = '';
    if (f.field === 'code_mode_saved_tools_path') {
      if (IS_ADDON_MODE) {
        lockedNote =
          '<div class="feature-locked-note">Hardcoded to ' +
          '<code>/data/saved_tools.json</code> in App (add-on) mode and cannot ' +
          'be changed (fixed here so saved tools survive App (add-on) updates).' +
          '</div>';
      } else if (f.origin === 'env') {
        lockedNote =
          `<div class="feature-locked-note">${envLockedNoteHtml(f.env_var, f.field)}</div>`;
      } else if (!f.value) {
        lockedNote =
          '<div class="feature-locked-note">If blank, custom tools are kept ' +
          'in memory only and lost on restart. Set a path on persistent ' +
          'storage to keep them.</div>';
      }
    } else if (f.origin === 'env') {
      lockedNote =
        `<div class="feature-locked-note">${envLockedNoteHtml(f.env_var, f.field)}</div>`;
    }
    info.innerHTML =
      `<div class="feature-name">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(meta.help)}</div>` +
      lockedNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    const disabled = !f.editable || lockedByGate;
    let inputEl;
    if (f.type === 'int' || f.type === 'float') {
      inputEl = document.createElement('input');
      inputEl.type = 'number';
      inputEl.value = f.value;
      if (typeof f.min === 'number') inputEl.min = f.min;
      if (typeof f.max === 'number') inputEl.max = f.max;
      if (f.type === 'float') inputEl.step = '0.1';
    } else {
      inputEl = document.createElement('input');
      inputEl.type = 'text';
      inputEl.value = String(f.value ?? '');
    }
    inputEl.disabled = disabled;
    inputEl.dataset.advField = f.field;
    inputEl.name = 'adv:' + f.field;
    inputEl.addEventListener('change', () => {
      let v;
      if (f.type === 'int') v = parseInt(inputEl.value, 10);
      else if (f.type === 'float') v = parseFloat(inputEl.value);
      else v = inputEl.value;
      commitAdvancedEdit(f.field, v);
    });
    control.appendChild(inputEl);

    row.appendChild(info);
    row.appendChild(control);
    parentEl.appendChild(row);
  });
}

// Returns true only when the server confirmed the save (HTTP ok), false
// on any network/HTTP failure. Callers that need to revert UI state on a
// failed save (the toggle handlers) branch on this; the additive return
// doesn't affect callers that ignore it.
async function saveFeatureFlag(fieldName, value) {
  updateStatus('Saving server setting...');
  let resp;
  try {
    resp = await fetch('./api/settings/features', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({flags: {[fieldName]: value}}),
    });
  } catch (e) {
    updateStatus('Save failed: ' + e.message, false, true);
    return false;
  }
  let data = null;
  try { data = await resp.json(); } catch (_e) {
    // On a 200 OK with truncated / non-JSON body, default to the
    // "restart needed" state so the user gets the banner — silently
    // skipping it would let them think the change took effect live
    // and they'd never restart. Only do this on resp.ok; for an
    // error response we want the HTTP status to drive the message.
    if (resp.ok) data = {restart_required: true};
  }
  if (!resp.ok) {
    let msg = `Save failed (HTTP ${resp.status})`;
    if (data?.error?.message) msg = 'Save failed: ' + data.error.message;
    updateStatus(msg, false, true);
    return false;
  }
  // Unified restart flow — save persists the change but does NOT fire
  // the addon restart. The user picks when to restart by clicking the
  // global Restart Add-on button in the cross-tab restart-required
  // banner. Same UX as the Tools tab. In standalone modes the restart
  // button is hidden (no supervisor to drive it) but the banner still
  // surfaces "restart required" as guidance.
  updateStatus('Saved. Restart required.', true);
  if (data?.restart_required) {
    markRestartRequired();
  }
  return true;
}

// ===== Tool Security Policies tab =====
// Live approval routes (pending/approve/deny) are only available from
// the main server (in-process ApprovalQueue). The sidecar serves
// config GET/PUT but returns 503 for the live endpoints — the UI
// degrades to "Live approvals unavailable in this mode."
//
// The card UI keeps an in-memory mutable copy of each rule
// (policyRuleEdits[tool_name]) so the user can edit conditions /
// remember_minutes locally before pressing "Save changes" on a card,
// which then GETs current policy, replaces the rule entry, and PUTs.
// This mirrors the syncPolicyRule() flow used by the Tools-tab toggle.
let policyRuleEdits = {};

async function syncPolicyMasterToggle() {
  // The master toggle on this tab is just a UI mirror of the same
  // `enable_tool_security_policies` feature flag the Server Settings
  // tab exposes — the addon-config flag is the single source of truth.
  // We rely on loadPolicyState() to have populated policyState.enabled
  // (it fetches /api/settings/features) so the only work here is to
  // reflect that bit into the checkbox.
  await loadPolicyState();
  const cb = document.getElementById('policy-master-toggle');
  if (cb) cb.checked = !!policyState.enabled;
}

async function policyLoadConfig() {
  await syncPolicyMasterToggle();
  const errEl = document.getElementById('policy-load-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  let resp;
  try {
    resp = await fetch('./api/policy/config');
  } catch (e) {
    showPolicyLoadError('Could not reach the server: ' + e.message);
    return;
  }
  if (!resp.ok) {
    // 500 with policy_file_corrupt:true is the explicit "your
    // tool_policy.json is broken, here's how to repair" message from
    // the handler — surface it instead of silently rendering empty.
    let detail = 'HTTP ' + resp.status;
    let bodyParsed = false;
    try {
      const body = await resp.json();
      bodyParsed = true;
      if (body && body.error) detail = body.error;
      if (body && body.policy_file_corrupt) {
        detail += ' (tool_policy.json appears corrupt; edit or delete it on the App (add-on) /data volume)';
      }
    } catch (_e) { /* keep the HTTP-status fallback */ }
    if (!bodyParsed) {
      // E.g. an HTML error page from a misrouted sidecar — give the
      // operator a hint that the body itself was unparseable, not
      // just the status code.
      detail += ' (response body unparseable)';
    }
    showPolicyLoadError('Failed to load policy: ' + detail);
    return;
  }
  const p = await resp.json();
  document.getElementById('policy-wait-seconds').value = p.wait_seconds ?? 60;
  document.getElementById('policy-ttl-minutes').value = p.approval_ttl_minutes ?? 5;
  renderPolicyCards(p);
}

function showPolicyLoadError(msg) {
  const errEl = document.getElementById('policy-load-error');
  if (!errEl) return;
  errEl.style.display = '';
  errEl.textContent = msg;
}

function renderPolicyCards(policy) {
  const listEl = document.getElementById('policy-rules-list');
  const emptyEl = document.getElementById('policy-rules-empty');
  listEl.innerHTML = '';
  policyRuleEdits = {};
  const rules = (policy && policy.rules) || [];
  if (rules.length === 0) {
    emptyEl.style.display = '';
    return;
  }
  emptyEl.style.display = 'none';
  // Group rules by tool_name. The Tools-tab toggle creates exactly one
  // rule per tool; defensively handle the case where a hand-edited file
  // has multiple entries: each becomes its own card so the user can
  // see/edit them all.
  const byTool = {};
  rules.forEach((r, idx) => {
    const key = r.tool_name + ' ' + idx;
    byTool[key] = {tool_name: r.tool_name, rule: r, originalIndex: idx};
  });
  Object.keys(byTool).forEach(key => {
    const entry = byTool[key];
    // Deep clone the rule into the edit buffer so card-local changes
    // don't mutate the server response until "Save changes".
    const editKey = entry.tool_name;
    policyRuleEdits[editKey] = JSON.parse(JSON.stringify(entry.rule));
    listEl.appendChild(renderPolicyCard(entry.tool_name, policyRuleEdits[editKey]));
  });
}

function displayPredicate(p) {
  if (!p || !p.path) return '(invalid)';
  if (p.op === 'exists') return p.path + ' exists';
  const val = (p.value === undefined) ? 'null' : JSON.stringify(p.value);
  return p.path + ' ' + p.op + ' ' + val;
}

function renderPolicyCard(toolName, rule) {
  const card = document.createElement('div');
  card.className = 'policy-rule-card';
  card.dataset.tool = toolName;
  rule.when = rule.when || [];
  const predicateRows = rule.when.map((p, i) => (
    '<li class="policy-predicate-row" data-idx="' + i + '">' +
      '<code>' + escapeHtml(displayPredicate(p)) + '</code>' +
      '<button class="policy-edit-predicate" data-idx="' + i + '">edit</button>' +
      '<button class="policy-remove-predicate" data-idx="' + i + '">×</button>' +
    '</li>'
  )).join('');
  const emptyHint = rule.when.length === 0
    ? '<li class="policy-predicate-row"><em style="color:var(--text-secondary);font-size:0.8rem">' +
      '(no conditions, rule matches every call to this tool)</em></li>'
    : '';
  card.innerHTML =
    '<div class="policy-rule-header">' +
      '<strong>' + escapeHtml(toolName) + '</strong>' +
      '<button class="policy-rule-remove" title="Remove from policy">×</button>' +
    '</div>' +
    '<div class="policy-rule-predicates">' +
      '<label class="features-sub" style="display:block;margin-bottom:4px">' +
        'Require approval when ALL of these conditions match (no conditions = always require approval):' +
      '</label>' +
      '<ul class="policy-predicate-list">' + emptyHint + predicateRows + '</ul>' +
      '<button class="policy-add-predicate">+ Add condition</button>' +
      '<div class="policy-predicate-form" style="display:none;">' +
        '<div class="policy-form-row">' +
          '<label class="policy-form-label">Argument:</label>' +
          '<select name="policy:predicate-path" class="policy-predicate-path-select">' +
            '<option value="">(loading...)</option>' +
          '</select>' +
          '<input type="text" name="policy:predicate-path-custom" class="policy-predicate-path-custom" ' +
            'placeholder="e.g. args.color_temp" style="display:none">' +
        '</div>' +
        '<div class="policy-form-row">' +
          '<label class="policy-form-label">Match when:</label>' +
          '<select name="policy:predicate-op" class="policy-predicate-op">' +
            '<option value="exists">is present (any value)</option>' +
            '<option value="eq">equals</option>' +
            '<option value="neq">does NOT equal</option>' +
            '<option value="in">is one of</option>' +
            '<option value="not_in">is NOT one of</option>' +
            '<option value="contains">contains</option>' +
            '<option value="regex">matches regex</option>' +
            '<option value="gt">is greater than</option>' +
            '<option value="lt">is less than</option>' +
          '</select>' +
        '</div>' +
        '<div class="policy-form-row policy-value-row">' +
          '<label class="policy-form-label">Value:</label>' +
          '<span class="policy-predicate-value-slot"></span>' +
        '</div>' +
        '<div class="policy-form-row">' +
          '<button class="policy-predicate-form-save">Save condition</button>' +
          '<button class="policy-predicate-form-cancel">Cancel</button>' +
        '</div>' +
        '<div class="policy-predicate-form-error" style="display:none;"></div>' +
      '</div>' +
    '</div>' +
    '<div class="policy-rule-lifetime">' +
      '<label>Remember approval for:' +
        '<input type="number" name="policy:remember-minutes" min="0" max="1440" class="policy-remember-minutes" ' +
          'value="' + (rule.remember_minutes || 0) + '">' +
        'minutes (0 = single-shot)' +
      '</label>' +
    '</div>' +
    '<span class="policy-save-status" style="font-size:0.78rem;color:var(--text-secondary)"></span>';

  // Auto-save: every condition add/edit/remove and every remember-minutes
  // change immediately PUTs the rule to disk. No manual "Save changes"
  // button — the only signal is the small status text below the card.
  let autoSaveSeq = 0;
  const autoSave = async () => {
    const status = card.querySelector('.policy-save-status');
    const mySeq = ++autoSaveSeq;
    status.textContent = 'Saving…';
    try {
      await savePolicyRule(toolName, rule);
      // Skip the success label if a newer save started (rapid edits)
      if (mySeq === autoSaveSeq) status.textContent = 'Saved.';
    } catch (err) {
      if (mySeq === autoSaveSeq) {
        status.textContent = 'Save failed: ' + err.message;
      }
    }
  };

  // Re-render the card in place after a condition-list mutation so the
  // rows reflect the new in-memory rule object.
  const rerenderCard = () => {
    const replacement = renderPolicyCard(toolName, rule);
    card.replaceWith(replacement);
  };

  card.querySelector('.policy-rule-remove').addEventListener('click', async () => {
    if (!confirm('Remove "' + toolName + '" from the security policy?')) return;
    try {
      await removePolicyRule(toolName);
      delete policyRuleEdits[toolName];
      card.remove();
      // Refresh card list + empty state from server (also refreshes
      // Tools-tab gated state on next visit via loadPolicyState).
      await policyLoadConfig();
    } catch (err) {
      alert('Failed to remove rule: ' + err.message);
    }
  });

  // remember-minutes is a number input; debounce so typing "30" doesn't
  // fire three saves (3, 30 — or rapid arrow-key presses).
  let rmDebounce = null;
  card.querySelector('.policy-remember-minutes').addEventListener('input', (e) => {
    rule.remember_minutes = parseInt(e.target.value, 10) || 0;
    if (rmDebounce) clearTimeout(rmDebounce);
    rmDebounce = setTimeout(autoSave, 500);
  });

  const formEl = card.querySelector('.policy-predicate-form');
  const opEl = formEl.querySelector('.policy-predicate-op');
  const pathSelectEl = formEl.querySelector('.policy-predicate-path-select');
  const pathCustomEl = formEl.querySelector('.policy-predicate-path-custom');
  const valueSlotEl = formEl.querySelector('.policy-predicate-value-slot');
  const errorEl = formEl.querySelector('.policy-predicate-form-error');
  let editingIdx = -1;
  // Tool schema is fetched lazily on first form-open and cached on
  // the card so reopening the form doesn't refetch.
  let toolSchema = null;
  // value-source choice cache: { source_key: [values] }
  const valueChoiceCache = {};

  const FREE_TEXT_OPT = '__custom__';

  const currentPath = () => (
    pathSelectEl.value === FREE_TEXT_OPT
      ? pathCustomEl.value.trim()
      : pathSelectEl.value
  );

  const populatePathSelect = (selectedPath) => {
    const paths = (toolSchema && toolSchema.paths) || [];
    let html = '';
    // Wildcard: match the condition against EVERY argument of the call.
    // Always first AND default, so the form has a sensible value out of
    // the box and users never hit "argument is required" by saving an
    // empty placeholder.
    html += '<option value="args.*" ' +
      'title="Match against every argument of the call. Combine with op=equals/is one of to gate on any arg having a given value.">' +
      '(any argument)</option>';
    for (const p of paths) {
      const tip = p.description ? ' title="' + escapeHtml(p.description) + '"' : '';
      html += '<option value="' + escapeHtml(p.path) + '"' + tip + '>' +
        escapeHtml(p.label) +
        (p.required ? ' *' : '') +
        (p.type ? ' (' + escapeHtml(p.type) + ')' : '') +
        '</option>';
    }
    html += '<option value="' + FREE_TEXT_OPT + '">(other, type a path)</option>';
    pathSelectEl.innerHTML = html;

    // If the existing condition uses a path the schema doesn't know
    // about (read-only tool, free-text from earlier, removed arg),
    // drop into custom mode automatically so we don't silently clobber
    // the existing value.
    if (selectedPath) {
      const isWildcard = selectedPath === 'args.*';
      const match = paths.find(p => p.path === selectedPath);
      if (isWildcard || match) {
        pathSelectEl.value = selectedPath;
        pathCustomEl.style.display = 'none';
        pathCustomEl.value = '';
      } else {
        pathSelectEl.value = FREE_TEXT_OPT;
        pathCustomEl.style.display = '';
        pathCustomEl.value = selectedPath;
      }
    } else {
      // New condition: default to "(any argument)" so the form is
      // immediately submittable once the user fills in a value.
      pathSelectEl.value = 'args.*';
      pathCustomEl.style.display = 'none';
      pathCustomEl.value = '';
    }
  };

  // Latest value-source fetch error, surfaced as a hint under the value
  // row so the user notices when the dropdown fell back to free-text
  // because of a real failure (vs because no source is registered).
  let lastValueSourceError = null;

  const loadValueChoices = async (sourceKey) => {
    if (valueChoiceCache[sourceKey]) {
      lastValueSourceError = null;
      return valueChoiceCache[sourceKey];
    }
    try {
      const r = await fetch('./api/policy/value-source?source=' +
        encodeURIComponent(sourceKey));
      if (!r.ok) {
        lastValueSourceError = 'value-source fetch failed (HTTP ' + r.status + '); falling back to free-text';
        return null;
      }
      const data = await r.json();
      const values = Array.isArray(data.values) ? data.values : [];
      valueChoiceCache[sourceKey] = values;
      lastValueSourceError = null;
      return values;
    } catch (e) {
      lastValueSourceError = 'value-source fetch failed (' + e.message + '); falling back to free-text';
      return null;
    }
  };

  // Ops where leaving the value blank is meaningful UX shorthand for
  // "gate any call where this argument is present, regardless of
  // value". On save, those blank-value entries are coerced to
  // op=exists (see readValueControl + the form-save handler). Ops
  // that genuinely require a value (regex / gt / lt) stay strict.
  const VALUE_OPTIONAL_OPS = new Set(['exists', 'eq', 'neq', 'in', 'not_in', 'contains']);

  const hintForOp = (op) => {
    if (op === 'exists') {
      return 'Leave blank. This op gates on the argument being present at all, regardless of value.';
    }
    if (op === 'in' || op === 'not_in') {
      return 'Pick one or more values, or type a JSON list. Leave blank to gate on any value.';
    }
    if (op === 'regex') {
      return 'A regular expression to match the argument against.';
    }
    if (op === 'contains') {
      return 'A substring (for strings) or item (for lists). Leave blank to gate on any value.';
    }
    if (op === 'gt' || op === 'lt') {
      return 'A number to compare against.';
    }
    return 'The value the argument must equal. Leave blank to gate on any value.';
  };

  // Sequence number for renderValueControl — rapid path/op edits can
  // start several overlapping fetches; only the latest one is allowed
  // to mutate the DOM. Without this, an earlier slow fetch can land
  // after a later fast one and clobber the user's chosen control.
  let renderSeq = 0;

  // Render the value control inside valueSlotEl based on current op +
  // path. The control is always visible (even for op=exists) so users
  // can refine the rule later without re-discovering where the input
  // went.
  const renderValueControl = async (existingValue) => {
    const mySeq = ++renderSeq;
    const op = opEl.value;
    const path = currentPath();
    const pathMeta = ((toolSchema && toolSchema.paths) || [])
      .find(p => p.path === path);
    const sourceKey = (toolSchema && toolSchema.value_sources)
      ? toolSchema.value_sources[path]
      : null;
    const isMulti = (op === 'in' || op === 'not_in');
    const isSingleChoice = (op === 'eq' || op === 'neq');
    const choosable = isMulti || isSingleChoice;

    // 1) Live value source (e.g. ha_entities) wins — most useful.
    if (sourceKey && choosable) {
      if (mySeq !== renderSeq) return;
      valueSlotEl.innerHTML = '<em style="color:var(--text-secondary);font-size:0.78rem">' +
        'Loading choices…</em>';
      const choices = await loadValueChoices(sourceKey);
      if (mySeq !== renderSeq) return;  // newer render in flight; discard.
      if (choices) {
        renderChoiceSelect(choices, existingValue, isMulti);
        renderHint(op);
        return;
      }
      // fetch failed → fall through to free-text (renderHint will
      // surface the error via lastValueSourceError below).
    }

    // 2) Schema-declared enum — render as choice list too.
    if (choosable && pathMeta && Array.isArray(pathMeta.enum) && pathMeta.enum.length) {
      if (mySeq !== renderSeq) return;
      renderChoiceSelect(pathMeta.enum, existingValue, isMulti);
      renderHint(op);
      return;
    }

    // 3) Free-text JSON fallback (or op=exists, where blank is the norm).
    if (mySeq !== renderSeq) return;
    renderFreeTextValue(existingValue);
    renderHint(op);
  };

  const renderChoiceSelect = (choices, existingValue, isMulti) => {
    const existingArr = Array.isArray(existingValue)
      ? existingValue
      : (existingValue !== undefined && existingValue !== null ? [existingValue] : []);
    let html = '<select name="policy:predicate-value" class="policy-predicate-value-control"' +
      (isMulti ? ' multiple size="6" style="min-width:220px"' : '') +
      '>';
    if (!isMulti) {
      html += '<option value="">(pick a value)</option>';
    }
    for (const c of choices) {
      const selected = existingArr.includes(c) ? ' selected' : '';
      html += '<option value="' + escapeHtml(String(c)) + '"' + selected + '>' +
        escapeHtml(String(c)) + '</option>';
    }
    html += '</select>';
    valueSlotEl.innerHTML = html;
  };

  const renderFreeTextValue = (existingValue) => {
    const op = opEl.value;
    let placeholder;
    if (op === 'exists') {
      placeholder = 'usually left blank';
    } else if (op === 'in' || op === 'not_in') {
      placeholder = '["lock","alarm_control_panel"]';
    } else if (op === 'regex') {
      placeholder = '^light\..+';
    } else {
      placeholder = '"lock"  or  42  or  true';
    }
    const initial = (existingValue === undefined || existingValue === null)
      ? ''
      : JSON.stringify(existingValue);
    valueSlotEl.innerHTML = '<input type="text" name="policy:predicate-value" ' +
      'class="policy-predicate-value-control policy-predicate-value" ' +
      'placeholder="' + escapeHtml(placeholder) + '" ' +
      'value="' + escapeHtml(initial) + '">';
  };

  const renderHint = (op) => {
    // Remove any previous hint then add a fresh one below the value row.
    const oldHint = formEl.querySelector('.policy-form-hint');
    if (oldHint) oldHint.remove();
    const hint = document.createElement('div');
    hint.className = 'policy-form-hint';
    let text = hintForOp(op);
    // If a value-source fetch failed (HA outage, sidecar 503, …) the
    // dropdown silently downgraded to free-text — surface that so the
    // user knows the typo'd rule they're about to author isn't picking
    // from a populated list.
    if (lastValueSourceError) {
      text = lastValueSourceError + '. ' + text;
      hint.style.color = 'var(--danger)';
    }
    hint.textContent = text;
    formEl.querySelector('.policy-value-row').after(hint);
  };

  const readValueControl = () => {
    const op = opEl.value;
    const ctrl = valueSlotEl.querySelector('.policy-predicate-value-control');
    if (!ctrl) return {ok: true, value: undefined};
    if (ctrl.tagName === 'SELECT') {
      if (ctrl.multiple) {
        const picked = Array.from(ctrl.selectedOptions).map(o => o.value);
        if (picked.length === 0) {
          if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
          return {ok: false, error: 'pick at least one value'};
        }
        return {ok: true, value: picked};
      }
      if (!ctrl.value) {
        if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
        return {ok: false, error: 'pick a value'};
      }
      return {ok: true, value: ctrl.value};
    }
    const raw = ctrl.value.trim();
    if (!raw) {
      if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
      return {ok: false, error: 'value is required for op=' + op};
    }
    // First try raw JSON. If that fails, fall back to smart-coercion
    // so users can type "lock" or "lock,alarm" without remembering the
    // quoting rules.
    try {
      return {ok: true, value: JSON.parse(raw)};
    } catch (_e) {
      const coerced = coerceBarewords(raw, op);
      if (coerced.ok) return coerced;
      return {ok: false, error: coerced.error};
    }
  };

  // Coerce common bareword inputs into the JSON the backend expects.
  // "lock"               (op=eq)        → "lock"
  // "lock"               (op=in)        → ["lock"]
  // "lock,alarm_control" (op=in/not_in) → ["lock","alarm_control"]
  // "42"                 → 42  (numeric autodetect for any op)
  // "true" / "false"     → boolean
  const coerceBarewords = (raw, op) => {
    const wrap = (v) => (op === 'in' || op === 'not_in') ? [v] : v;
    if (op === 'in' || op === 'not_in') {
      // Try comma-split first — if any chunk is comma-separated, build list
      if (raw.indexOf(',') !== -1) {
        const items = raw.split(',').map(s => s.trim()).filter(Boolean);
        if (items.length === 0) {
          return {ok: false, error: 'empty list for op=' + op};
        }
        return {ok: true, value: items.map(coerceScalar)};
      }
    }
    const scalar = coerceScalar(raw);
    return {ok: true, value: wrap(scalar)};
  };

  const coerceScalar = (s) => {
    if (s === 'true') return true;
    if (s === 'false') return false;
    if (s === 'null') return null;
    if (/^-?\d+$/.test(s)) return parseInt(s, 10);
    if (/^-?\d+\.\d+$/.test(s)) return parseFloat(s);
    return s; // plain string
  };

  const fetchToolSchema = async () => {
    if (toolSchema !== null) return toolSchema;
    try {
      const r = await fetch('./api/policy/tool-schema?name=' +
        encodeURIComponent(toolName));
      if (r.ok) {
        toolSchema = await r.json();
      } else {
        // 503/404/etc: server can't introspect (sidecar / tool not
        // found). Use an empty schema so the UI still works via free
        // text. Surface the failure through lastValueSourceError so
        // renderHint shows the user why their dropdown is gone.
        toolSchema = {paths: [], value_sources: {}};
        lastValueSourceError = 'tool-schema fetch failed (HTTP ' + r.status +
          '); falling back to free-text';
      }
    } catch (e) {
      toolSchema = {paths: [], value_sources: {}};
      lastValueSourceError = 'tool-schema fetch failed (' + e.message +
        '); falling back to free-text';
    }
    return toolSchema;
  };

  opEl.addEventListener('change', () => renderValueControl(undefined));
  pathSelectEl.addEventListener('change', () => {
    pathCustomEl.style.display = (pathSelectEl.value === FREE_TEXT_OPT) ? '' : 'none';
    renderValueControl(undefined);
  });
  pathCustomEl.addEventListener('input', () => renderValueControl(undefined));

  const openForm = async (idx) => {
    editingIdx = idx;
    errorEl.style.display = 'none';
    errorEl.textContent = '';
    formEl.style.display = '';
    await fetchToolSchema();
    if (idx >= 0) {
      const p = rule.when[idx];
      opEl.value = p.op || 'eq';
      populatePathSelect(p.path || '');
      await renderValueControl(p.value);
    } else {
      opEl.value = 'eq';
      populatePathSelect('');
      await renderValueControl(undefined);
    }
  };

  card.querySelector('.policy-add-predicate').addEventListener('click', () => openForm(-1));

  card.querySelectorAll('.policy-edit-predicate').forEach(btn => {
    btn.addEventListener('click', () => openForm(parseInt(btn.dataset.idx, 10)));
  });

  card.querySelectorAll('.policy-remove-predicate').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx, 10);
      rule.when.splice(idx, 1);
      await autoSave();
      rerenderCard();
    });
  });

  formEl.querySelector('.policy-predicate-form-cancel').addEventListener('click', () => {
    formEl.style.display = 'none';
    editingIdx = -1;
  });

  formEl.querySelector('.policy-predicate-form-save').addEventListener('click', async () => {
    let op = opEl.value;
    const path = currentPath();
    if (!path) {
      errorEl.textContent = 'argument is required';
      errorEl.style.display = '';
      return;
    }
    const predicate = {path: path, op: op};
    // op=exists is presence-only — backend rejects any value field,
    // so ignore whatever's in the value box even if the user typed
    // something. Other ops read normally.
    if (op !== 'exists') {
      const parsed = readValueControl();
      if (!parsed.ok) {
        errorEl.textContent = parsed.error;
        errorEl.style.display = '';
        return;
      }
      if (parsed.value === undefined) {
        // User left value blank on an op where "any value matches"
        // is meaningful UX shorthand (eq/neq/in/not_in/contains).
        // Silently coerce to op=exists so the row reads as
        // "args.* exists" and the rule actually gates on presence
        // rather than storing a useless null-match. predicate is what gets
        // persisted, so set it directly (the local `op` is not read again).
        predicate.op = 'exists';
      } else {
        predicate.value = parsed.value;
      }
    }
    if (editingIdx >= 0) {
      rule.when[editingIdx] = predicate;
    } else {
      rule.when.push(predicate);
    }
    await autoSave();
    rerenderCard();
  });

  return card;
}

async function savePolicyRule(toolName, ruleObj) {
  const r = await fetch('./api/policy/config');
  if (!r.ok) throw new Error('Could not load policy: ' + r.status);
  const policy = await r.json();
  policy.rules = policy.rules || [];
  const idx = policy.rules.findIndex(rule => rule.tool_name === toolName);
  if (idx >= 0) {
    policy.rules[idx] = ruleObj;
  } else {
    // Defensive: a card exists for a tool with no server-side rule
    // (e.g. the user removed the rule from another tab between load
    // and save). Append rather than silently drop the edit.
    policy.rules.push(ruleObj);
  }
  await policyPut(policy, 'Save rule');
}

async function removePolicyRule(toolName) {
  // Mirror syncPolicyRule(toolName, false) — kept as a separate helper
  // so the card's remove button stays self-contained, but the on-wire
  // shape is identical.
  await syncPolicyRule(toolName, false);
}

async function saveGlobalSettings() {
  const statusEl = document.getElementById('policy-global-save-status');
  setStatusAlert(statusEl, false);
  statusEl.textContent = 'Saving...';
  let resp;
  try {
    resp = await fetch('./api/policy/config');
  } catch (e) {
    setStatusAlert(statusEl, true);
    statusEl.textContent = 'Network error: ' + e.message;
    showToast('Network error: ' + e.message, {isError: true});
    return;
  }
  if (!resp.ok) {
    setStatusAlert(statusEl, true);
    statusEl.textContent = 'Load failed: ' + resp.status;
    showToast('Load failed: ' + resp.status, {isError: true});
    return;
  }
  const policy = await resp.json();
  policy.wait_seconds = parseInt(document.getElementById('policy-wait-seconds').value, 10);
  policy.approval_ttl_minutes = parseInt(document.getElementById('policy-ttl-minutes').value, 10);
  try {
    await policyPut(policy, 'Save global settings');
    statusEl.textContent = 'Saved.';
    showToast('Saved.');
  } catch (e) {
    setStatusAlert(statusEl, true);
    statusEl.textContent = e.message;
    showToast(e.message, {isError: true});
  }
}

async function policyLoadPending() {
  const list = document.getElementById('policy-pending-list');
  let resp;
  try {
    resp = await fetch('./api/policy/pending');
  } catch (e) {
    // Surface the failure inline — silent return leaves the pending
    // list visibly frozen with no signal that polling broke.
    list.innerHTML = '<em style="color:var(--text-secondary)">Lost contact with server (' + escapeHtml(e.message) + '). Retrying.</em>';
    return;
  }
  if (resp.status === 503) {
    // 503 has three causes. Only confidently say "feature is off"
    // when /api/settings/features actually told us so; if we couldn't
    // determine the flag (network drop, server down), fall back to
    // the server's 503 message rather than misleadingly claiming the
    // user disabled the feature.
    if (policyState.enabledKnown && !policyState.enabled) {
      list.innerHTML = '<em>Tool Security Policies is turned off. Toggle it on (top of this tab or in Server Settings) and restart the App (add-on) to enable gating.</em>';
    } else {
      // Feature is on (or unknown) but the queue isn't reachable —
      // sidecar mode, startup ImportError, or transient outage.
      let msg = 'Live approvals unavailable. Check the App (add-on) log for ImportError / RuntimeError details.';
      try {
        const body = await resp.json();
        if (body && body.error) msg = body.error;
      } catch (_e) { /* keep default */ }
      list.innerHTML = '<em>' + escapeHtml(msg) + '</em>';
    }
    return;
  }
  if (!resp.ok) return;
  const data = await resp.json();
  const pending = data.pending || [];
  if (pending.length === 0) {
    list.textContent = 'No pending approvals.';
    return;
  }
  list.innerHTML = pending.map(p => (
    '<div data-pending-token="' + escapeHtml(p.token) + '" style="border:1px solid var(--border); padding:10px; margin:6px 0; border-radius:8px; background:var(--surface)">' +
    '<strong>' + escapeHtml(p.tool_name) + '</strong>' +
    '<pre style="white-space:pre-wrap; background:var(--bg); padding:8px; margin:6px 0; border-radius:6px; font-size:0.8rem">' +
    escapeHtml(JSON.stringify(p.args, null, 2)) + '</pre>' +
    '<small style="color:var(--text-secondary)">Expires: ' + escapeHtml(p.expires_at) + '</small><br>' +
    '<div style="margin-top:8px; display:flex; gap:8px">' +
    '<button class="restart-btn" data-policy-token="' + escapeHtml(p.token) + '" data-policy-action="approve">Approve</button>' +
    '<button class="danger-btn" data-policy-token="' + escapeHtml(p.token) + '" data-policy-action="deny">Deny</button>' +
    '</div></div>'
  )).join('');
  // Re-bind decision buttons each render (no event delegation needed —
  // pending list is small and re-rendered on every poll).
  list.querySelectorAll('button[data-policy-token]').forEach(btn => {
    btn.addEventListener('click', () =>
      policyDecide(btn.dataset.policyToken, btn.dataset.policyAction)
    );
  });
}

async function policyDecide(token, action) {
  let resp;
  try {
    resp = await fetch('./api/policy/' + action, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: token}),
    });
  } catch (e) {
    alert('Network error: ' + e.message);
    return;
  }
  if (!resp.ok) {
    let body;
    try { body = await resp.json(); } catch (_) { body = {error: 'HTTP ' + resp.status}; }
    if (resp.status === 409 && body.current_decision) {
      alert("This approval was already " + body.current_decision +
            ", possibly by another tab or session.");
    } else if (resp.status === 404) {
      alert("This approval token is no longer valid (already consumed or expired).");
    } else {
      alert('Approval action failed: ' + (body.error || resp.statusText));
    }
  }
  policyLoadPending();
}

document.getElementById('policy-save-global-btn').addEventListener('click', saveGlobalSettings);

// Master toggle on this tab mirrors the Server Settings checkbox.
// Persist via the same /api/settings/features endpoint so a save here
// shows up in Server Settings (and the addon's config.yaml) on reload.
document.getElementById('policy-master-toggle').addEventListener('change', async (e) => {
  const previous = !e.target.checked;  // user just flipped; previous is the OPPOSITE.
  const ok = await saveFeatureFlag('enable_tool_security_policies', e.target.checked);
  if (!ok) {
    // Save definitely failed — the server still has the old value.
    // Revert the checkbox and surface the failure (set the status AFTER
    // the revert so it isn't clobbered).
    e.target.checked = previous;
    updateStatus('Tool Security Policies change did not save. The server still has the previous value', false, true);
    return;
  }
  // Re-read the truth from the server and sync the checkbox back to
  // it. If the follow-up read can't confirm what the server has, revert
  // to the pre-flip value so the UI doesn't lie about persisted state.
  await loadPolicyState();
  if (policyState.enabledKnown) {
    e.target.checked = !!policyState.enabled;
  } else {
    e.target.checked = previous;
  }
});

// Read Only Mode toggle (Tools tab, above the search box) — same flag
// plumbing as the policy master toggle: persist via
// /api/settings/features, re-read server truth, revert on failure.
document.getElementById('read-only-mode-toggle').addEventListener('change', async (e) => {
  const previous = !e.target.checked;  // user just flipped; previous is the OPPOSITE.
  const ok = await saveFeatureFlag('read_only_mode', e.target.checked);
  if (!ok) {
    // Save definitely failed — the server still has the previous value.
    // Revert the checkbox and leave readOnlyState.enabled untouched (do
    // NOT write an unconfirmed value). Set the status AFTER the revert
    // so the revert's render/sync can't clobber the message.
    e.target.checked = previous;
    render();
    updateStatus('Read Only Mode change did not save. The server still has the previous value', false, true);
    return;
  }
  // Re-read the truth from the server and sync the checkbox back to it.
  await loadPolicyState();
  if (readOnlyState.enabledKnown) {
    e.target.checked = !!readOnlyState.enabled;
  } else {
    // Save reported OK but the follow-up read couldn't confirm — revert
    // to the pre-flip value rather than assert an unconfirmed state.
    e.target.checked = previous;
  }
  // Re-render so write-tool rows reflect the forced-off state instantly.
  render();
});

// Poll for pending approvals every 3s when Tool Security Policies tab is visible.
setInterval(() => {
  const policiesTab = document.querySelector('.tab[data-panel="tool-security-policies"]');
  if (policiesTab && policiesTab.classList.contains('active')) {
    policyLoadPending();
  }
}, 3000);

// ===== Tab switching =====
// Generic dispatcher — every .tab button names its target panel via
// data-panel, every .panel has matching id="panel-<name>". Adding a
// new tab is one button + one panel div; no JS change needed.
function activateTab(target, opts) {
  const focusTab = opts && opts.focusTab;
  document.querySelectorAll('.tab').forEach(t => {
    const selected = t.dataset.panel === target;
    t.classList.toggle('active', selected);
    // Expose tab state + roving tabindex to assistive tech (WAI-ARIA APG
    // tabs pattern). Only the selected tab stays in the Tab sequence;
    // arrow keys move between the rest. (#1596)
    t.setAttribute('aria-selected', selected ? 'true' : 'false');
    t.tabIndex = selected ? 0 : -1;
    if (selected && focusTab) t.focus();
  });
  document.querySelectorAll('.panel').forEach(p =>
    p.classList.toggle('active', p.id === 'panel-' + target)
  );
  if (target === 'backups') { loadBackupConfig(); loadBackups(); }
  if (target === 'tool-security-policies') { policyLoadConfig(); policyLoadPending(); }
  if (target === 'entity-visibility') { visibilityLoadConfig(); }
  if (target === 'tools') {
    // Refresh gated-toggle + read-only state in case the user changed
    // them from another tab while it was active.
    loadPolicyState().then(() => { syncReadOnlyToggle(); render(); }).catch(() => {});
  }
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => activateTab(tab.dataset.panel));
});

// Keyboard navigation for the tablist (WAI-ARIA APG tabs pattern): Left/Right
// move + activate the adjacent tab, Home/End jump to the ends. (#1596)
{
  const tablist = document.querySelector('.tabs[role="tablist"]');
  if (tablist) {
    tablist.addEventListener('keydown', (e) => {
      const tabs = Array.from(tablist.querySelectorAll('.tab'));
      const currentIndex = tabs.indexOf(document.activeElement);
      if (currentIndex === -1) return;
      let nextIndex = null;
      if (e.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
      else if (e.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      else if (e.key === 'Home') nextIndex = 0;
      else if (e.key === 'End') nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      e.preventDefault();
      activateTab(tabs[nextIndex].dataset.panel, { focusTab: true });
    });
  }
}

// Cross-tab links — any <a data-panel-link="<name>"> switches tabs
// in-page rather than following the href (used by the "no gated
// tools" empty state to point users at the Tools tab).
document.addEventListener('click', (e) => {
  const link = e.target.closest('[data-panel-link]');
  if (!link) return;
  e.preventDefault();
  activateTab(link.dataset.panelLink);
});

// ===== Advanced settings =====
const ADVANCED_FIELD_META = {
  homeassistant_url:   { label: "Home Assistant URL",          help: "Display only. Set via HOMEASSISTANT_URL env var or App (add-on)-managed (Supervisor)." },
  homeassistant_token: { label: "Home Assistant token",        help: "Display only. Set via HOMEASSISTANT_TOKEN env var. Masked here for security." },
  timeout:             { label: "HA request timeout (s)",      help: "Per-request HTTP timeout. Range 1–600. Restart required." },
  max_retries:         { label: "HA request max retries",      help: "Retry budget per failed REST call. Range 0–20. Restart required." },
  verify_ssl:          { label: "Verify SSL certificates",     help: "Skip TLS verification only on trusted networks (self-signed certs, hostname mismatch). Restart required." },
  fuzzy_threshold:     { label: "Fuzzy-search threshold",      help: "Lower = looser entity match. Range 0–100." },
  entity_search_limit: { label: "Entity search result limit",  help: "Max entities returned by ha_search_entities. Range 1–1000." },
  automation_config_time_budget: { label: "Automation config time budget (s)", help: "Max seconds ha_search/ha_deep_search spends fetching automation configs before returning a partial result. Raise on instances with many automations. Range 1–600. Restart required." },
  script_config_time_budget:     { label: "Script config time budget (s)",     help: "Max seconds ha_search/ha_deep_search spends fetching script configs before returning a partial result. Range 1–600. Restart required." },
  scene_config_time_budget:      { label: "Scene config time budget (s)",      help: "Max seconds ha_search/ha_deep_search spends fetching scene configs before returning a partial result. Range 1–600. Restart required." },
  individual_config_timeout:     { label: "Per-request config fetch timeout (s)", help: "Timeout for each individual automation/script/scene config fetch during deep search. On HA servers that serve config reads serially, raise this and/or lower the batch size so queued requests don't time out. Values above the HA request timeout (HA_TIMEOUT, default 30) have no extra effect — the HTTP client gives up first. Range 1–600. Restart required." },
  individual_fetch_batch_size:   { label: "Config fetch batch size",          help: "How many per-id config fetches deep search issues concurrently. Lower toward 1 on HA servers that serve config reads serially (symptom: 'timed out' partial-result warnings). Range 1–100. Restart required." },
  backup_hint:         { label: "Backup-hint level",           help: "Tunes how strongly the LLM is prompted to take a full-HA snapshot before risky writes." },
  dashboard_screenshot_engine_url: { label: "Dashboard screenshot engine URL", help: "Base URL of the screenshot engine (e.g. http://puppet:10000). Leave blank to auto-discover the Puppet App (add-on) via the Supervisor (HA OS / Supervised). Only used when the Dashboard Screenshot beta feature is enabled. Takes effect without a restart." },
  enable_websocket:    { label: "Enable WebSocket",            help: "WebSocket-based state monitoring. Disabling falls back to polling; many tools degrade. Restart required." },
  enabled_tool_modules: { label: "Enabled tool modules",       help: "Comma-separated module names, or 'all'. Restricts which tool registry modules load at startup. Restart required." },
  enable_dashboard_partial_tools: { label: "Dashboard partial-update tools", help: "Token-efficient partial dashboard tools. Disable for clients with programmatic tool use." },
  mcp_server_name:     { label: "MCP server name",             help: "Reported in MCP handshake. Restart required." },
  mcp_server_version:  { label: "MCP server version",          help: "Defaults to the package version. Overriding can confuse clients that key on this string. Restart required." },
  environment:         { label: "Environment",                 help: "'development' or 'production'. Affects logging verbosity. Restart required." },
  log_level:           { label: "Log level",                   help: "DEBUG/INFO/WARNING/ERROR/CRITICAL. Set once at startup; restart required." },
  debug:               { label: "Debug mode",                  help: "Verbose request logging. Logs sensitive data; do not enable in production. Restart required." },
  code_mode_max_duration:    { label: "Code-mode max duration (s)",   help: "Wall-clock budget per sandbox run. Range 1–300. Restart required." },
  code_mode_max_memory:      { label: "Code-mode max memory (bytes)", help: "RSS cap per sandbox run. Range 1 MB–256 MB. Restart required." },
  code_mode_max_recursion:   { label: "Code-mode max recursion",      help: "Recursion-depth cap per sandbox run. Restart required." },
  code_mode_max_invocations: { label: "Code-mode max invocations",    help: "API/tool-call cap per sandbox run. Restart required." },
  code_mode_saved_tools_path:{ label: "Saved-tools path",              help: "JSON file where ha_manage_custom_tool persists saved tools across restarts. Restart required." },
  sidecar_pin_port:    { label: "Settings UI sidecar port",    help: "0 = a new free port each restart (default); set 1024–65535 to pin a fixed port so the settings URL stays stable across restarts. Falls back to a free port if the pinned one is busy. Restart required." },
  enable_dev_mode:     { label: "Developer mode",               help: "⚠ DANGER: registers hidden developer tools (ha_dev_manage_server, ha_dev_manage_settings) that let AI agents change server settings and replace the running server version (e.g. install a PR build). For development and testing only. Restart required." },
};

// Fields that require an MCP-host restart to take effect when changed
// from this surface. Used to surface the restart-required banner on save.
// REST client construction (timeout / verify_ssl / max_retries) is cached
// once at startup so those need restart even though the underlying call
// is per-request.
const ADVANCED_RESTART_REQUIRED = new Set([
  "timeout", "max_retries", "verify_ssl",
  "enabled_tool_modules", "enable_websocket",
  "log_level", "debug",
  "mcp_server_name", "mcp_server_version", "environment",
  // fuzzy_threshold is read once by SmartSearchTools at the
  // lazy-init singleton (tools/smart_search.py) — changes
  // need restart to rebuild the searcher.
  "fuzzy_threshold",
  // The three smart-search time budgets are read once at import by
  // SmartSearchTools' _config module (singleton), so a change needs a
  // restart. dashboard_screenshot_engine_url is intentionally absent —
  // it is resolved live per capture, so it takes effect immediately.
  "automation_config_time_budget", "script_config_time_budget",
  "scene_config_time_budget",
  // The Attempt-C per-request timeout and batch size (#1784) share the
  // budgets' import-time consumption, so they need a restart too.
  "individual_config_timeout", "individual_fetch_batch_size",
  "code_mode_max_duration", "code_mode_max_memory",
  "code_mode_max_recursion", "code_mode_max_invocations",
  "code_mode_saved_tools_path",
  // The sidecar binds its port once at spawn (run_main), so changing the
  // pin needs a restart to respawn the sidecar on the new port.
  "sidecar_pin_port",
  // Dev-mode tools register at startup; toggling needs a restart to
  // (un)register them.
  "enable_dev_mode",
]);

let _advancedFields = [];
let _advancedDirty = {};  // {field: newValue} for unsaved edits

async function loadAdvancedSettings() {
  // Mirrors loadFeatureFlags' 3-arm error handling: surface network /
  // HTTP / parse failures in the first section container so the user
  // (and field debuggers reading the page) can see what went wrong.
  // Console-log too so devtools has a stack.
  // Connection section was removed; fall back to
  // advSearch — the first remaining section — for error display.
  const errSlot = document.getElementById('advSearch');
  let resp;
  try {
    resp = await fetch('./api/settings/advanced');
  } catch (err) {
    console.error('loadAdvancedSettings fetch failed:', err);
    if (errSlot) errSlot.innerHTML =
      '<div class="adv-row"><div class="adv-help">' +
      'Advanced settings unavailable (network error reaching ' +
      '/api/settings/advanced).</div></div>';
    return;
  }
  if (!resp.ok) {
    if (errSlot) errSlot.innerHTML =
      `<div class="adv-row"><div class="adv-help">` +
      `Advanced settings unavailable (HTTP ${resp.status}).</div></div>`;
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (err) {
    console.error('loadAdvancedSettings JSON parse failed:', err);
    if (errSlot) errSlot.innerHTML =
      '<div class="adv-row"><div class="adv-help">' +
      'Advanced settings response was not valid JSON.</div></div>';
    return;
  }
  _advancedFields = data.fields || [];
  if (typeof data.is_addon === 'boolean') {
    IS_ADDON_MODE = data.is_addon;
  }
  // Do NOT clear _advancedDirty here. This runs on the post-save reload (and
  // the feature-flag re-render below); clearing it would wipe edits the user
  // made to OTHER fields while the save was in flight and reset their inputs
  // to the server value — silent data loss. Pending edits are re-stamped onto
  // the freshly-rendered inputs at the end of this function.
  const bySection = {};
  _advancedFields.forEach(f => {
    (bySection[f.section] ||= []).push(f);
  });
  // Render each section into its dedicated container. Sections from
  // ADVANCED_SETTINGS_FIELDS that are NOT in the Server Settings tab
  // (e.g. "beta_codemode" is rendered under the Beta master toggle by
  // Chunk 3b, not here, and "connection" was removed from the panel
  // per user feedback) are skipped at this surface — they have no
  // container in panel-server. renderAdvancedSection is a no-op when
  // its target container is missing.
  renderAdvancedSection('advSearch', bySection.search || []);
  renderAdvancedSection('advOperations', bySection.operations || []);
  renderAdvancedSection('advToolsSurface', bySection.tools_surface || []);
  renderAdvancedSection('advDiagnostics', bySection.diagnostics || []);
  renderAdvancedSection('advSidecar', bySection.sidecar || []);
  renderAdvancedSection('advDeveloper', bySection.developer || []);
  applySidecarAvailability(data.is_stdio !== false);
  // Re-render feature flags so the code_mode sub-numerics show up
  // beneath enable_code_mode (race: loadFeatureFlags may have run
  // before _advancedFields was populated). Cheap no-op if feature
  // flags haven't loaded yet.
  if (Object.keys(_lastFeatureFlags).length > 0) {
    renderFeatureFlags(_lastFeatureFlags);
  }
  // Re-apply still-pending edits on top of the freshly-rendered (server-valued)
  // inputs so an edit made during an in-flight save isn't visually reverted.
  Object.entries(_advancedDirty).forEach(([fname, val]) => {
    const input = document.querySelector('[data-adv-field="' + fname + '"]');
    if (!input) return;
    if (input.type === 'checkbox') input.checked = !!val;
    else input.value = val;
  });
}

function applySidecarAvailability(isStdio) {
  // The sidecar-port setting only applies when this settings page is served
  // by the stdio settings-UI sidecar. In HTTP/SSE/OAuth/addon deployments
  // there is no sidecar, so dim + disable the section and explain why, rather
  // than letting a user save a value that does nothing.
  // Remove any note from a prior load first, so the <h2> title is once again
  // the section's previousElementSibling (an injected note would otherwise
  // shadow it on a re-render).
  const prevNote = document.getElementById('advSidecarNote');
  if (prevNote) prevNote.remove();
  const section = document.getElementById('advSidecar');
  if (!section) return;
  const title = section.previousElementSibling;
  if (isStdio) {
    section.classList.remove('dimmed');
    if (title) title.classList.remove('dimmed');
    return;
  }
  section.classList.add('dimmed');
  if (title) title.classList.add('dimmed');
  section.querySelectorAll('input, select').forEach((el) => { el.disabled = true; });
  const note = document.createElement('div');
  note.id = 'advSidecarNote';
  note.className = 'adv-section-note';
  note.textContent =
    'Available in stdio mode only. This server runs over HTTP, which has '
    + 'no settings-UI sidecar to pin.';
  section.parentNode.insertBefore(note, section);
}

function renderAdvancedSection(containerId, fields) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '';
  fields.forEach(f => {
    const row = document.createElement('div');
    row.className = 'adv-row' + (f.editable ? '' : ' locked');
    const meta = ADVANCED_FIELD_META[f.field] || { label: f.field, help: '' };
    let controlHtml;
    if (f.choices) {
      controlHtml = `<select name="adv:${escapeHtml(f.field)}" data-adv-field="${escapeHtml(f.field)}" aria-labelledby="label-adv-${escapeHtml(f.field)}" ${f.editable ? '' : 'disabled'}>` +
        f.choices.map(c =>
          `<option value="${escapeHtml(c)}" ${String(f.value) === c ? 'selected' : ''}>${escapeHtml(c)}</option>`
        ).join('') +
        '</select>';
    } else if (f.type === 'bool') {
      controlHtml = `<input type="checkbox" name="adv:${escapeHtml(f.field)}" data-adv-field="${escapeHtml(f.field)}" aria-labelledby="label-adv-${escapeHtml(f.field)}" ${f.value ? 'checked' : ''} ${f.editable ? '' : 'disabled'}>`;
    } else if (f.type === 'int' || f.type === 'float') {
      controlHtml = `<input type="number" name="adv:${escapeHtml(f.field)}" data-adv-field="${escapeHtml(f.field)}" aria-labelledby="label-adv-${escapeHtml(f.field)}" value="${Number(f.value)}" ` +
        (f.min !== undefined ? `min="${f.min}" ` : '') +
        (f.max !== undefined ? `max="${f.max}" ` : '') +
        (f.type === 'float' ? 'step="0.1" ' : '') +
        (f.editable ? '' : 'disabled') + '>';
    } else {
      // str
      controlHtml = `<input type="text" name="adv:${escapeHtml(f.field)}" data-adv-field="${escapeHtml(f.field)}" aria-labelledby="label-adv-${escapeHtml(f.field)}" value="${escapeHtml(String(f.value ?? ''))}" ${f.editable ? '' : 'disabled'}>`;
    }
    let originMsg = '';
    if (f.origin === 'env') {
      originMsg = envLockedNoteHtml(f.env_var, f.field);
    } else if (!f.editable) {
      originMsg = 'Display only. Modify via env var or App (add-on) settings.';
    }
    row.innerHTML =
      `<div class="adv-info">` +
        `<div class="adv-name" id="label-adv-${escapeHtml(f.field)}">${escapeHtml(meta.label)}</div>` +
        `<div class="adv-help">${escapeHtml(meta.help)}</div>` +
        (originMsg ? `<div class="adv-locked-note">${originMsg}</div>` : '') +
      `</div>` +
      `<div class="adv-control">${controlHtml}</div>`;
    el.appendChild(row);
  });
  // Auto-save on change: toggles fire immediately, number/text/select
  // fields fire when the user leaves the field (the native 'change'
  // event). scheduleAdvancedSave() debounces so editing several fields
  // in a row coalesces into one save + one toast.
  el.querySelectorAll('[data-adv-field]').forEach(input => {
    input.addEventListener('change', () => {
      const fname = input.dataset.advField;
      const f = _advancedFields.find(x => x.field === fname);
      if (!f) return;
      // Dev mode arms tools that can rewrite server settings and swap
      // the running server version — confirm before enabling, matching
      // the stopSidecar / restart danger-action convention.
      if (fname === 'enable_dev_mode' && input.checked && !confirm(
        '⚠ Enable developer mode?\n\n'
        + 'After the next restart, hidden developer tools are exposed to '
        + 'connected AI agents. They can change server settings and '
        + 'replace the running server version. Only enable this for '
        + 'development and testing.'
      )) {
        input.checked = false;
        return;
      }
      let v;
      if (input.type === 'checkbox') v = input.checked;
      else if (input.type === 'number') v = (f.type === 'float') ? parseFloat(input.value) : parseInt(input.value, 10);
      else v = input.value;
      commitAdvancedEdit(fname, v);
    });
  });
}

// Stamp a parsed advanced-field value into the pending set and arm the
// debounced save. Shared by both renderers that wire up advanced inputs
// (the Server Settings panel and the code-mode sub-rows). A NaN value (an
// empty/garbage number field committing on blur) is dropped, and any prior
// pending value for the field is cleared so a stale earlier edit can't be
// persisted by the already-armed debounce.
function commitAdvancedEdit(field, value) {
  if (typeof value === 'number' && Number.isNaN(value)) { delete _advancedDirty[field]; return; }
  _advancedDirty[field] = value;
  scheduleAdvancedSave();
}

// Advanced fields auto-save (Server Settings tab). scheduleAdvancedSave()
// debounces edits the same way the Tools tab debounces toggle saves
// (scheduleSave), so tabbing through several fields coalesces into one save
// cycle and one toast. That cycle may be one or two POSTs — file-routed and
// addon-routed fields go as separate batches (see saveAdvancedSettings).
let advancedSaveTimer = null;
let _advSaving = false;
function scheduleAdvancedSave() {
  clearTimeout(advancedSaveTimer);
  advancedSaveTimer = setTimeout(saveAdvancedSettings, 800);
}

async function saveAdvancedSettings() {
  // Re-entrancy guard: if a save is already in flight, don't run a second
  // one concurrently (it would race the post-save reload's input refresh).
  // Re-arm the debounce so any edit made during the in-flight save still
  // gets saved once it completes.
  if (_advSaving) { scheduleAdvancedSave(); return; }
  if (Object.keys(_advancedDirty).length === 0) return;
  _advSaving = true;
  try {
  // Partition the dirty fields into addon-routed and file-routed
  // batches. The server rejects mixed batches with
  // 500 so the UI splits them client-side: addon-synced fields go in
  // their own POST (routes through Supervisor /addons/self/options),
  // file-mode fields go in a separate POST (writes the override file).
  // Both batches must succeed for the save to count.
  const addonDirty = {};
  const fileDirty = {};
  Object.entries(_advancedDirty).forEach(([fname, val]) => {
    const f = _advancedFields.find(x => x.field === fname);
    if (f && f.origin === 'addon') {
      addonDirty[fname] = val;
    } else {
      fileDirty[fname] = val;
    }
  });
  const batches = [];
  if (Object.keys(fileDirty).length) batches.push(fileDirty);
  if (Object.keys(addonDirty).length) batches.push(addonDirty);
  const restartFields = Object.keys(_advancedDirty);
  try {
    for (const payload of batches) {
      const resp = await fetch('./api/settings/advanced', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      // JSON parse can fail on a 200 with mangled body (proxy
      // injection, truncated response). Default to
      // ``{restart_required: true}`` on success-with-garbage so the
      // user still gets the restart banner; surface "save returned
      // non-JSON" on non-OK.
      let data;
      try {
        data = await resp.json();
      } catch (parseErr) {
        console.error('saveAdvancedSettings JSON parse failed:', parseErr);
        if (resp.ok) {
          data = {restart_required: true};
        } else {
          showToast(`Save failed (HTTP ${resp.status}, non-JSON body)`, {isError: true});
          return;
        }
      }
      if (!resp.ok) {
        let msg = 'Save failed';
        if (data && data.error) {
          if (typeof data.error === 'string') msg = data.error;
          else if (data.error.message) msg = data.error.message;
        }
        showToast(msg, {isError: true});
        return;
      }
    }
    // Confirm the save. When the saved field(s) need a restart, say so in
    // the toast itself (the restartNotice banner also appears) so the
    // requirement is never silent.
    const needsRestart = restartFields.some(
      f => ADVANCED_RESTART_REQUIRED.has(f)
    );
    showToast(needsRestart ? 'Saved. Restart required.' : 'Saved.');
    if (needsRestart) {
      markRestartRequired();
    }
    // Clear only the fields we just saved — an edit that arrived while the
    // POST was in flight stays pending for the next scheduled save.
    restartFields.forEach(f => { delete _advancedDirty[f]; });
    // Refresh display so origins update (default -> file, etc.) — but ONLY
    // when it is safe to rebuild the panel. renderAdvancedSection does
    // innerHTML='', which drops focus and wipes typing in other fields. Skip
    // the reload while either (a) an edit is still pending in _advancedDirty
    // (e.g. arrived during the in-flight POST — its re-armed save reloads
    // later), or (b) the user is actively typing in another advanced field
    // whose change hasn't fired yet (not in _advancedDirty), which the reload
    // would otherwise discard. A later save reloads once safe.
    const editingAdvField = !!(
      document.activeElement
      && document.activeElement.closest
      && document.activeElement.closest('[data-adv-field]')
    );
    if (Object.keys(_advancedDirty).length === 0 && !editingAdvField) {
      // Await so a reload failure surfaces via toast rather than leaving stale data.
      try {
        await loadAdvancedSettings();
      } catch (reloadErr) {
        console.error('post-save reload failed:', reloadErr);
        showToast('Saved (reload failed; refresh to verify).');
      }
    }
  } catch (err) {
    showToast('Network error: ' + String(err), {isError: true});
  }
  } finally {
    _advSaving = false;
  }
}

loadFeatureFlags();
loadAdvancedSettings();
loadTools();
loadFsCustomPaths();

// Auto-activate tab from ?tab=<name> query string (used by approval URLs
// generated by the policy middleware: /settings?tab=tool-security-policies&token=...).
// If a &token=X is present and the target is the policy tab, scroll to
// the matching pending entry once policyLoadPending() resolves.
(function activateTabFromQuery() {
  try {
    const params = new URLSearchParams(window.location.search);
    const target = params.get('tab');
    if (!target) return;
    const tabBtn = document.querySelector('.tab[data-panel="' + target + '"]');
    if (!tabBtn) return;
    activateTab(target);
    const token = params.get('token');
    if (token && target === 'tool-security-policies') {
      // policyLoadPending() runs inside activateTab; wait a tick then
      // scroll to the matching pending entry if it exists.
      setTimeout(() => {
        const row = document.querySelector('[data-pending-token="' + token + '"]');
        if (row && row.scrollIntoView) {
          row.scrollIntoView({behavior: 'smooth', block: 'center'});
        }
      }, 500);
    }
  } catch (_) { /* best-effort */ }
})();

// #1572 accessibility prefs — bidirectional binding between the header
// theme toggle, the Accessibility tab controls, and the underlying
// localStorage / <html> attributes. The anti-FOUC head script already set
// the initial attributes; this module keeps them in sync for the rest of
// the session and persists user changes. The block from `const PREFS` to
// `const APPLY` must stay logically identical to the copy in
// site/src/layouts/Layout.astro (comments and formatting may differ) —
// enforced by tests/src/unit/test_anti_fouc_parity.py.
(function bindAccessibilityPrefs() {
  const root = document.documentElement;
  const mql = window.matchMedia('(prefers-color-scheme: light)');
  const PREFS = {
    theme:    { key: 'ha-mcp-theme',         default: 'auto'      },
    fontSize: { key: 'ha-mcp-font-size',     default: '100'       },
    contrast: { key: 'ha-mcp-contrast',      default: 'normal'    },
    shade:    { key: 'ha-mcp-shade',         default: 'off-white' },
    custom:   { key: 'ha-mcp-custom-colors', default: ''          },
  };
  // One-click presets (Firefox Reader View shape, #1574 review): each sets
  // the full (theme, shade, contrast) triple; the checked chip is derived
  // back from the stored triple, so hand-edited combos simply match none.
  const PRESETS = {
    dark:     { theme: 'dark',  shade: 'off-white', contrast: 'normal' },
    light:    { theme: 'light', shade: 'off-white', contrast: 'normal' },
    auto:     { theme: 'auto',  shade: 'off-white', contrast: 'normal' },
    paper:    { theme: 'light', shade: 'paper',     contrast: 'normal' },
    gray:     { theme: 'light', shade: 'gray',      contrast: 'normal' },
    contrast: { theme: 'light', shade: 'pure',      contrast: 'high'   },
  };
  const read = (p) => {
    try { return localStorage.getItem(PREFS[p].key) || PREFS[p].default; }
    catch (_) { return PREFS[p].default; }
  };
  const write = (p, v) => {
    let stored = true;
    try { localStorage.setItem(PREFS[p].key, v); } catch (_) { stored = false; }
    // Surface-specific follow-up (server sync on the settings UI,
    // blocked-storage note on both) lives outside this mirrored core.
    if (window.__haMcpPrefsHook) window.__haMcpPrefsHook(p, v, stored);
  };

  // Apply functions mirror what the anti-FOUC head script does, so the
  // runtime path and the pre-paint path stay observably identical.
  const applyTheme = (pref) => {
    const resolved = pref === 'auto' ? (mql.matches ? 'light' : 'dark') : pref;
    root.setAttribute('data-theme', resolved);
  };
  const applyFontSize = (pct) => {
    const n = parseInt(pct, 10);
    if (isNaN(n) || n <= 100 || n > 150) root.style.fontSize = '';
    else root.style.fontSize = (16 * n / 100) + 'px';
  };
  const applyContrast = (tier) => {
    if (tier === 'high') root.setAttribute('data-contrast', 'high');
    else root.removeAttribute('data-contrast');
  };
  const applyShade = (shade) => {
    if (shade && shade !== 'off-white') root.setAttribute('data-shade', shade);
    else root.removeAttribute('data-shade');
  };
  const HEX_RE = /^#[0-9a-fA-F]{6}$/;
  const channels = (hex) =>
    parseInt(hex.slice(1, 3), 16) + ' ' + parseInt(hex.slice(3, 5), 16) + ' ' + parseInt(hex.slice(5, 7), 16);
  // Custom colors layer on top of any preset as inline styles (inline beats
  // stylesheet rules, so the cascade is: preset CSS -> user custom). Both
  // surfaces' variable names are written; names a surface does not use are
  // inert. Returns the parsed object so callers can reuse it.
  const CUSTOM_VARS = {
    bg:     { hex: ['--bg'], chan: ['--surface-0'] },
    text:   { hex: ['--text', '--text-primary'], chan: [] },
    accent: { hex: ['--accent', '--accent-text'], chan: ['--brand'] },
  };
  const applyCustom = (raw) => {
    for (const part in CUSTOM_VARS) {
      CUSTOM_VARS[part].hex.concat(CUSTOM_VARS[part].chan).forEach((name) => root.style.removeProperty(name));
    }
    let custom = {};
    try { custom = JSON.parse(raw || '{}') || {}; } catch (_) { return {}; }
    for (const part in CUSTOM_VARS) {
      const v = custom[part];
      if (typeof v !== 'string' || !HEX_RE.test(v)) continue;
      CUSTOM_VARS[part].hex.forEach((name) => root.style.setProperty(name, v));
      CUSTOM_VARS[part].chan.forEach((name) => root.style.setProperty(name, channels(v)));
    }
    return custom;
  };
  const APPLY = { theme: applyTheme, fontSize: applyFontSize, contrast: applyContrast, shade: applyShade, custom: applyCustom };

  // #1574 review: localStorage is the synchronous store the anti-FOUC
  // script reads at paint time, but it is origin-scoped and the stdio
  // sidecar binds a fresh random port (= fresh empty origin) per session.
  // This hook therefore (a) mirrors every change to the server
  // (./api/settings/theme -> theme_prefs.json), which seeds the next
  // fresh origin via the server-prefs head script, and (b) surfaces a
  // blocked localStorage (private mode) once instead of silently losing
  // the choices on reload. Debounced so color-picker drags don't flood
  // the endpoint; best-effort because the browser copy already applied.
  const storageNote = document.getElementById('a11y-storage-note');
  const pendingPrefs = {};
  let prefsSyncTimer = null;
  window.__haMcpPrefsHook = (pref, value, stored) => {
    if (!stored && storageNote) storageNote.hidden = false;
    pendingPrefs[pref] = value;
    clearTimeout(prefsSyncTimer);
    prefsSyncTimer = setTimeout(() => {
      const body = JSON.stringify(pendingPrefs);
      for (const k in pendingPrefs) delete pendingPrefs[k];
      fetch('./api/settings/theme', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      }).then((resp) => {
        // A non-2xx means client and server disagree about valid values —
        // an implementation bug worth a console trail, not a user error.
        if (!resp.ok) console.warn('ha-mcp: theme prefs not persisted:', resp.status);
      }).catch(() => { /* offline / sidecar gone — localStorage still has it */ });
    }, 400);
  };

  const setPref = (pref, value) => {
    write(pref, value);
    APPLY[pref](value);
  };
  const applyPreset = (name) => {
    const p = PRESETS[name];
    if (!p) return;
    setPref('theme', p.theme);
    setPref('shade', p.shade);
    setPref('contrast', p.contrast);
  };
  const reflectPreset = () => {
    const theme = read('theme'), shade = read('shade'), contrast = read('contrast');
    let active = '';
    for (const name in PRESETS) {
      const p = PRESETS[name];
      if (p.theme === theme && p.shade === shade && p.contrast === contrast) { active = name; break; }
    }
    document.querySelectorAll('input[type="radio"][name="a11y-preset"]').forEach((r) => {
      r.checked = (r.value === active);
    });
  };
  const reflectRadio = (name, value) => {
    document.querySelectorAll('input[type="radio"][name="' + name + '"]').forEach((r) => {
      r.checked = (r.value === value);
    });
  };
  // WCAG 2.x relative-luminance contrast for the custom-color warning.
  const luminance = (hex) => {
    const f = (i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(1) + 0.7152 * f(3) + 0.0722 * f(5);
  };
  const updateContrastWarning = (custom) => {
    const warn = document.getElementById('a11y-contrast-warning');
    if (!warn) return;
    const comparable = HEX_RE.test(custom.bg || '') && HEX_RE.test(custom.text || '');
    if (!comparable) { warn.hidden = true; return; }
    const l1 = luminance(custom.bg), l2 = luminance(custom.text);
    warn.hidden = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05) >= 4.5;
  };

  // Header <select> for theme — quick Dark/Light/Auto access.
  const headerToggle = document.getElementById('themeToggle');
  if (headerToggle) {
    headerToggle.value = read('theme');
    headerToggle.addEventListener('change', () => {
      setPref('theme', headerToggle.value);
      reflectPreset();
      refreshSwatches();
    });
  }

  // Re-apply when the OS preference flips while in Auto.
  mql.addEventListener('change', () => {
    if (read('theme') === 'auto') {
      applyTheme('auto');
      refreshSwatches();
    }
  });

  reflectRadio('a11y-font-size', read('fontSize'));
  reflectPreset();
  // Initialize each swatch from the saved custom value, falling back to
  // the theme's current computed color so the wells show reality instead
  // of an arbitrary black default.
  const cssColorToHex = (value) => {
    const v = (value || '').trim();
    if (HEX_RE.test(v)) return v;
    let m = v.match(/^rgba?\((\d+)[, ]+(\d+)[, ]+(\d+)/);
    if (!m) m = v.match(/^(\d+) (\d+) (\d+)$/);
    if (!m) return '';
    return '#' + [m[1], m[2], m[3]].map((n) => (+n).toString(16).padStart(2, '0')).join('');
  };
  const currentColorFor = (part) => {
    const body = getComputedStyle(document.body);
    const rootStyle = getComputedStyle(root);
    if (part === 'bg') return cssColorToHex(body.backgroundColor);
    if (part === 'text') return cssColorToHex(body.color);
    return cssColorToHex(rootStyle.getPropertyValue('--accent')) ||
      cssColorToHex(rootStyle.getPropertyValue('--brand'));
  };
  const customInputs = document.querySelectorAll('input[type="color"][data-custom]');
  // Sync the swatch wells with reality: a stored custom value wins,
  // otherwise the active theme's computed color. Re-run after anything
  // that changes the active palette (preset click, header toggle, OS
  // auto-flip, clear, reset) so the wells never keep showing a previous
  // theme's colors.
  const refreshSwatches = () => {
    let custom = {};
    try { custom = JSON.parse(read('custom') || '{}') || {}; } catch (_) { /* ignore */ }
    customInputs.forEach((inp) => {
      const v = custom[inp.dataset.custom];
      const fallback = currentColorFor(inp.dataset.custom);
      if (HEX_RE.test(v || '')) inp.value = v;
      else if (fallback) inp.value = fallback;
    });
  };
  updateContrastWarning(applyCustom(read('custom')));
  refreshSwatches();
  customInputs.forEach((inp) => {
    inp.addEventListener('input', () => {
      let custom = {};
      try { custom = JSON.parse(read('custom') || '{}') || {}; } catch (_) { /* start fresh */ }
      custom[inp.dataset.custom] = inp.value;
      write('custom', JSON.stringify(custom));
      applyCustom(read('custom'));
      updateContrastWarning(custom);
    });
  });
  const clearBtn = document.getElementById('a11y-custom-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      write('custom', '');
      updateContrastWarning(applyCustom(''));
      refreshSwatches();
    });
  }

  // Scope the change listener to the Accessibility panel rather than the
  // document: the settings page carries dozens of unrelated inputs (tool
  // toggles, server fields, backup forms) and a document-level listener
  // would fire on every one of them just to bail the filter.
  const a11yPanel = document.getElementById('panel-accessibility');
  if (a11yPanel) {
    a11yPanel.addEventListener('change', (e) => {
      const t = e.target;
      if (!(t instanceof HTMLInputElement) || t.type !== 'radio') return;
      if (t.name === 'a11y-preset') {
        applyPreset(t.value);
        if (headerToggle) headerToggle.value = read('theme');
        refreshSwatches();
      } else if (t.name === 'a11y-font-size') {
        setPref('fontSize', t.value);
      }
    });
  }

  const resetBtn = document.getElementById('a11y-reset');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      setPref('theme', PREFS.theme.default);
      setPref('shade', PREFS.shade.default);
      setPref('contrast', PREFS.contrast.default);
      setPref('fontSize', PREFS.fontSize.default);
      write('custom', '');
      updateContrastWarning(applyCustom(''));
      reflectRadio('a11y-font-size', PREFS.fontSize.default);
      reflectPreset();
      if (headerToggle) headerToggle.value = PREFS.theme.default;
      refreshSwatches();
    });
  }
})();

// --- Entity visibility filter tab (#1728) ---
let visibilityVersion = 1;

function _visibilityShowLoadError(msg) {
  const el = document.getElementById('visibility-load-error');
  if (!el) return;
  el.style.display = msg ? '' : 'none';
  el.textContent = msg || '';
}

function _visibilityParseList(value, sep) {
  return value.split(sep).map(s => s.trim()).filter(Boolean);
}

async function visibilityLoadConfig() {
  _visibilityShowLoadError('');
  let resp;
  try {
    resp = await fetch('./api/visibility/config');
  } catch (e) {
    _visibilityShowLoadError('Could not reach the server: ' + e.message);
    return;
  }
  if (!resp.ok) {
    let detail = 'HTTP ' + resp.status;
    try {
      const body = await resp.json();
      if (body && body.error) detail = body.error;
      if (body && body.visibility_file_corrupt) {
        detail += ' (entity_visibility.json appears corrupt; edit or delete it on the App (add-on) /data volume)';
      }
    } catch (_e) { /* keep the HTTP-status fallback */ }
    _visibilityShowLoadError('Failed to load visibility config: ' + detail);
    return;
  }
  const c = await resp.json();
  visibilityVersion = c.version ?? 1;
  const cats = c.exclude_categories || [];
  document.getElementById('visibility-enabled').checked = !!c.enabled;
  document.getElementById('visibility-cat-diagnostic').checked = cats.includes('diagnostic');
  document.getElementById('visibility-cat-config').checked = cats.includes('config');
  document.getElementById('visibility-exclude-hidden').checked = !!c.exclude_hidden;
  document.getElementById('visibility-areas').value = (c.exclude_areas || []).join(', ');
  document.getElementById('visibility-labels').value = (c.exclude_labels || []).join(', ');
  document.getElementById('visibility-deny').value = (c.deny_entity_ids || []).join('\n');
  document.getElementById('visibility-allow-areas').value = (c.allow_areas || []).join(', ');
  document.getElementById('visibility-allow-labels').value = (c.allow_labels || []).join(', ');
  document.getElementById('visibility-allow-entities').value = (c.allow_entity_ids || []).join('\n');
  document.getElementById('visibility-respect-assist').checked = !!c.respect_assist_exposure;
}

async function visibilitySaveConfig() {
  const statusEl = document.getElementById('visibility-save-status');
  const cats = [];
  if (document.getElementById('visibility-cat-diagnostic').checked) cats.push('diagnostic');
  if (document.getElementById('visibility-cat-config').checked) cats.push('config');
  const config = {
    version: visibilityVersion,
    enabled: document.getElementById('visibility-enabled').checked,
    exclude_categories: cats,
    exclude_hidden: document.getElementById('visibility-exclude-hidden').checked,
    deny_entity_ids: _visibilityParseList(document.getElementById('visibility-deny').value, '\n'),
    exclude_areas: _visibilityParseList(document.getElementById('visibility-areas').value, ','),
    exclude_labels: _visibilityParseList(document.getElementById('visibility-labels').value, ','),
    allow_areas: _visibilityParseList(document.getElementById('visibility-allow-areas').value, ','),
    allow_labels: _visibilityParseList(document.getElementById('visibility-allow-labels').value, ','),
    allow_entity_ids: _visibilityParseList(document.getElementById('visibility-allow-entities').value, '\n'),
    respect_assist_exposure: document.getElementById('visibility-respect-assist').checked,
  };
  setStatusAlert(statusEl, false);
  statusEl.textContent = 'Saving...';
  let resp;
  try {
    resp = await fetch('./api/visibility/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
  } catch (e) {
    setStatusAlert(statusEl, true);
    statusEl.textContent = 'Save failed: ' + e.message;
    return;
  }
  if (resp.status === 409) {
    // Do NOT reload the config here: that would overwrite the user's unsaved
    // edits (there would be nothing left to "re-apply"). Keep the edits in the
    // form, surface the conflict, and let the user reload deliberately — mirrors
    // the policy tab's optimistic-lock message.
    setStatusAlert(statusEl, true);
    statusEl.textContent =
      'Config was changed in another tab or session. Reload the page to see the '
      + 'latest, then re-apply your changes.';
    return;
  }
  if (!resp.ok) {
    let detail = 'HTTP ' + resp.status;
    try { const b = await resp.json(); if (b && b.error) detail = b.error; } catch (_e) { /* fallback */ }
    setStatusAlert(statusEl, true);
    statusEl.textContent = 'Save failed: ' + detail;
    return;
  }
  const body = await resp.json();
  visibilityVersion = body.version ?? (visibilityVersion + 1);
  setStatusAlert(statusEl, false);
  statusEl.textContent = 'Saved.';
}

(function wireVisibilitySave() {
  const btn = document.getElementById('visibility-save-btn');
  if (btn) btn.addEventListener('click', visibilitySaveConfig);
})();
