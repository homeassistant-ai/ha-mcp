// The server embeds exactly one merged locale catalog in the page. English
// values are already merged as fallback, so an incomplete translation remains
// usable and adding a language requires only a new locales/<code>.json file.
const I18N_PAYLOAD = (() => {
  const el = document.getElementById('ha-mcp-i18n');
  if (!el) return {locale: 'en', dir: 'ltr', messages: {}, tool_groups: {}, tools: {}, languages: []};
  try {
    return JSON.parse(el.textContent || '{}');
  } catch (err) {
    console.warn('[ha-mcp] invalid embedded translation catalog', err);
    return {locale: 'en', dir: 'ltr', messages: {}, tool_groups: {}, tools: {}, languages: []};
  }
})();
const LOCALE_COOKIE = 'ha_mcp_locale';

function t(key, params = {}, fallback = key) {
  let value = Object.prototype.hasOwnProperty.call(I18N_PAYLOAD.messages || {}, key)
    ? I18N_PAYLOAD.messages[key]
    : fallback;
  Object.keys(params).forEach(name => {
    value = value.replaceAll(`{${name}}`, String(params[name]));
  });
  return value;
}

// Translation for HTML contexts (innerHTML sinks). Catalog strings are data,
// not markup: the whole value is HTML-escaped first, then only the allowlist
// of inline formatting tags the catalogs use (<code>, <strong>, and the
// internal tab links) is restored, so a translated string can never
// contribute any other markup. Placeholders are substituted AFTER escaping:
// params are trusted HTML fragments built by the caller (escape any dynamic
// text inside them with escapeHtml at the call site).
function tHtml(key, params = {}, fallback = key) {
  const raw = Object.prototype.hasOwnProperty.call(I18N_PAYLOAD.messages || {}, key)
    ? I18N_PAYLOAD.messages[key]
    : fallback;
  let value = escapeHtml(raw)
    .replace(/&lt;(\/?)(code|strong)&gt;/g, '<$1$2>')
    .replace(/&lt;a href=&quot;#&quot; data-panel-link=&quot;([a-z][a-z-]*)&quot;&gt;/g, '<a href="#" data-panel-link="$1">')
    .replace(/&lt;\/a&gt;/g, '</a>');
  Object.keys(params).forEach(name => {
    value = value.replaceAll(`{${name}}`, String(params[name]));
  });
  return value;
}

function localizeMeta(section, field, meta) {
  return {
    ...meta,
    label: t(`${section}.${field}.label`, {}, meta.label || field),
    help: t(`${section}.${field}.help`, {}, meta.help || ''),
  };
}

function _localeNumber(value) {
  try {
    return Number(value).toLocaleString(I18N_PAYLOAD.locale);
  } catch (_err) {
    return String(value);
  }
}

// A row's help text plus the facts its payload carries (range, off value,
// restart), rendered here so the catalog sentences never repeat them.
function helpWithFacts(help, row) {
  const parts = help ? [help] : [];
  if (typeof row.min === 'number' && typeof row.max === 'number') {
    const range = {min: _localeNumber(row.min), max: _localeNumber(row.max)};
    parts.push(typeof row.off_value === 'number'
      ? t('common.range_or_value', {...range, value: _localeNumber(row.off_value)}, 'Range {min}–{max}, or {value}.')
      : t('common.range', range, 'Range {min}–{max}.'));
  }
  if (row.restart_required) parts.push(t('common.restart_required', {}, 'Restart required.'));
  return parts.join(' ');
}

function localizedToolGroup(group) {
  return (I18N_PAYLOAD.tool_groups || {})[group] || group;
}

function _placeholderNames(value) {
  return Array.from(new Set(Array.from(
    String(value || '').matchAll(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g),
    match => match[1]
  ))).sort();
}

function _translatedToolField(tool, field, expectedSource, fallback = expectedSource) {
  const translated = ((I18N_PAYLOAD.tools || {})[tool.name] || {})[field];
  if (!translated) return fallback;
  const sourceFields = _placeholderNames(expectedSource);
  const translatedFields = _placeholderNames(translated);
  if (sourceFields.join('\0') !== translatedFields.join('\0')) {
    console.warn(
      `[ha-mcp] ignoring ${field} translation for ${tool.name}: placeholder mismatch`,
      {source: sourceFields, translated: translatedFields}
    );
    return fallback;
  }
  return translated;
}

function localizedToolCopy(tool, description) {
  const sourceTitle = tool.title || tool.name;
  return {
    title: _translatedToolField(tool, 'title', sourceTitle),
    // Validate the translated description against the DISPLAYED condensed
    // text (the same first-line summary shown in the row), not the full
    // runtime docstring: deeper docstring lines carry API-path tokens like
    // {slug}/{type} that never appear in the summary, and comparing against
    // them would wrongly drop an otherwise-valid translation. Mirrors the
    // title branch, which compares against the displayed sourceTitle.
    description: _translatedToolField(tool, 'description', description),
  };
}

function applyStaticTranslations(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n, {}, el.textContent);
  });
  root.querySelectorAll('[data-i18n-html]').forEach(el => {
    // Rewrite only from the catalog: the server-rendered markup already is
    // the English copy, and routing it back through the escape+allowlist
    // pipeline would depend on attribute serialization being round-trip
    // stable, which it is not guaranteed to be.
    const key = el.dataset.i18nHtml;
    if (Object.prototype.hasOwnProperty.call(I18N_PAYLOAD.messages || {}, key)) {
      el.innerHTML = tHtml(key);
    }
  });
  for (const attribute of ['placeholder', 'title', 'aria-label']) {
    const dataName = `i18n${attribute.split('-').map(part => part[0].toUpperCase() + part.slice(1)).join('')}`;
    root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(el => {
      const key = el.dataset[dataName];
      el.setAttribute(attribute, t(key, {}, el.getAttribute(attribute) || ''));
    });
  }
}

