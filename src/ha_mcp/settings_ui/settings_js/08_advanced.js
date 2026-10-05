// ===== Advanced settings =====
const ADVANCED_FIELD_META = {
  homeassistant_url:   { label: "Home Assistant URL",          help: "Display only. Set via HOMEASSISTANT_URL env var or App (add-on)-managed (Supervisor)." },
  homeassistant_token: { label: "Home Assistant token",        help: "Display only. Set via HOMEASSISTANT_TOKEN env var. Masked here for security." },
  timeout:             { label: "HA request timeout (s)",      help: "Per-request HTTP timeout. Range 1–600. Restart required." },
  max_retries:         { label: "HA request max retries",      help: "Retry budget per failed REST call. Range 0–20. Restart required." },
  verify_ssl:          { label: "Verify SSL certificates",     help: "Skip TLS verification only on trusted networks (self-signed certs, hostname mismatch). Restart required." },
  ha_tool_concurrency: { label: "Home Assistant tool concurrency", help: "Optional limit on outer Home Assistant tool calls across all MCP sessions. Internal REST, WebSocket, and per-tool fan-out concurrency is unchanged. 0 keeps unlimited behavior. A call waiting for capacity fails after 60 seconds. Range 0–32. Restart required." },
  fuzzy_threshold:     { label: "Fuzzy-search threshold",      help: "Lower = looser entity match. Range 0–100." },
  automation_config_time_budget: { label: "Automation config time budget (s)", help: "Max seconds deep search spends fetching automation configs before returning a partial result. Raise on instances with many automations. Range 1–600. Restart required." },
  script_config_time_budget:     { label: "Script config time budget (s)",     help: "Max seconds deep search spends fetching script configs before returning a partial result. Range 1–600. Restart required." },
  scene_config_time_budget:      { label: "Scene config time budget (s)",      help: "Max seconds deep search spends fetching scene configs before returning a partial result. Range 1–600. Restart required." },
  individual_config_timeout:     { label: "Per-request config fetch timeout (s)", help: "Timeout for each individual automation/script/scene config fetch during deep search. On HA servers that serve config reads serially, raise this and/or lower the batch size so queued requests don't time out. Values above the HA request timeout (HA_TIMEOUT, default 30) have no extra effect — the HTTP client gives up first. Range 1–600. Restart required." },
  individual_fetch_batch_size:   { label: "Config fetch batch size",          help: "How many per-id config fetches deep search issues concurrently. Lower toward 1 on HA servers that serve config reads serially (symptom: 'timed out' partial-result warnings). Range 1–100. Restart required." },
  enable_history_query_guardrails: { label: "History query guardrails",         help: "Reject recorder queries that exceed conservative entity and time-range budgets. Disabled by default." },
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
  http_transport_diagnostics: { label: "HTTP transport diagnostics", help: "Experimental. Automatically enable INFO logging for HTTP diagnostics while the server runs, without payloads or credentials. Other loggers are unchanged; explicit Home Assistant logger overrides and filters still apply. Does not confirm client receipt. HTTP only; restart required." },
  http_json_response: { label: "JSON responses instead of streaming", help: "Experimental. Return a single JSON response instead of an SSE stream. Affects all HTTP clients on this server and disables streamed progress for these responses. HTTP only; restart required." },
  code_mode_max_duration:    { label: "Code-mode max duration (s)",   help: "Wall-clock budget per sandbox run. Range 1–300. Restart required." },
  code_mode_max_memory:      { label: "Code-mode max memory (bytes)", help: "RSS cap per sandbox run. Range 1 MB–256 MB. Restart required." },
  code_mode_max_recursion:   { label: "Code-mode max recursion",      help: "Recursion-depth cap per sandbox run. Restart required." },
  code_mode_max_invocations: { label: "Code-mode max invocations",    help: "API/tool-call cap per sandbox run. Restart required." },
  code_mode_saved_tools_path:{ label: "Saved-tools path",              help: "JSON file where ha_manage_custom_tool persists saved tools across restarts. Restart required." },
  extra_yaml_write_keys:     { label: "Extra YAML write keys",        help: "Extra top-level keys ha_config_set_yaml may write in addition to the built-in ones, for YAML-first integrations on this install (e.g. alert2). Enter one key per line or separate keys with commas. Keys that redefine Home Assistant's own trust boundary can never be added and are ignored. Requires custom component 1.2.4 or newer." },
  sidecar_pin_port:    { label: "Settings UI sidecar port",    help: "0 picks a free port on first start and keeps it for later restarts; 1024–65535 pins a preferred port (falls back to a free one if taken). Restart required." },
  enable_dev_mode:     { label: "Developer mode",               help: "⚠ DANGER: registers hidden developer tools (ha_dev_manage_server, ha_dev_manage_settings) that let AI agents change server settings and replace the running server version (e.g. install a PR build). For development and testing only. Restart required." },
  dev_tools_security_policy_access: { label: "Dev tools security policy access", help: "⚠ DANGER: while developer mode is on, lets the developer tools rewrite tool security policies, add or remove per-tool approval gates, and approve or deny pending approvals on your behalf — an AI agent can accept its own gated calls. For policy testing only. Takes effect without a restart." },
};

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
      escapeHtml(t('advanced.errors.network', {}, 'Advanced settings unavailable (network error reaching /api/settings/advanced).')) +
      '</div></div>';
    return;
  }
  if (!resp.ok) {
    if (errSlot) errSlot.innerHTML =
      `<div class="adv-row"><div class="adv-help">` +
      `${escapeHtml(t('advanced.errors.http', {status: resp.status}, `Advanced settings unavailable (HTTP ${resp.status}).`))}</div></div>`;
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (err) {
    console.error('loadAdvancedSettings JSON parse failed:', err);
    if (errSlot) errSlot.innerHTML =
      '<div class="adv-row"><div class="adv-help">' +
      escapeHtml(t('advanced.errors.json', {}, 'Advanced settings response was not valid JSON.')) + '</div></div>';
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
  // Re-render feature flags so the advanced-backed sub-rows (code_mode
  // numerics, extra YAML write keys) show up beneath their parent toggles
  // (race: loadFeatureFlags may have run before _advancedFields was
  // populated). Cheap no-op if feature flags haven't loaded yet.
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
  // by the stdio settings-UI sidecar. In HTTP/OAuth/addon deployments
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
  note.textContent = t(
    'advanced.sidecar.stdio_only',
    {},
    'Available in stdio mode only. This server runs over HTTP, which has no settings-UI sidecar to pin.'
  );
  section.parentNode.insertBefore(note, section);
}

function renderAdvancedSection(containerId, fields) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '';
  fields.forEach(f => {
    const row = document.createElement('div');
    row.className = 'adv-row' + (f.editable ? '' : ' locked');
    const meta = localizeMeta(
      'advanced',
      f.field,
      ADVANCED_FIELD_META[f.field] || { label: f.field, help: '' }
    );
    let controlHtml;
    if (f.choices) {
      controlHtml = `<select name="adv:${escapeHtml(f.field)}" data-adv-field="${escapeHtml(f.field)}" aria-labelledby="label-adv-${escapeHtml(f.field)}" ${f.editable ? '' : 'disabled'}>` +
        f.choices.map(c =>
          `<option value="${escapeHtml(c)}" ${String(f.value) === c ? 'selected' : ''}>${escapeHtml(t(`advanced.${f.field}.choices.${c}`, {}, c))}</option>`
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
      originMsg = escapeHtml(t('origins.display_only', {}, 'Display only. Modify via env var or App (add-on) settings.'));
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
        t(
          'advanced.enable_dev_mode.confirm',
          {},
          '⚠ Enable developer mode?\n\nAfter the next restart, hidden developer tools are exposed to connected AI agents. They can change server settings and replace the running server version. Only enable this for development and testing.'
        )
      )) {
        input.checked = false;
        return;
      }
      // Policy access hands the same agents the security policies that
      // gate them (and the approval queue) — its own confirm, since it
      // is stored independently of the dev-mode toggle above (it only
      // takes effect while dev mode is on).
      if (fname === 'dev_tools_security_policy_access' && input.checked && !confirm(
        t(
          'advanced.dev_tools_security_policy_access.confirm',
          {},
          '⚠ Let developer tools change security policies?\n\nWhile developer mode is on, connected AI agents will be able to rewrite tool security policies, add or remove per-tool approval gates, and approve or deny pending approvals — including their own gated calls. Enable only while testing policies.'
        )
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
          showToast(t(
            'errors.save_failed_non_json',
            {status: resp.status},
            `Save failed (HTTP ${resp.status}, non-JSON body)`
          ), {isError: true});
          return;
        }
      }
      if (!resp.ok) {
        let msg = t('errors.save_failed', {}, 'Save failed');
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
      f => _advancedFields.some(x => x.field === f && x.restart_required)
    );
    showToast(needsRestart
      ? t('status.saved_restart', {}, 'Saved. Restart required.')
      : t('status.saved', {}, 'Saved.'));
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
        showToast(t('status.saved_reload_failed', {}, 'Saved (reload failed; refresh to verify).'));
      }
    }
  } catch (err) {
    showToast(t('errors.network', {message: String(err)}, 'Network error: ' + String(err)), {isError: true});
  }
  } finally {
    _advSaving = false;
  }
}

loadFeatureFlags();
loadAdvancedSettings();
loadTools();
loadFsCustomPaths();

