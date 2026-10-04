async function loadTools() {
  let resp;
  try {
    resp = await fetch('./api/settings/tools');
  } catch (e) {
    updateStatus(t(
      'errors.network_endpoint',
      {endpoint: '/api/settings/tools', message: e.message},
      'Network error reaching /api/settings/tools: ' + e.message
    ), false, true);
    return;
  }
  if (!resp.ok) {
    updateStatus(t(
      'errors.http_endpoint',
      {endpoint: '/api/settings/tools', status: resp.status, detail: resp.statusText},
      `/api/settings/tools returned HTTP ${resp.status} ${resp.statusText}`
    ), false, true);
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (e) {
    updateStatus(t(
      'errors.json_endpoint',
      {endpoint: '/api/settings/tools', message: e.message},
      'Failed to parse /api/settings/tools response as JSON: ' + e.message
    ), false, true);
    return;
  }
  toolData = data.tools || [];
  toolStates = data.states || {};
  toolEnvPinned = data.env_pinned || {};
  bpsLockedTools = new Set(data.bps_locked_tools || []);
  toolLlm = data.llm_api || {};
  toolLlmOverrides = data.llm_api_overrides || {};
  llmApiAvailable = !!data.llm_api_available;
  READ_ONLY_EXEMPT = new Set(data.read_only_exempt || []);
  // Load policy state before the first render so the "security gated"
  // toggle reflects current policy.rules. loadPolicyState() never throws
  // — it keeps the prior gate state on failure.
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
      t(
        'tools.empty',
        {},
        'No tools found. The sidecar reads ~/.ha-mcp/tool_metadata.json. ' +
        'If it is missing or empty, restart your MCP client. See ~/.ha-mcp/sidecar.log for details.'
      ),
      false, true
    );
    return;
  }
  try {
    render();
  } catch (e) {
    updateStatus(t(
      'errors.render',
      {message: e.message},
      'Render failed: ' + e.message + ' (open browser devtools for the stack)'
    ), false, true);
    throw e;
  }
  updateStatus(t('status.loaded', {}, 'Loaded'));
}

// Restart-chrome half of applyInfoChrome: which restart control is shown
// and what the restart notice tells the user to do. Split out (with the
// footer below) so applyInfoChrome stays orchestration — the branch ladder
// here alone sits near the complexity limit.
function _applyRestartChrome(info) {
  // Show restart button if running as add-on; show Stop Sidecar
  // button only when this page is served by the stdio sidecar
  // (HTTP modes serve the same HTML but is_sidecar=false there, so
  // clicking Stop wouldn't make sense — it would kill the MCP server).
  // Also tailor the restart-notice copy to the install mode so the
  // user is told exactly what action they need to take ("close and
  // reopen Claude Desktop" vs "click Restart Add-on" vs "restart
  // your Docker container") instead of a generic "restart the add-on"
  // that only matches one of three real deployment surfaces.
  const noticeEl = document.getElementById('restartNoticeText');
  if (info.is_addon) {
    document.getElementById('restartBtn').style.display = '';
    if (noticeEl) {
      noticeEl.textContent = t(
        'notice.restart.addon',
        {},
        '⚠ Changes saved. Click "Restart App (add-on)" for them to take effect. Then refresh the MCP tool list in your AI client.'
      );
    }
  } else if (info.deployment_mode === 'embedded') {
    // In-process (custom component) server: the restart endpoint reloads
    // the server config entry, which reinstalls-if-newer and swaps the
    // worker onto the freshly installed code.
    const rbtn = document.getElementById('restartBtn');
    rbtn.style.display = '';
    restartTargetEmbedded = true;
    rbtn.textContent = t('actions.restart_server', {}, 'Restart HA-MCP Server');
    if (noticeEl) {
      noticeEl.textContent = t(
        'notice.restart.embedded',
        {},
        '⚠ Changes saved. Click "Restart HA-MCP Server" for them to take effect. Then refresh the MCP tool list in your AI client.'
      );
    }
  } else if (info.is_sidecar) {
    if (noticeEl) {
      noticeEl.textContent = t(
        'notice.restart.sidecar',
        {},
        '⚠ Changes saved. Fully quit and reopen your MCP client for them to take effect.'
      );
    }
    document.getElementById('sidecarStopRow').style.display = '';
  } else if (noticeEl) {
    // HTTP / Docker / standalone — no button we can wire to a restart,
    // so describe the action in process terms, then the client-refresh
    // step (remote connectors cache the tool list, same as add-on mode).
    noticeEl.textContent = t(
      'notice.restart.standalone',
      {},
      '⚠ Changes saved. Restart the ha-mcp process, then refresh the MCP tool list in your AI client.'
    );
  }
}

// Footer — show the running build and the same deployment classification
// used by ha_report_issue. The backend keeps embedded and sidecar distinct
// because they need different restart behavior; preserve those concise
// names here and clarify the less obvious packaging-oriented values.
function _applyVersionFooter(info) {
  if (!info.version) return;
  const fEl = document.getElementById('versionFooterText');
  const deploymentMode = typeof info.deployment_mode === 'string'
    ? info.deployment_mode
    : '';
  const deploymentLabels = {
    embedded: 'embedded',
    sidecar: 'sidecar',
    addon: t('footer.deployment.addon', {}, 'app/add-on'),
    docker: t('footer.deployment.docker', {}, 'container/docker'),
    git: t('footer.deployment.git', {}, 'source checkout'),
    pypi: t('footer.deployment.pypi', {}, 'python package'),
    unknown: t('footer.deployment.unknown', {}, 'unknown'),
  };
  const deploymentLabel = Object.prototype.hasOwnProperty.call(
    deploymentLabels, deploymentMode
  ) ? deploymentLabels[deploymentMode] : deploymentMode;
  const installation = deploymentLabel
    ? ' · ' + t(
      'footer.installation',
      {method: deploymentLabel},
      'installation: {method}'
    )
    : '';
  if (fEl) fEl.textContent = 'ha-mcp ' + info.version + installation;
}

