// FEATURE_META:BEGIN GENERATED (scripts/generate_locales.py)
// English fallback + row order for the feature toggles. Do not edit:
// the strings and their order come from the features.* keys in
// locales/en.json — edit that file, then run
// scripts/generate_locales.py.
// The master/sub-flag gating these rows describe is enforced by
// config.py:_apply_feature_flag_overrides; the UI dims sub-rows and
// re-renders live when the master toggle flips.
const FEATURE_META = {
  enable_tool_search: {
    label: "Enable tool search",
    help: "Replace the full tool catalog with search-based discovery. Reduces idle context by roughly 90%, to about 5K tokens. ⚠️ Do NOT enable this in clients with their own built-in tool search / deferred tools (claude.ai, Claude Desktop, Claude Code) — the two search layers conflict, and the client's built-in tool search is the better choice there; leave this off. Whether tools are deferred depends on your client and model combination: use this when your setup loads the full catalog up front — models without native deferred tools (e.g. Gemini, OpenAI-compatible local models, Claude Haiku), clients that inline all tool schemas regardless of model (e.g. GitHub Copilot CLI), or smaller context windows. Some Codex models and ChatGPT include deferred tools too — check your client/model directly to confirm its features so you don't leave this enabled unnecessarily. Tools are found via ha_search_tools and executed via categorized proxies (read/write/delete). Requires restart to take effect.",
  },
  tool_search_max_results: {
    label: "Tool search max results",
    help: "Maximum number of hidden tools returned per ha_search_tools call when tool search is enabled; a pinned tool that ranks inside that top count is added as a short name-only entry on top of it. Lower values (2-3) save context tokens but may miss relevant tools. Requires restart.",
  },
  enable_tool_security_policies: {
    label: "Enable Tool Security Policies (advanced)",
    help: "Gate high-stakes tool calls (lock/alarm control, automation writes, etc.) behind user approval. When a guarded tool is called, the agent tells the user to open the Tool Security Policies tab in the web UI and click Approve before the call proceeds. Per-tool rules with optional argument conditions are configured in the Tool Security Policies tab. Off by default. Requires restart to take effect.",
  },
  read_only_mode: {
    label: "Read Only Mode",
    help: "Toggles all write tools off, and removes ability for tools to make any write or destructive calls. Mixed read/write tools — backups, Apps (add-ons), energy preferences, voice pipelines, and code mode when enabled — stay available with their write operations blocked. Same toggle as the web UI Tools tab. Off by default. Requires restart to take effect.",
  },
  redact_secrets: {
    label: "Redact Secrets",
    help: "Redacts secrets from tool responses before they reach the AI assistant. App (add-on) options and integration fields marked as passwords are replaced with a set/empty marker (so \"is this credential configured?\" stays answerable), and other tool responses are scrubbed of secret values the server has already seen (values shorter than 6 characters are skipped, since replacing tiny fragments would corrupt unrelated output). Fields not marked as passwords by their schema cannot be detected. Off by default.",
  },
  enable_mandatory_bps: {
    label: "Attach best-practice skills on writes",
    help: "Master switch for the write-tool skill content delivery feature (issue #1182). When enabled (default), the six config write tools (automations, scripts, scenes, helpers, dashboards, raw YAML) attach the canonical Home Assistant best-practice reference files under `skill_content` on every successful write, plus auto-embed any reference sections cited by best-practice warnings. Each tool also exposes a per-call `MandatoryBPS` parameter the agent can set to false on subsequent calls once it has the content. When this master switch is off, NO skill_content goes out regardless of the per-call parameter or BP warnings. Recommended ON as the first choice; disable only for models with very small context windows. Turning this off may degrade write accuracy. Requires restart to take effect.",
  },
  enable_strict_mandatory_bps: {
    label: "Strict best-practices mode",
    help: "Strict mode: prevents the client from using the tool until it can prove that it read the best practices. While on, the six best-practice write tools (automations, scripts, scenes, helpers, dashboards, raw YAML) are blocked and return an error directing the client to read the best-practices skill via ha_get_skill_guide and pass back the acknowledgment key it obtains there. While on, the ha_get_skill_guide tool is locked enabled — it is the only publisher of the acknowledgment key. Child of the \"Attach best-practice skills on writes\" option above and inert while that parent is off. Requires restart to take effect.",
  },
  enable_beta_features: {
    label: "Enable beta features (master)",
    help: "⚠ DANGER — these tools can PERMANENTLY DAMAGE your Home Assistant installation. They write to your YAML config, your filesystem, install custom components, run arbitrary sandboxed Python, and edit tool docstrings the AI sees. There is no warranty and no support guarantee — you enable them at your OWN RISK. Take a Home Assistant backup before turning this on, and never enable in production without one. Master gate for the experimental sub-flags below; sub-flags are ignored at runtime while this master is off, even when explicitly set to true. The same toggle is also surfaced in the web settings UI under \"Beta features (dangerous)\" — either surface reflects the other on restart.",
  },
  enable_yaml_config_editing: {
    label: "Enable YAML config editing (beta)",
    help: "Beta feature. Allows AI assistants to add, replace, or remove top-level keys in configuration.yaml and packages/*.yaml. Only whitelisted keys are allowed (e.g., template, sensor, command_line, mqtt, knx); core keys like homeassistant, http, and recorder are blocked. Each edit validates YAML syntax, runs a config check, and creates an automatic backup. Changes to most keys require a full HA restart to take effect. See docs/beta.md for known limitations. Dedicated tools (automations, scripts, scenes, helpers, template sensors) should be preferred when available. REQUIRES the master \"Enable beta features\" toggle above (and in the web UI) to be on — otherwise this sub-flag is ignored at runtime regardless of its value here.",
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
    help: "Sets HAMCP_ENABLE_FILESYSTEM_TOOLS=true. Enables direct file read/write access to your Home Assistant filesystem. WARNING: This gives the MCP server sensitive direct file access to your system. Only enable if you trust the AI assistant with file operations. Requires restart to take effect. REQUIRES the master \"Enable beta features\" toggle above (and in the web UI) to be on — otherwise this sub-flag is ignored at runtime regardless of its value here.",
  },
  enable_code_mode: {
    label: "Enable custom tool sandbox (beta)",
    help: "Beta feature. Enables the ha_manage_custom_tool tool, which lets AI assistants create, run, save, and delete custom Python code in a secure sandbox when no built-in tool can handle the request. Code runs in an isolated interpreter with no filesystem or arbitrary network access. Sandbox code can hit the HA REST API (api_get/api_post), send WebSocket commands (ws_send), call existing MCP tools (call_tool), or remove a saved tool (delete_saved_tool). Saved tools persist to /data/saved_tools.json by default so they survive app (add-on) restarts, and are visible to any client that can connect. See docs/beta.md for known limitations. Requires restart to take effect. REQUIRES the master \"Enable beta features\" toggle above (and in the web UI) to be on — otherwise this sub-flag is ignored at runtime regardless of its value here.",
  },
  enable_lite_docstrings: {
    label: "Enable lite tool docstrings (beta)",
    help: "Beta feature. Replaces the docstrings on 15 heavy ha-mcp tools (ha_config_get_automation, ha_config_set_automation, ha_config_get_script, ha_config_set_script, ha_config_get_scene, ha_config_set_scene, ha_config_list_helpers, ha_config_set_helper, ha_config_get_dashboard, ha_config_set_dashboard, ha_call_service, ha_config_set_yaml, ha_search, ha_manage_backup, ha_report_issue) with shorter variants that defer schema and example detail to the ha_get_skill_guide tool (or its skill:// resource). One exception: ha_report_issue defers to the instructions field of its own response instead. Tuning the Backup-hint setting still applies in lite mode. WARNING: this reduces idle token usage, but may degrade LLM performance — the trimmed descriptions rely on the LLM actually calling the skill tool or reading the skill resource for detail, which is not guaranteed (some models will skip the extra tool call and end up with less guidance than they had before). Best paired with a client that supports MCP resources or with enable_tool_search. Requires restart to take effect. REQUIRES the master \"Enable beta features\" toggle above (and in the web UI) to be on — otherwise this sub-flag is ignored at runtime regardless of its value here.",
  },
  enable_dashboard_screenshot: {
    label: "Enable dashboard screenshot mode (beta)",
    help: "Beta feature — disabled by default. Adds the ha_get_dashboard_screenshot tool plus include_screenshot / return_screenshot options on the dashboard get/set tools, so AI assistants can inspect one or more responsive Lovelace images (e.g. to verify a dashboard they just created). Supports stable named views, mobile/tablet/desktop batches, and PNG/JPEG/WebP/BMP output. Rendering runs in a separate, opt-in engine — balloob's \"Puppet\" App (add-on), headless Chromium — which you install once (add balloob's repository to the App store, then install \"Puppet\") and give a long-lived access token; on Docker/Container deployments you run that engine as a sidecar and set HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL. Nothing heavy is installed unless you both enable this and install the engine. Requires restart to take effect. REQUIRES the master \"Enable beta features\" toggle above (and in the web UI) to be on — otherwise this sub-flag is ignored at runtime regardless of its value here.",
  },
};
// FEATURE_META:END GENERATED