function _readLocaleCookie() {
  const prefix = `${LOCALE_COOKIE}=`;
  const entry = document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith(prefix));
  if (!entry) return '';
  try {
    return decodeURIComponent(entry.slice(prefix.length));
  } catch (err) {
    console.warn('[ha-mcp] ignoring malformed locale cookie', err);
    return '';
  }
}

function _writeLocaleCookie(locale) {
  if (locale === 'auto') {
    document.cookie = `${LOCALE_COOKIE}=; Max-Age=0; Path=/; SameSite=Lax`;
    return;
  }
  document.cookie = `${LOCALE_COOKIE}=${encodeURIComponent(locale)}; Max-Age=31536000; Path=/; SameSite=Lax`;
}

function initializeLanguageControl() {
  document.documentElement.lang = I18N_PAYLOAD.locale || 'en';
  document.documentElement.dir = I18N_PAYLOAD.dir || 'ltr';
  applyStaticTranslations();

  const select = document.getElementById('languageToggle');
  if (!select) return;
  select.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = 'auto';
  auto.textContent = t('language.auto', {}, 'Auto');
  select.appendChild(auto);
  (I18N_PAYLOAD.languages || []).forEach(language => {
    const option = document.createElement('option');
    option.value = language.code;
    option.textContent = language.native_name;
    select.appendChild(option);
  });
  const saved = _readLocaleCookie();
  select.value = Array.from(select.options).some(option => option.value === saved)
    ? saved
    : 'auto';
  select.addEventListener('change', () => {
    _writeLocaleCookie(select.value);
    window.location.reload();
  });
}

initializeLanguageControl();

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
  el.textContent = t(
    'errors.javascript',
    {message: e.message, where},
    `JS error: ${e.message} @ ${where}`
  );
});
window.addEventListener('unhandledrejection', (e) => {
  const el = document.getElementById('status');
  if (!el) return;
  setStatusAlert(el, true);
  const message = e.reason && e.reason.message ? e.reason.message : String(e.reason);
  el.textContent = t('errors.async', {message}, `Async error: ${message}`);
});

