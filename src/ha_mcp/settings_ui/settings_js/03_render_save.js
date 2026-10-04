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
  applyFlagToggle({
    toggleId: 'read-only-mode-toggle',
    noteId: 'read-only-locked',
    fieldName: 'read_only_mode',
    value: readOnlyState.enabled,
    known: readOnlyState.enabledKnown,
    flag: readOnlyState.flag,
  });
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
  const tr = t;
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
    const groupLabel = localizedToolGroup(tag);
    const group = document.createElement('div');
    group.className = 'group';

    // Per-group toggle state: enabled if ANY non-mandatory/non-gated/non-env-pinned
    // tool is enabled. Tools forced off by Read Only Mode are excluded so the
    // group master switch can't fight the mode.
    const toggleable = tools.filter(t =>
      !MANDATORY.includes(t.name) && !bpsLockedTools.has(t.name) &&
      !t.disabled_by && !toolEnvPinned[t.name] &&
      !isReadOnlyForcedOff(t));
    const anyEnabled = toggleable.some(t => getState(t.name) !== 'disabled');
    const groupEnabled = tools.filter(t => {
      if (isReadOnlyForcedOff(t)) return false;
      if (toolEnvPinned[t.name]) return toolEnvPinned[t.name] !== 'disabled';
      const s = getState(t.name);
      return MANDATORY.includes(t.name) || bpsLockedTools.has(t.name) ||
        (!t.disabled_by && s !== 'disabled');
    }).length;

    // Master-switch checked state. Normally it mirrors "any toggleable tool
    // enabled" — it is a bulk control over the tools the user can actually
    // flip. But when a group has NO toggleable tools (all mandatory /
    // env-pinned / feature-gated), the switch is disabled and purely reflects
    // status: show it ON only when the group is FULLY enabled (every tool on),
    // matching the "N/N enabled" count. anyEnabled can't be reused here — it is
    // always false over an empty toggleable set. So an all-mandatory group
    // (e.g. Search & Discovery) reads as ON, while a partially-enabled locked
    // group (e.g. a mandatory tool beside an env-pinned-off one) reads as OFF
    // rather than contradicting its own "1/N enabled" count.
    const masterChecked =
      toggleable.length === 0 ? groupEnabled === tools.length : anyEnabled;

    const header = document.createElement('div');
    header.className = 'group-header';
    header.innerHTML = `<div class="group-header-left">` +
      `<span class="group-chevron">&#9654;</span>` +
      `<span class="group-name">${escapeHtml(groupLabel)}</span>` +
      `<span class="group-count">${escapeHtml(tr(
        'tools.group_count',
        {enabled: groupEnabled, total: tools.length},
        `${groupEnabled}/${tools.length} enabled`
      ))}</span>` +
      `</div>` +
      `<label class="switch group-master" title="${escapeHtml(tr(
        'tools.group_toggle_title',
        {},
        'Enable/disable all tools in this group'
      ))}">` +
        `<input type="checkbox" name="tool-group:${escapeHtml(tag)}" ${masterChecked ? 'checked' : ''} ${toggleable.length === 0 ? 'disabled' : ''}>` +
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
      // BPS-locked tools render with the mandatory lock plus a note
      // naming the toggle to turn off first (#1886).
      const isBpsLocked = bpsLockedTools.has(t.name);
      const isMandatory = MANDATORY.includes(t.name) || isBpsLocked;
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
      // The security gate is a policy RULE keyed by tool name, so it can be
      // authored for a tool that is not registered yet. Feature-gated rows
      // therefore keep this switch live (when policies are on): otherwise
      // the first enable+restart would expose the tool ungated, which is
      // exactly the window ha_manage_security_policy must not have.
      const canGate = policyState.enabled && (isEnabled || isFeatureGated);

      const sourceDesc = (t.description || '').split('\n')[0].slice(0, 120);
      const toolCopy = localizedToolCopy(t, sourceDesc);
      const title = toolCopy.title;
      const desc = toolCopy.description;

      const div = document.createElement('div');
      div.className = isEnvPinned ? 'tool env-pinned' : 'tool';
      div.dataset.name = t.name.toLowerCase();
      // Search the same localized title the user sees while retaining the
      // source title as a secondary alias for bilingual/admin workflows.
      div.dataset.title = [title, t.title].filter(Boolean).join(' ').toLowerCase();

      let badges = '';
      if (isMandatory) badges += `<span class="badge mandatory">${escapeHtml(tr('tools.badges.mandatory', {}, 'mandatory'))}</span>`;
      if (category === 'read') badges += `<span class="badge readonly">${escapeHtml(tr('tools.badges.read_only', {}, 'read-only'))}</span>`;
      else if (category === 'write') badges += `<span class="badge write">${escapeHtml(tr('tools.badges.writes', {}, 'writes'))}</span>`;
      else if (category === 'delete') badges += `<span class="badge destructive">${escapeHtml(tr('tools.badges.deletes', {}, 'deletes'))}</span>`;
      // A missing/unknown category must still render a visible badge — a
      // destructive tool showing no tier badge would understate its risk.
      else badges += `<span class="badge unknown">${escapeHtml(category) || '?'}</span>`;

      // Two flavors of "how to turn this on": beta gates point at the dev
      // App (add-on) config, non-beta ones (the policy-editing tool) at
      // their own settings tab. disabled_by_beta comes from the server so
      // the client never has to know which flags are beta.
      const gatedNote = disabledBy
        ? `<div class="disabled-by-note">${
            t.disabled_by_beta === false
              ? tHtml(
                  'tools.notes.gated_disabled',
                  {setting: `<code>${escapeHtml(disabledBy)}</code>`},
                  'Disabled. Turn on {setting} — the policy-editing tool\'s toggle is on the Tool Security Policies tab — then restart the App (add-on).'
                )
              : tHtml(
                  'tools.notes.beta_disabled',
                  {setting: `<code>${escapeHtml(disabledBy)}</code>`},
                  'Beta. Set {setting} in the dev App (add-on) config or the matching env var (see docs/beta.md).'
                )
          }</div>`
        : '';
      const envPinnedNote = isEnvPinned
        ? `<div class="feature-locked-note">${tHtml(
            'tools.notes.env_pinned',
            {variable: `<code>${escapeHtml(envPinVar)}</code>`},
            'Pinned by {variable}. Unset the environment variable to edit here.'
          )}</div>`
        : '';
      const bpsLockedNote = isBpsLocked
        ? `<div class="feature-locked-note">${escapeHtml(tr(
            'tools.notes.bps_locked',
            {},
            'Locked while "Strict best-practices mode" is on (Server Settings tab) — strict mode publishes its acknowledgment key through this tool. Turn that off first to disable this tool.'
          ))}</div>`
        : '';
      const readOnlyNote = roForcedOff
        ? `<div class="disabled-by-note">${escapeHtml(tr(
            'tools.notes.read_only_off',
            {},
            'Off. Read Only Mode is on; write tools are disabled.'
          ))}</div>`
        : (roExemptActive
          ? `<div class="feature-locked-note">${escapeHtml(tr(
              'tools.notes.read_only_partial',
              {},
              'Read Only Mode: write operations of this tool are blocked; read operations stay available.'
            ))}</div>`
          : '');
      // LLM API exposure column — rendered only on the embedded custom-component
      // server (see llmApiAvailable); dropped elsewhere rather than shown as a
      // no-op. Built here as a fragment, matching the *Note consts above.
      const llmToggleHtml = llmApiAvailable
        ? `<div class="toggle-group ${isEnabled ? '' : 'disabled-toggle'}" ` +
             `title="${escapeHtml(tr('tools.llm_api.help', {}, "Offer this tool to Home Assistant conversation agents through the LLM API. Applies on the agent's next message - no restart. A tool disabled above is unavailable to agents regardless."))}">` +
          `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:llm" data-tool="${escapeHtml(t.name)}" data-field="llm" ` +
            `aria-label="${escapeHtml(tr('tools.aria.llm_api', {title}, `${title} exposed to the conversation-agent LLM API`))}" ` +
            `${(toolLlm[t.name] !== false) ? 'checked' : ''} ${isEnabled ? '' : 'disabled'}>` +
            `<span class="slider"></span></label>` +
          `<span>${escapeHtml(tr('tools.llm_api.label', {}, 'LLM API'))}</span>` +
        `</div>`
        : '';

      div.innerHTML = `<div class="tool-info">` +
        `<div class="tool-name">${escapeHtml(title)}${badges}</div>` +
        `<div class="tool-meta">${escapeHtml(t.name)}</div>` +
        (desc ? `<div class="tool-desc">${escapeHtml(desc)}</div>` : '') +
        gatedNote +
        envPinnedNote +
        bpsLockedNote +
        readOnlyNote +
        `</div>` +
        `<div class="tool-toggles">` +
          `<div class="toggle-group">` +
            `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:enabled" data-tool="${escapeHtml(t.name)}" data-field="enabled" ` +
              `aria-label="${escapeHtml(tr('tools.aria.enabled', {title}, `${title} enabled`))}" ` +
              `${isEnabled ? 'checked' : ''} ${lockEnabled ? 'disabled' : ''}>` +
              `<span class="slider"></span></label>` +
            `<span>${escapeHtml(tr('tools.states.enabled', {}, 'enabled'))}</span>` +
          `</div>` +
          `<div class="toggle-group ${!isEnabled ? 'disabled-toggle' : ''}">` +
            `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:pinned" data-tool="${escapeHtml(t.name)}" data-field="pinned" ` +
              `aria-label="${escapeHtml(tr('tools.aria.pinned', {title}, `${title} pinned`))}" ` +
              `${isPinned ? 'checked' : ''} ${lockPinned ? 'disabled' : ''}>` +
              `<span class="slider"></span></label>` +
            `<span>${escapeHtml(tr('tools.states.pinned', {}, 'pinned'))}</span>` +
          `</div>` +
          `<div class="toggle-group ${canGate ? '' : 'disabled-toggle'}" ` +
               `title="${policyState.enabled ? '' : escapeHtml(tr('tools.security.enable_first', {}, 'Enable Tool Security Policies in App (add-on) config first.'))}">` +
            `<label class="switch"><input type="checkbox" name="tool:${escapeHtml(t.name)}:gated" data-tool="${escapeHtml(t.name)}" data-field="gated" ` +
              `aria-label="${escapeHtml(tr('tools.aria.security_gated', {title}, `${title} security gated`))}" ` +
              `${isToolGated(t.name) ? 'checked' : ''} ` +
              `${canGate ? '' : 'disabled'}>` +
              `<span class="slider"></span></label>` +
            `<span>${escapeHtml(tr('tools.states.security_gated', {}, 'security gated'))}</span>` +
          `</div>` +
          llmToggleHtml +
        `</div>`;

      const inputs = div.querySelectorAll('input[type="checkbox"]');
      inputs.forEach(input => {
        if (input.disabled) return;
        input.addEventListener('change', async (e) => {
          const field = e.target.dataset.field;
          if (field === 'gated') {
            // Optimistic UI: flip local state, sync to server, rollback on failure.
            // Gated lives in policy.rules (not tool_config), so we skip scheduleSave().
            const wasGated = isToolGated(t.name);
            const hadBare = policyState.bareRuleTools.has(t.name);
            const nowGated = e.target.checked;
            const setBare = present => {
              if (present) policyState.bareRuleTools.add(t.name);
              else policyState.bareRuleTools.delete(t.name);
            };
            setBare(nowGated !== (policyState.toolsEffect === 'allow'));
            try {
              await policyWriteOnce(() => syncPolicyRule(t.name, nowGated));
            } catch (err) {
              // Restore the rule set itself: under a bare `*` rule the
              // switch's old position does not say whether this tool had one.
              setBare(hadBare);
              e.target.checked = wasGated;
              alert(tr(
                'policies.errors.update_tool',
                {message: err.message},
                'Failed to update tool security policy: ' + err.message
              ));
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
    `<span>${escapeHtml(tr('tools.summary.total', {count: total}, `${total} total`))}</span>` +
    `<span style="color:var(--success)">${escapeHtml(tr('tools.summary.enabled', {count: enabledCount}, `${enabledCount} enabled`))}</span>` +
    `<span style="color:var(--accent)">${escapeHtml(tr('tools.summary.pinned', {count: pinnedCount}, `${pinnedCount} pinned`))}</span>` +
    `<span style="color:var(--danger)">${escapeHtml(tr('tools.summary.disabled', {count: disabledCount}, `${disabledCount} disabled`))}</span>`;

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
  updateStatus(t('status.unsaved', {}, 'Unsaved changes...'));
  saveTimer = setTimeout(saveConfig, 800);
}

async function saveConfig() {
  updateStatus(t('status.saving', {}, 'Saving...'));
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
    updateStatus(t('errors.save_failed_detail', {message: e.message}, 'Save failed: ' + e.message), false, true);
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
      updateStatus(t('status.saved_restart', {}, 'Saved. Restart required.'), true);
      markRestartRequired();
    } else {
      updateStatus(t(
        'status.saved_llm_api',
        {},
        'Saved. LLM API exposure applies on the next agent message.'
      ), true);
    }
  } else {
    // Surface the server's structured error when present (mirrors
    // saveAdvancedSettings / saveFeatureFlag) instead of a generic
    // "Save failed!" that hides why the write was rejected.
    let msg = t('errors.save_failed', {}, 'Save failed!');
    try {
      const data = await resp.json();
      if (data?.error?.message) msg = t('errors.save_failed_detail', {message: data.error.message}, 'Save failed: ' + data.error.message);
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
    dismiss.setAttribute('aria-label', t('actions.dismiss', {}, 'Dismiss'));
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