// The beta sub-flag fields gated by the master beta toggle. Populated
// from the ``beta_sub_flags`` array in the /api/settings/features
// response so the JS stays in sync with Python's
// ``config.BETA_FEATURE_FIELDS`` without duplicating the name list here.
let BETA_SUB_FLAGS = new Set();

// Sub-flags of ``enable_yaml_config_editing`` (confirm-flow toggle +
// per-key packages gates). They ARE in BETA_SUB_FLAGS (it mirrors
// config.BETA_FEATURE_FIELDS verbatim), but the main render pass skips
// them via the includes() guard below, and renderSubFlagRows
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
// renderSubFlagRows re-renders it nested beneath its parent so
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
  env: t('origins.env_locked', {}, 'Set via environment variable; unset it to edit here.'),
  // addon-origin fields are editable: save POSTs through Supervisor
  // /addons/self/options and triggers a restart so both surfaces stay
  // in sync. No locked note needed.
};

const ORIGIN_INFO_NOTE = {
  addon: t('origins.addon_synced', {}, 'Synced to the App (add-on) Configuration tab. Restart required after save.'),
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
    return tHtml('origins.env_var_unset', {variable: envVarTag}, 'Set via env var {variable}; unset it to edit here.');
  }
  if (fieldName === 'enable_beta_features') {
    return tHtml(
      'origins.beta_legacy_bridge',
      {variable: envVarTag},
      'Auto-enabled in App (add-on) mode (legacy bridge; your options.json predates the master toggle schema entry). Set <code>enable_beta_features</code> explicitly in the App (add-on) Configuration tab to take direct control. (env: {variable})'
    );
  }
  return tHtml(
    'origins.addon_runtime',
    {variable: envVarTag},
    'Set by the App (add-on) runtime environment, managed by Home Assistant Supervisor; cannot be changed from this web UI. (env: {variable})'
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
      escapeHtml(t('features.errors.network', {}, 'Feature flags unavailable (network error reaching /api/settings/features).')) +
      '</div></div>';
    return;
  }
  if (!resp.ok) {
    document.getElementById('featuresBody').innerHTML =
      `<div class="feature-row"><div class="feature-help">` +
      `${escapeHtml(t('features.errors.http', {status: resp.status}, `Feature flags unavailable (HTTP ${resp.status}).`))}</div></div>`;
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (err) {
    console.error('loadFeatureFlags JSON parse failed:', err);
    document.getElementById('featuresBody').innerHTML =
      '<div class="feature-row"><div class="feature-help">' +
      escapeHtml(t('features.errors.json', {}, 'Feature flags response was not valid JSON.')) + '</div></div>';
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
    // by renderSubFlagRows below right after their parent so
    // the nesting reads in source order.
    if (YAML_PACKAGES_SUB_FLAGS.includes(fieldName)) return;
    // Same for the strict-mode sub-row — rendered by
    // renderSubFlagRows right after its enable_mandatory_bps parent.
    if (MANDATORY_BPS_SUB_FLAGS.includes(fieldName)) return;
    const meta = localizeMeta('features', fieldName, FEATURE_META[fieldName]);
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
      `<div class="feature-name" id="label-feature-${escapeHtml(fieldName)}">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(helpWithFacts(meta.help, f))}</div>` +
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
      renderAdvancedSubRows(
        targetBody, 'beta_codemode', 'codemode-sub', !masterOn || !codeModeOn
      );
    }
    // After rendering the enable_yaml_config_editing parent, inject
    // its 3 per-key sub-rows (automation/script/scene). Dimmed when
    // either the master beta is off (parent forced off) or the parent
    // itself is off.
    if (fieldName === 'enable_yaml_config_editing') {
      const parentOn = !!f.value;
      renderSubFlagRows(flags, targetBody, YAML_PACKAGES_SUB_FLAGS, {
        cssClass: 'yaml-packages-sub',
        lockedByGate: !masterOn || !parentOn,
      });
      // Extra write keys (#1887) – same nesting depth as the per-key
      // toggles above, but a text value from the advanced cache.
      renderAdvancedSubRows(
        targetBody, 'beta_yamlkeys', 'yaml-packages-sub', !masterOn || !parentOn
      );
    }
    // After the enable_mandatory_bps parent row, inject its strict-mode
    // sub-row. This whole group is non-beta, so the only gate is the
    // parent toggle (no master beta involved).
    if (fieldName === 'enable_mandatory_bps') {
      const parentOn = !!f.value;
      renderSubFlagRows(flags, targetBody, MANDATORY_BPS_SUB_FLAGS, {
        cssClass: 'mandatory-bps-sub',
        lockedByGate: !parentOn,
      });
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

// Shared renderer for bool sub-flag rows nested under a parent toggle
// (yaml-packages under enable_yaml_config_editing, strict mode under
// enable_mandatory_bps). Each row is a checkbox+slider whose change
// handler syncs _lastFeatureFlags and POSTs via saveFeatureFlag, dimmed
// + input-disabled when ``lockedByGate`` is true. Callers precompute
// lockedByGate from whatever gates apply to their group (yaml-packages:
// master beta AND parent; mandatory-bps: parent only) and pass the CSS
// class that carries the group's indent/guide-bar depth. The number/text
// code-mode sub-numerics are NOT rendered here — they save through
// commitAdvancedEdit and live in renderAdvancedSubRows.
function renderSubFlagRows(flags, parentEl, subFieldNames, { cssClass, lockedByGate }) {
  subFieldNames.forEach(fieldName => {
    const f = flags[fieldName];
    if (!f) return;
    const meta = localizeMeta(
      'features',
      fieldName,
      FEATURE_META[fieldName] || { label: fieldName, help: '' }
    );
    const row = document.createElement('div');
    row.className = 'feature-row ' + cssClass + (lockedByGate ? ' dimmed' : '');

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
      `<div class="feature-name" id="label-feature-${escapeHtml(fieldName)}">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(helpWithFacts(meta.help, f))}</div>` +
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

// Shared renderer for non-bool sub-rows that live in ADVANCED_SETTINGS_FIELDS
// but render nested under a feature toggle (code-mode numerics under
// enable_code_mode, extra YAML write keys under enable_yaml_config_editing).
// They save through commitAdvancedEdit rather than saveFeatureFlag, which is
// why renderSubFlagRows (checkbox-only) cannot serve them.
function renderAdvancedSubRows(parentEl, section, cssClass, lockedByGate) {
  const cmRows = (_advancedFields || []).filter(x => x.section === section);
  cmRows.forEach(f => {
    const meta = localizeMeta('advanced', f.field, {});
    const row = document.createElement('div');
    row.className = 'feature-row ' + cssClass + (lockedByGate ? ' dimmed' : '');
    const multiline = f.field === 'extra_yaml_write_keys';
    if (multiline) row.classList.add('list-editor');

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
        lockedNote = `<div class="feature-locked-note">${tHtml(
          'advanced.code_mode_saved_tools_path.addon_locked',
          {},
          'Hardcoded to <code>/data/saved_tools.json</code> in App (add-on) mode and cannot be changed (fixed here so saved tools survive App (add-on) updates).'
        )}</div>`;
      } else if (f.origin === 'env') {
        lockedNote =
          `<div class="feature-locked-note">${envLockedNoteHtml(f.env_var, f.field)}</div>`;
      } else if (!f.value) {
        lockedNote = `<div class="feature-locked-note">${escapeHtml(t(
          'advanced.code_mode_saved_tools_path.blank_warning',
          {},
          'If blank, custom tools are kept in memory only and lost on restart. Set a path on persistent storage to keep them.'
        ))}</div>`;
      }
    } else if (f.origin === 'env') {
      lockedNote =
        `<div class="feature-locked-note">${envLockedNoteHtml(f.env_var, f.field)}</div>`;
    }
    info.innerHTML =
      `<div class="feature-name" id="label-feature-${escapeHtml(f.field)}">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(helpWithFacts(meta.help, f))}</div>` +
      lockedNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    const disabled = !f.editable || lockedByGate;
    let inputEl;
    if (multiline) {
      inputEl = document.createElement('textarea');
      inputEl.rows = 4;
      inputEl.value = String(f.value ?? '').split(',').map(s => s.trim()).filter(Boolean).join('\n');
    } else if (f.type === 'int' || f.type === 'float') {
      inputEl = document.createElement('input');
      inputEl.type = 'number';
      inputEl.value = f.value;
      if (typeof f.min === 'number') inputEl.min = f.off_value ?? f.min;
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
    inputEl.setAttribute('aria-labelledby', 'label-feature-' + f.field);
    inputEl.addEventListener('change', () => {
      let v;
      if (f.type === 'int') v = parseInt(inputEl.value, 10);
      else if (f.type === 'float') v = parseFloat(inputEl.value);
      else if (multiline) v = inputEl.value.split(/[,\r\n]+/).map(s => s.trim()).filter(Boolean).join(',');
      else v = inputEl.value;
      commitAdvancedEdit(f.field, v);
    });
    control.appendChild(inputEl);

    row.appendChild(info);
    row.appendChild(control);
    parentEl.appendChild(row);
  });
}

// Three-valued, because "did not succeed" and "did not happen" are not the
// same thing and the toggle handlers have to tell them apart:
//
//   parsed body (truthy) - the server confirmed the save. Carries `applied`
//                          (what it persisted) and `restart_required`.
//   false                - the server answered and refused. The previous
//                          value is confirmed; reverting the UI is correct.
//   null                 - AMBIGUOUS. No usable answer came back, so the
//                          write may or may not have landed. Callers must
//                          re-read before asserting which value the server
//                          holds.
//
// `!saved` still covers both failure cases for callers that only care
// whether it succeeded.
async function saveFeatureFlag(fieldName, value) {
  updateStatus(t('status.saving_server_setting', {}, 'Saving server setting...'));
  let resp;
  try {
    resp = await fetch('./api/settings/features', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({flags: {[fieldName]: value}}),
    });
  } catch (e) {
    updateStatus(t('errors.save_failed_detail', {message: e.message}, 'Save failed: ' + e.message), false, true);
    // null, not false: the request was never answered, so we do NOT know
    // whether it landed. `!saved` still holds for callers that only care
    // about "didn't succeed"; the toggle handlers check for null and
    // re-read before asserting which value the server has. An `!resp.ok`
    // below stays `false` — there the server answered, so the previous
    // value is confirmed and a re-read would only turn a known state
    // into an unknown one.
    return null;
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
    let msg = t('errors.save_failed_http', {status: resp.status}, `Save failed (HTTP ${resp.status})`);
    if (data?.error?.message) msg = t('errors.save_failed_detail', {message: data.error.message}, 'Save failed: ' + data.error.message);
    updateStatus(msg, false, true);
    // Not every error response means the write didn't happen. In app
    // (add-on) mode the save goes through the supervisor, and
    // _supervisor.py catches EVERY httpx.HTTPError - read timeouts
    // included - into _SupervisorOptionsError.transport(), which hardcodes
    // 502 and maps to CONNECTION_FAILED. A read timeout means the POST
    // reached the supervisor and the RESPONSE was lost, so
    // /addons/self/options can be written while we answer 502. A bodyless
    // 502/504 is ingress doing the same in front of us: `data` stays null,
    // because the `resp.ok` guard above only supplies the fallback body on
    // success. Both are ambiguous in exactly the way a rejected fetch is.
    //
    // File mode stays unambiguous - every failure path in
    // _write_feature_flag_overrides_file returns before or from the atomic
    // write - and a supervisor validation refusal keeps its real status
    // code, so it lands as `false` and still reverts.
    if (data?.error?.code === 'CONNECTION_FAILED') return null;
    if (!data && (resp.status === 502 || resp.status === 504)) return null;
    return false;
  }
  // Unified restart flow — save persists the change but does NOT fire
  // the addon restart. The user picks when to restart by clicking the
  // global Restart Add-on button in the cross-tab restart-required
  // banner. Same UX as the Tools tab. In standalone modes the restart
  // button is hidden (no supervisor to drive it) but the banner still
  // surfaces "restart required" as guidance.
  updateStatus(t('status.saved_restart', {}, 'Saved. Restart required.'), true);
  if (data?.restart_required) {
    markRestartRequired();
  }
  // The parsed body (truthy, so `if (!ok)` call sites are unaffected), not
  // a bare true: both save paths echo `applied` — the server stating what
  // it persisted — which a caller can fall back on when the follow-up
  // re-read fails.
  return data || {};
}

// The value the server echoed back for one field of a saveFeatureFlag
// response, or undefined when the body carried no usable echo (a 200 with
// a truncated body, or a future response shape without `applied`).
function appliedFlagValue(saved, fieldName) {
  const applied = saved && saved.applied;
  const value = applied ? applied[fieldName] : undefined;
  return typeof value === 'boolean' ? value : undefined;
}