let toolData = [];
let toolStates = {};
// Map of tool name → "disabled" | "pinned" for env-var-pinned tools.
// Populated from data.env_pinned in loadTools(); read by render() to
// lock rows and show the env-var name banner.
let toolEnvPinned = {};
// Tools locked enabled while strict best-practices mode
// (enable_mandatory_bps + enable_strict_mandatory_bps) is on (#1886).
// Populated from data.bps_locked_tools in loadTools(); rendered like
// mandatory tools plus a note naming the toggle to turn off first.
// Server-side saves reject the conflict too — this lock is the
// courteous UI half.
let bpsLockedTools = new Set();
let ignoredDisabledTools = new Set();
// Conversation-agent LLM API exposure (#1745). toolLlm mirrors the
// server-computed EFFECTIVE value per tool (user override, else the
// deny-by-default for beta/dev/restart tools); toolLlmOverrides holds
// only the user-set overrides and is what saveConfig persists — tools
// never flipped keep tracking their defaults across releases.
let toolLlm = {};
let toolLlmOverrides = {};
// True only on the in-process custom-component (embedded) server, which
// registers the LLM API exposure surface. On the add-on / Docker / standalone
// server nothing consumes it, so the per-tool "LLM API" toggle is hidden
// rather than shown as a no-op. Set from data.llm_api_available (see
// settings_ui/__init__.py).
let llmApiAvailable = false;
let saveTimer = null;
let openGroups = new Set();

// Per-tool "security gated" toggle state mirrors the bare (condition-free)
// rules in policy.rules from /api/policy/config. Under the default
// require-approval list a bare rule gates its tool; under an allow list
// (rule_effect 'allow') it approves it, so there the toggle reads "gated"
// exactly when neither the tool nor `*` has a bare rule. The Tools tab uses isToolGated()
// to render the third toggle alongside enabled/pinned.
// `enabled` is tri-state: true/false from the addon-config flag, or
// null when the features fetch failed — downstream branches need to
// distinguish "definitively off" from "couldn't determine" so they
// don't false-confidently tell the user the feature is off.
const policyState = {
  enabled: false,
  enabledKnown: false,
  bareRuleTools: new Set(),
  // rule_effect each surface was last rendered under: the Tools-tab gate
  // toggles (loadPolicyState) and the Policies-tab cards and selector
  // (renderPolicyCards) load separately. null until that surface loaded.
  // policyPut refuses a write from a surface whose mode is stale or was
  // never read, so a mode switched elsewhere is neither written back nor
  // used to save a rule meant for the other mode.
  toolsEffect: null,
  cardsEffect: null,
  // enable_security_policy_tool — registers ha_manage_security_policy, the
  // MCP tool that can rewrite these rules. Independent of `enabled`: it
  // governs who may edit the policy, not whether it is enforced.
  manageToolEnabled: false,
  manageToolKnown: false,
  // The raw /api/settings/features entries behind the two switches
  // (origin / editable / env_var), or null when the fetch failed. An
  // env-pinned flag reports editable:false and every save is rejected, so
  // the switch must lock and explain — same as the generated feature rows.
  masterFlag: null,
  manageToolFlag: null,
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
  // Raw flag entry, as with the policy switches above: READ_ONLY_MODE can
  // be env-pinned (Docker / standalone), and a switch that looks live
  // while every save 4xxs is worse than one that says why it is locked.
  flag: null,
};

// Mixed read/write tools that stay enabled in Read Only Mode (their
// write operations are blocked server-side at call time). Populated
// from data.read_only_exempt in loadTools().
let READ_ONLY_EXEMPT = new Set();

// Whether the features payload actually reported one flag. "Known" is per
// FLAG, not per response: /api/settings/features emits `value` for every
// flag it knows, so a missing entry means this server build (or an overlay
// in front of it) did not report that flag — not that it is off. Treating
// absence as false renders the switch off-and-editable, and the next save
// would overwrite an enabled server value with it.
function flagReported(flag) {
  return !!flag && flag.value !== undefined && flag.value !== null;
}