async function applyInfoChrome() {
  try {
    const infoResp = await fetch('./api/settings/info');
    const info = await infoResp.json();
    _applyRestartChrome(info);
    _applyVersionFooter(info);
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
  if (!confirm(t('server.sidecar.confirm_disable', {},
    '⚠ PERMANENTLY disable the settings server?\n\n' +
    'This stops the running server AND writes a disable marker so it will NOT respawn on future ha-mcp launches.\n\n' +
    'To restore access later, delete ~/.ha-mcp/settings_ui_disabled and unset HA_MCP_DISABLE_SETTINGS_UI. Continue?'
  ))) return;
  btn.disabled = true;
  btn.textContent = t('status.stopping', {}, 'Stopping...');
  try {
    const resp = await fetch('./api/settings/shutdown', {method: 'POST'});
    if (resp.ok) {
      btn.textContent = t('status.stopped_offline', {}, 'Stopped. This page will go offline');
    } else {
      let msg = t('errors.stop_failed', {}, 'Stop failed');
      try {
        const err = await resp.json();
        if (err.error && err.error.message) msg = t('errors.failed_detail', {detail: err.error.message}, 'Failed: ' + err.error.message);
      } catch (_e) {}
      btn.textContent = msg;
      btn.disabled = false;
      alert(msg);
    }
  } catch (_e) {
    // Connection drop is expected — the sidecar process is exiting.
    btn.textContent = t('status.stopped_connection', {}, 'Stopped (connection dropped)');
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
// True when the restart button drives the embedded (custom component) server
// rather than the app (add-on) — the wait/give-up copy must name the right
// thing (issue #2279 feedback: the embedded flow said "app" throughout).
let restartTargetEmbedded = false;

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
        const current = restartTargetEmbedded ? info.worker_id : info.instance_id;
        if (current && current !== previousInstanceId) {
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
  btn.textContent = t('status.restarting', {}, 'Restarting…');
  await new Promise(r => setTimeout(r, RESTART_PROBE_INITIAL_GRACE_MS));
  btn.textContent = restartTargetEmbedded
    ? t('status.waiting_server', {}, 'Waiting for the HA-MCP server to come back online…')
    : t('status.waiting_addon', {}, 'Waiting for App (add-on) to come back online…');
  const restarted = await _probeAddonRestarted(previousInstanceId);
  if (restarted) {
    window.location.reload();
  } else {
    // Probe gave up after RESTART_PROBE_MAX_TOTAL_MS. Restart either
    // never actually fired (silent supervisor failure → instance_id
    // never flipped) OR supervisor is genuinely slower than the cap.
    // Surface a clear next-step instead of silently doing nothing.
    btn.textContent = restartTargetEmbedded
      ? t('errors.server_not_back', {}, 'The HA-MCP server did not come back online. Reload the page manually.')
      : t('errors.addon_not_back', {}, 'App (add-on) did not come back online. Reload the page manually.');
    btn.disabled = false;
    restartInProgress = false;
  }
}

async function restartAddon() {
  if (restartInProgress) return;
  const btn = document.getElementById('restartBtn');
  if (!confirm(t('actions.restart_confirm', {}, 'Restart HA-MCP now? The page will reload automatically once it is back online.'))) return;
  restartInProgress = true;
  btn.disabled = true;
  btn.textContent = t('status.restarting', {}, 'Restarting…');
  // Capture the current process's ``instance_id`` BEFORE firing the
  // restart so the poll cycle has a baseline to compare against.
  // null is fine — the probe degrades to the old "any 200 means up"
  // mode rather than refusing to reload.
  const info = await _fetchSettingsInfo();
  // Embedded restarts reload the config entry inside the surviving HA
  // process, so instance_id never flips there; worker_id is pinned to the
  // server instance and flips exactly when the reload completes.
  const previousInstanceId = restartTargetEmbedded
    ? (info?.worker_id ?? null)
    : (info?.instance_id ?? null);
  try {
    const resp = await fetch('./api/settings/restart', {method: 'POST'});
    if (!resp.ok && resp.status < 500) {
      // 4xx is a genuine config error (e.g. SUPERVISOR_TOKEN unset).
      // The restart was NOT initiated — surface the error and let the
      // user fix the underlying cause. Keep button enabled so they
      // can retry once the issue is resolved. Don't broadcast (other
      // tabs would only see a misleading "restart in progress").
      let msg = t('errors.restart_failed', {}, 'Restart failed');
      try {
        const err = await resp.json();
        if (err?.error?.message) msg = t('errors.failed_detail', {detail: err.error.message}, 'Failed: ' + err.error.message);
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
      // Receivers may get this before their own init has classified the
      // deployment; without the sender's flag they would compare an
      // embedded worker_id baseline against instance_id and time out.
      targetEmbedded: restartTargetEmbedded,
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
      // Use the originating tab's baseline so every tab waits for the
      // SAME identity flip before reloading, and adopt its deployment
      // classification too: this listener can fire before this tab's own
      // init resolved it, and an embedded worker_id baseline compared
      // against instance_id would never flip. Falls back to null →
      // "any 200 = ready" mode if the originator couldn't capture one.
      if (typeof data.targetEmbedded === 'boolean') {
        restartTargetEmbedded = data.targetEmbedded;
      }
      _runRestartReloadCycle(data.previousInstanceId ?? null);
    }
  });
}