// Reset every flag-backed switch to "unknown". Shared by loadPolicyState's
// two failure paths so a transient 503 and a network error leave identical
// state — the switches then render indeterminate + disabled, not "off".
function _clearFlagSwitchState() {
  policyState.enabled = false;
  policyState.enabledKnown = false;
  policyState.masterFlag = null;
  policyState.manageToolEnabled = false;
  policyState.manageToolKnown = false;
  policyState.manageToolFlag = null;
  readOnlyState.enabled = false;
  readOnlyState.enabledKnown = false;
  readOnlyState.flag = null;
}

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
      // The payload also carries is_addon, which envLockedNoteHtml needs
      // for its add-on-vs-standalone wording. Landing straight on the
      // Policies tab never runs loadFeatureFlags(), so pick it up here too.
      if (typeof fdata.is_addon === 'boolean') IS_ADDON_MODE = fdata.is_addon;
      const flags = fdata.flags || {};
      const flag = flags['enable_tool_security_policies'];
      policyState.enabled = !!(flag && flag.value);
      policyState.enabledKnown = flagReported(flag);
      policyState.masterFlag = flag || null;
      const toolFlag = flags['enable_security_policy_tool'];
      policyState.manageToolEnabled = !!(toolFlag && toolFlag.value);
      policyState.manageToolKnown = flagReported(toolFlag);
      policyState.manageToolFlag = toolFlag || null;
      const roFlag = flags['read_only_mode'];
      readOnlyState.enabled = !!(roFlag && roFlag.value);
      readOnlyState.enabledKnown = flagReported(roFlag);
      readOnlyState.flag = roFlag || null;
    } else {
      // Log it: this handler is now the evidence the ambiguous-save path
      // branches on, so "why did a security switch go unknown?" has to be
      // answerable from the console.
      console.warn(
        '[ha-mcp] /api/settings/features returned HTTP ' + fresp.status +
        '; flag switches shown as unknown');
      _clearFlagSwitchState();
    }
  } catch (e) {
    // A malformed payload lands here as a TypeError, indistinguishable from
    // a network drop unless we say which we saw.
    console.warn('[ha-mcp] failed to read feature flags', e);
    _clearFlagSwitchState();
  }
  try {
    const r = await fetch('./api/policy/config');
    if (!r.ok) {
      // Transient failure — keep the previously-loaded gate state rather
      // than clobbering it to empty, so a blip doesn't make the Tools tab
      // falsely claim nothing is gated.
      console.warn('[ha-mcp] /api/policy/config returned HTTP ' + r.status + '; keeping prior gated-tools state');
      return;
    }
    const p = await r.json();
    // The Tools-tab gate toggle reflects the BARE unconditional rule only
    // (no predicates); conditional rules are managed in the Policies tab.
    policyState.bareRuleTools = new Set(
      (p.rules || [])
        .filter(rule => !rule.when || rule.when.length === 0)
        .map(rule => rule.tool_name)
    );
    policyState.toolsEffect = effectOf(p);
  } catch (e) {
    // Policy endpoint unavailable (sidecar stub) or network blip — keep the
    // prior gate state rather than resetting it to empty. On first load it
    // is already an empty Set, so the default still holds.
    console.warn('[ha-mcp] failed to load policy config', e);
  }
}

// Wrap PUT /api/policy/config so every caller gets identical handling of
// the 409 (optimistic-concurrency) and other failure paths. The full
// policy round-trips through every caller, so the version GET'd here
// goes back out in the PUT body and the server can reject stale writes.
function effectOf(policy) {
  return (policy && policy.rule_effect) || 'require_approval';
}

async function policyPut(policy, opLabel, surfaceEffect, expectedEffect = effectOf(policy)) {
  // A rule reads the opposite way in the other mode, so never write on the
  // strength of a mode the user was not shown.
  if (surfaceEffect === null) {
    throw new Error(t(
      'policies.errors.effect_unknown', {operation: opLabel},
      opLabel + ' failed: this page could not read what a matching rule does. Reload the page, then re-apply your changes.'
    ));
  }
  if (surfaceEffect !== expectedEffect) {
    throw new Error(t(
      'policies.errors.effect_changed', {operation: opLabel},
      opLabel + ' failed: the policy changed what a matching rule does since this page loaded it. Reload the page, then re-apply your changes.'
    ));
  }
  const w = await fetch('./api/policy/config', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(policy),
  });
  if (w.status === 409) {
    throw new Error(t(
      'policies.errors.conflict',
      {operation: opLabel},
      opLabel + ' failed: policy was modified in another tab/session. Reload the page, then re-apply your changes.'
    ));
  }
  if (!w.ok) {
    const detail = w.status + ' ' + await w.text();
    throw new Error(t('common.operation_failed', {operation: opLabel, detail}, opLabel + ' failed: ' + detail));
  }
  return await w.json();
}

// Where a NEW tool's rules belong in the rule list: before the first
// wildcard rule, else at the end. In a require-approval list
// find_matching_rule() is first-match (it supplies remember_minutes and the
// matched_rule shown to the user), so a tool-specific rule appended after a
// `*` rule would never be the match. An allow list does not depend on order.
function wildcardInsertIndex(rules) {
  const idx = rules.findIndex(rule => rule.tool_name === '*');
  return idx === -1 ? rules.length : idx;
}

// Mirrors bare_rule_gates() in policy/model.py.
function bareRuleGates(bare, effect, toolName) {
  if (effect === 'allow') return !bare.has(toolName) && !bare.has('*');
  return bare.has(toolName);
}

function isToolGated(toolName) {
  return bareRuleGates(policyState.bareRuleTools, policyState.toolsEffect, toolName);
}

async function syncPolicyRule(toolName, gated) {
  const r = await fetch('./api/policy/config');
  if (!r.ok) throw new Error(t('policies.errors.load', {status: r.status}, 'Could not load policy: ' + r.status));
  const policy = await r.json();
  policy.rules = policy.rules || [];
  // The gate toggle manages ONLY the bare, unconditional rule (empty `when`)
  // for this tool; predicate-bearing rules authored in the policy editor are
  // preserved. A conditional rule must not be mistaken for the gate (enabling
  // would silently no-op) nor wiped on un-gate. Under an allow list the bare
  // rule approves the tool, so gating it means removing that rule.
  const isBareGate = rule =>
    rule.tool_name === toolName && (!rule.when || rule.when.length === 0);
  if (gated !== (policy.rule_effect === 'allow')) {
    if (!policy.rules.some(isBareGate)) {
      policy.rules.splice(wildcardInsertIndex(policy.rules), 0,
        {tool_name: toolName, when: [], remember_minutes: 0});
    }
  } else {
    policy.rules = policy.rules.filter(rule => !isBareGate(rule));
  }
  // An allow list's bare `*` rule approves every tool, so gating one would
  // save a no-op; refuse instead (set_tool(gated=) refuses the same way).
  // A mode change since the page loaded is left to policyPut, whose message
  // (reload) is the one that applies then.
  const bare = new Set(policy.rules
    .filter(rule => !rule.when || rule.when.length === 0)
    .map(rule => rule.tool_name));
  if (effectOf(policy) === policyState.toolsEffect
      && bareRuleGates(bare, effectOf(policy), toolName) !== gated) {
    throw new Error(t(
      'policies.errors.wildcard_rule',
      {},
      'An unconditional "*" rule in this allow list approves every tool, so this switch cannot gate one. Remove that rule on the Tool Security Policies tab first.'
    ));
  }
  await policyPut(policy, t('policies.operations.sync_gated', {}, 'Sync gated toggle'), policyState.toolsEffect);
  // The tool's card on the policy tab still holds its rules from before this
  // write; a remember save queued from it would put the gate back. Mark it
  // stale so that save is dropped (the tab reloads the cards on its next
  // visit).
  if (toolName in policyRuleEdits) policyRuleEdits[toolName] = null;
}

