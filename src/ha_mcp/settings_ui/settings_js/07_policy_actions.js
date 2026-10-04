// Each card write reads the whole policy and writes it back, so the cards
// write one at a time. Two at once can fail with a version conflict, and
// for one card the later write can carry its rule from before the earlier
// write's change and undo it. A remember save sent after "Remove from
// policy" would also put the tool's rules back.
let policyWriteInFlight = null;
function policyWriteOnce(write) {
  return (policyWriteInFlight = (async () => {
    try { return await write(); } finally { policyWriteInFlight = null; }
  })());
}

async function savePolicyRule(toolName, ruleObj) {
  // Under an allow list a card left without conditions approves every call
  // to the tool: the loosening direction, so it is never saved silently.
  if (policyState.cardsEffect === 'allow' && !(ruleObj.conditions || []).length && !confirm(t(
    'policies.card.confirm_approve_all', {tool: toolName},
    'Approve every call to "' + toolName + '" without asking?'
  ))) {
    throw new Error(t('policies.card.not_saved', {}, 'Not saved.'));
  }
  const r = await fetch('./api/policy/config');
  if (!r.ok) throw new Error(t('policies.errors.load', {status: r.status}, 'Could not load policy: ' + r.status));
  const policy = await r.json();
  policy.rules = policy.rules || [];
  // Expand the card's conditions into ONE rule each (they OR at evaluation —
  // the tool gates if ANY condition matches; a condition's own predicates AND
  // together as sub-parameters). No conditions = a single bare rule that
  // matches every call (gates it, or approves it in an allow list). Replaces
  // all of this tool's rules.
  const remember = ruleObj.remember_minutes || 0;
  const conditions = ruleObj.conditions || [];
  const remembers = ruleObj.remembers || [];
  // Preserve each condition's own lifetime unless the user actually touched
  // the card's remember input (rememberDirty) — an unrelated predicate edit
  // must not silently rewrite heterogeneous per-condition remember values.
  const rememberFor = (i) => (ruleObj.rememberDirty
    ? remember
    : (remembers[i] !== undefined ? remembers[i] : remember));
  const expanded = conditions.length === 0
    ? [{tool_name: toolName, when: [], remember_minutes: remember}]
    : conditions.map((preds, i) => ({tool_name: toolName, when: preds, remember_minutes: rememberFor(i)}));
  // Replace the tool's rules IN PLACE (at the position of its first rule)
  // rather than appending at the end: rule order is behaviorally significant
  // — find_matching_rule() takes the FIRST match's remember_minutes, so
  // moving a tool-specific rule behind a wildcard rule would silently switch
  // matching calls to the wildcard's approval lifetime.
  const others = [];
  let insertAt = -1;
  policy.rules.forEach(rule => {
    if (rule.tool_name === toolName) {
      if (insertAt === -1) insertAt = others.length;
    } else {
      others.push(rule);
    }
  });
  // A tool with no existing rule inserts before the first wildcard rule (not
  // at the end): first-match would otherwise resolve the tool's calls to the
  // wildcard's remember_minutes, never the new rule's.
  if (insertAt === -1) insertAt = wildcardInsertIndex(others);
  policy.rules = others.slice(0, insertAt).concat(expanded, others.slice(insertAt));
  await policyPut(policy, t('policies.operations.save_rule', {}, 'Save rule'), policyState.cardsEffect);
}

async function removePolicyRule(toolName) {
  // The card's "Remove from policy" button removes ALL of the tool's rules
  // (every condition), unlike the Tools-tab gate toggle which manages only
  // the bare unconditional rule via syncPolicyRule.
  const r = await fetch('./api/policy/config');
  if (!r.ok) throw new Error(t('policies.errors.load', {status: r.status}, 'Could not load policy: ' + r.status));
  const policy = await r.json();
  policy.rules = (policy.rules || []).filter(rule => rule.tool_name !== toolName);
  await policyPut(policy, t('policies.operations.save_rule', {}, 'Remove rule'), policyState.cardsEffect);
}

async function saveGlobalSettings() {
  const statusEl = document.getElementById('policy-global-save-status');
  setStatusAlert(statusEl, false);
  statusEl.textContent = t('status.saving', {}, 'Saving...');
  let resp;
  try {
    resp = await fetch('./api/policy/config');
  } catch (e) {
    setStatusAlert(statusEl, true);
    const message = t('errors.network', {message: e.message}, 'Network error: ' + e.message);
    statusEl.textContent = message;
    showToast(message, {isError: true});
    return;
  }
  if (!resp.ok) {
    setStatusAlert(statusEl, true);
    const message = t('errors.load_failed', {status: resp.status}, 'Load failed: ' + resp.status);
    statusEl.textContent = message;
    showToast(message, {isError: true});
    return;
  }
  const policy = await resp.json();
  const serverEffect = effectOf(policy);
  const switchedEffect = document.getElementById('policy-rule-effect').value;
  const effectChanged = switchedEffect !== serverEffect;
  const effectName = effect => t('policies.global.rule_effect.' + effect, {}, effect);
  if (effectChanged && serverEffect === policyState.cardsEffect && !confirm(t(
    'policies.global.rule_effect.confirm',
    {from: effectName(serverEffect), to: effectName(switchedEffect)},
    'Switch from "' + effectName(serverEffect) + '" to "' + effectName(switchedEffect) + '"?\n\nEvery rule below then has the opposite effect: calls it gated will run without approval, or calls it approved will need approval. Calls no rule matches flip the other way, and in an allow list conditions match more strictly.'
  ))) {
    document.getElementById('policy-rule-effect').value = serverEffect;
    statusEl.textContent = '';
    return;
  }
  policy.rule_effect = switchedEffect;
  policy.wait_seconds = parseInt(document.getElementById('policy-wait-seconds').value, 10);
  policy.approval_ttl_minutes = parseInt(document.getElementById('policy-ttl-minutes').value, 10);
  policy.event_decisions_enabled = document.getElementById('policy-event-decisions-toggle').checked;
  try {
    await policyPut(policy, t('policies.operations.save_global', {}, 'Save global settings'), policyState.cardsEffect, serverEffect);
    statusEl.textContent = t('status.saved', {}, 'Saved.');
    showToast(t('status.saved', {}, 'Saved.'));
    if (effectChanged) {
      // The rule cards and the Tools-tab toggles both read differently now;
      // policyLoadConfig() reloads both.
      await policyLoadConfig();
    }
  } catch (e) {
    setStatusAlert(statusEl, true);
    statusEl.textContent = e.message;
    showToast(e.message, {isError: true});
  }
}

// policyLoadConfig() does not await policyRefreshPinStatus(), so an older
// answer can land after a newer one; only the latest request may write.
let pinStatusSeq = 0;

// The PIN itself never reaches the page: this endpoint reports only that
// one exists, so a reload cannot put it back in front of anyone.
async function policyRefreshPinStatus() {
  const statusEl = document.getElementById('policy-pin-status');
  const toggle = document.getElementById('policy-event-decisions-toggle');
  if (!statusEl || !toggle) return;
  const mySeq = ++pinStatusSeq;
  let status;
  try {
    const r = await fetch('./api/policy/decision-pin');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    status = await r.json();
  } catch (e) {
    if (mySeq !== pinStatusSeq) return;
    // Say the state is unknown rather than implying "no PIN" — the switch
    // stays as the server last reported it, and saveGlobalSettings is what
    // the server validates anyway.
    statusEl.textContent = t('policies.global.pin.unknown', {}, 'Could not read whether a PIN is set.');
    return;
  }
  if (mySeq !== pinStatusSeq) return;
  // Three states, not two: a stored record the server cannot verify
  // against is neither "a PIN is set" nor "no PIN" — nobody can type a
  // PIN that matches it, and saying so is the only way the user knows to
  // set a new one rather than to keep retrying the old one.
  if (status.set) {
    statusEl.textContent = t('policies.global.pin.is_set', {}, 'A PIN is set.');
  } else if (status.invalid) {
    statusEl.textContent = t(
      'policies.global.pin.invalid',
      {},
      'The stored PIN cannot be read and matches nothing. Set a new one.'
    );
  } else {
    statusEl.textContent = t('policies.global.pin.not_set', {}, 'No PIN set. Set one to allow decisions over the event bus.');
  }
  // Without a PIN the server refuses the combination, so don't offer it.
  toggle.disabled = !status.set;
}

async function policySetPin() {
  const input = document.getElementById('policy-decision-pin');
  const label = t('policies.operations.set_pin', {}, 'Set PIN');
  try {
    const r = await fetch('./api/policy/decision-pin', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pin: input.value}),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    input.value = '';
    showToast(t('policies.global.pin.saved', {}, 'PIN saved.'));
    // A locked switch cannot be edited, so overwriting it loses nothing typed
    // since it locked. It can read off after a PIN removal whose toggle save
    // failed while the stored setting stayed on; show the stored value now
    // that the switch is usable. A failed read leaves it until the next
    // policyLoadConfig().
    const toggle = document.getElementById('policy-event-decisions-toggle');
    if (toggle && toggle.disabled) {
      try {
        const cfg = await fetch('./api/policy/config');
        if (cfg.ok) toggle.checked = !!(await cfg.json()).event_decisions_enabled;
        else console.warn('[ha-mcp] /api/policy/config returned HTTP ' + cfg.status + '; event-decisions switch may be stale');
      } catch (err) {
        console.warn('[ha-mcp] failed to re-read the event-decisions setting', err);
      }
    }
  } catch (e) {
    showToast(
      t('common.operation_failed', {operation: label, detail: e.message}, label + ' failed: ' + e.message),
      {isError: true}
    );
  }
  await policyRefreshPinStatus();
}

async function policyClearPin() {
  if (!confirm(t(
    'policies.global.pin.confirm_clear',
    {},
    'Remove the approval PIN? Deciding over the event bus switches off with it; the pending list in this tab is unaffected.'
  ))) return;
  const label = t('policies.operations.clear_pin', {}, 'Remove PIN');
  try {
    const r = await fetch('./api/policy/decision-pin', {method: 'DELETE'});
    const body = await r.json().catch(() => ({}));
    // Unchecked whenever the PIN is actually gone — including the error
    // the server sends when it removed the PIN but could not persist the
    // toggle with it (pin_removed on a 500). The flag below reports only
    // whether the PERSISTED toggle was on, so a box ticked but not yet
    // saved would otherwise survive the removal: left checked, then
    // disabled by the status refresh, and the next save submits the one
    // combination the server refuses.
    if (r.ok || body.pin_removed) {
      const toggleEl = document.getElementById('policy-event-decisions-toggle');
      if (toggleEl) toggleEl.checked = false;
    }
    if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
    if (body.event_decisions_disabled) {
      showToast(t(
        'policies.global.pin.cleared_and_disabled',
        {},
        'PIN removed, and deciding over the event bus switched off with it.'
      ));
    } else {
      showToast(t('policies.global.pin.cleared', {}, 'PIN removed.'));
    }
  } catch (e) {
    showToast(
      t('common.operation_failed', {operation: label, detail: e.message}, label + ' failed: ' + e.message),
      {isError: true}
    );
  }
  await policyRefreshPinStatus();
}

async function policyLoadPending() {
  const list = document.getElementById('policy-pending-list');
  let resp;
  try {
    resp = await fetch('./api/policy/pending');
  } catch (e) {
    // Surface the failure inline — silent return leaves the pending
    // list visibly frozen with no signal that polling broke.
    list.innerHTML = '<em style="color:var(--text-secondary)">' + escapeHtml(t(
      'policies.pending.offline',
      {message: e.message},
      'Lost contact with server (' + e.message + '). Retrying.'
    )) + '</em>';
    return;
  }
  if (resp.status === 503) {
    // 503 has three causes. Only confidently say "feature is off"
    // when /api/settings/features actually told us so; if we couldn't
    // determine the flag (network drop, server down), fall back to
    // the server's 503 message rather than misleadingly claiming the
    // user disabled the feature.
    if (policyState.enabledKnown && !policyState.enabled) {
      list.innerHTML = '<em>' + escapeHtml(t(
        'policies.pending.disabled',
        {},
        'Tool Security Policies is turned off. Toggle it on (top of this tab or in Server Settings) and restart the App (add-on) to enable gating.'
      )) + '</em>';
    } else {
      // Feature is on (or unknown) but the queue isn't reachable —
      // sidecar mode, startup ImportError, or transient outage.
      let msg = t(
        'policies.pending.unavailable',
        {},
        'Live approvals unavailable. Check the App (add-on) log for ImportError / RuntimeError details.'
      );
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
    list.textContent = t('policies.pending.empty', {}, 'No pending approvals.');
    return;
  }
  list.innerHTML = pending.map(p => (
    '<div data-pending-token="' + escapeHtml(p.token) + '" style="border:1px solid var(--border); padding:10px; margin:6px 0; border-radius:8px; background:var(--surface)">' +
    '<strong>' + escapeHtml(p.tool_name) + '</strong>' +
    '<pre style="white-space:pre-wrap; background:var(--bg); padding:8px; margin:6px 0; border-radius:6px; font-size:0.8rem">' +
    escapeHtml(JSON.stringify(p.args, null, 2)) + '</pre>' +
    '<small style="color:var(--text-secondary)">' + escapeHtml(t('policies.pending.expires', {time: p.expires_at}, 'Expires: ' + p.expires_at)) + '</small><br>' +
    '<div style="margin-top:8px; display:flex; gap:8px">' +
    '<button class="restart-btn" data-policy-token="' + escapeHtml(p.token) + '" data-policy-action="approve">' + escapeHtml(t('actions.approve', {}, 'Approve')) + '</button>' +
    '<button class="danger-btn" data-policy-token="' + escapeHtml(p.token) + '" data-policy-action="deny">' + escapeHtml(t('actions.deny', {}, 'Deny')) + '</button>' +
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
    alert(t('errors.network', {message: e.message}, 'Network error: ' + e.message));
    return;
  }
  if (!resp.ok) {
    // Only a body that actually parsed can carry a server message, so an
    // unparsable one leaves serverError empty rather than standing in with the
    // status line: a stand-in reads as a server message to both branches that
    // consult it — the 503 default and the generic detail — and shadows the
    // translated line meant to cover exactly this case.
    let body = {};
    let serverError = '';
    try {
      body = (await resp.json()) || {};
      if (typeof body.error === 'string') serverError = body.error;
    } catch (_) { /* not JSON — no server message to show */ }
    if (resp.status === 409 && body.current_decision) {
      // current_decision is a backend enum, not display text: interpolating it
      // raw leaves an English word inside a translated sentence.
      const decision = t(
        'policies.pending.decision.' + body.current_decision,
        {},
        body.current_decision
      );
      alert(t(
        'policies.pending.already_decided',
        {decision},
        'This approval was already ' + decision + ', possibly by another tab or session.'
      ));
    } else if (resp.status === 404) {
      alert(t('policies.pending.invalid_token', {}, 'This approval token is no longer valid (already consumed or expired).'));
    } else if (resp.status === 503) {
      // Same three causes as policyLoadPending's 503, answered the same way.
      // The 503 body is a fixed English paragraph, so folding it into
      // 'policies.pending.action_failed' spliced five English lines into the
      // middle of a translated clause.
      if (policyState.enabledKnown && !policyState.enabled) {
        alert(t(
          'policies.pending.disabled',
          {},
          'Tool Security Policies is turned off. Toggle it on (top of this tab or in Server Settings) and restart the App (add-on) to enable gating.'
        ));
      } else {
        // Feature is on (or we could not determine the flag). The server's
        // message names which of the remaining causes applied, so it stands
        // on its own rather than inside a sentence. Without one — a 503 from
        // an ingress or reverse proxy in front of us — the translated line is
        // all the user gets, which is what policyLoadPending does too.
        alert(serverError || t(
          'policies.pending.unavailable',
          {},
          'Live approvals unavailable. Check the App (add-on) log for ImportError / RuntimeError details.'
        ));
      }
    } else {
      const detail = serverError || resp.statusText || 'HTTP ' + resp.status;
      alert(t('policies.pending.action_failed', {detail}, 'Approval action failed: ' + detail));
    }
  }
  policyLoadPending();
}

// Shared failed-save handling for the three feature-flag toggles (policy
// master, policy-editing tool, Read Only Mode). Before this existed the
// block was ~40 lines duplicated three times, so review items 1 and 2 each
// cost three edits and a fourth copy would have been easy to miss.
//
// `saved === false` is a refusal: the server answered, so the previous value
// is confirmed and reverting is correct. `null` is ambiguous — the write may
// have landed with only its response lost — so re-read first and revert only
// when the readback actually shows the old value.
//
// `spec` is {readState, revertKey, revertText}: readState() returns
// {value, known} for this toggle's slice of policyState/readOnlyState.
//
// Repaints are NOT per-toggle. The re-read below is loadPolicyState(),
// which refreshes — or, through _clearFlagSwitchState(), clears — the
// state slices of ALL THREE switches. Repainting only the toggle being
// saved left the other two painted from state that no longer existed:
// a failed re-read after a Read Only Mode save cleared both policy
// slices while their switches stayed editable at the stale value with
// #policyUnknownNotice hidden, and vice versa for #roUnknownNotice.
function _repaintFlagSwitches() {
  paintPolicyGlobalToggles();
  syncReadOnlyToggle();
  render();
}

async function handleFailedFlagSave(checkbox, previous, saved, spec) {
  if (saved === false) {
    // No re-read happened, so the other switches' state is untouched and
    // repainting them is a no-op — one shape for both branches.
    checkbox.checked = previous;
    _repaintFlagSwitches();
    updateStatus(t(spec.revertKey, {}, spec.revertText), false, true);
    return;
  }
  await loadPolicyState();
  const state = spec.readState();
  let msg, ok = false;
  if (state.known && state.value === previous) {
    checkbox.checked = previous;
    msg = t(spec.revertKey, {}, spec.revertText);
  } else if (state.known) {
    // The write landed; only the response was lost. Leave the switch where
    // the server actually is, and arm the restart banner — these flags gate
    // tool registration at startup and the success toast auto-dismisses.
    checkbox.checked = state.value;
    msg = t('status.saved_restart', {}, 'Saved. Restart required.');
    ok = true;
    markRestartRequired();
  } else {
    // Neither value confirmed. Say the thing that is actually true rather
    // than reusing the global-unknown copy, which talks about "the two
    // switches below" and reads wrong in a snackbar.
    msg = t('errors.save_outcome_unknown', {},
      'Could not confirm the change. It may or may not have been applied — ' +
      'reload to see what the server has.');
  }
  // Paint before the status line so the repaint can't clobber it.
  _repaintFlagSwitches();
  updateStatus(msg, ok, !ok);
}

document.getElementById('policy-save-global-btn').addEventListener('click', saveGlobalSettings);
document.getElementById('policy-set-pin-btn').addEventListener('click', policySetPin);
document.getElementById('policy-clear-pin-btn').addEventListener('click', policyClearPin);

// Master toggle on this tab mirrors the Server Settings checkbox.
// Persist via the same /api/settings/features endpoint so a save here
// shows up in Server Settings (and the addon's config.yaml) on reload.
document.getElementById('policy-master-toggle').addEventListener('change', async (e) => {
  const previous = !e.target.checked;  // user just flipped; previous is the OPPOSITE.
  const saved = await saveFeatureFlag('enable_tool_security_policies', e.target.checked);
  if (!saved) {
    await handleFailedFlagSave(e.target, previous, saved, {
      readState: () => ({value: policyState.enabled, known: policyState.enabledKnown}),
      revertKey: 'policies.errors.master_save',
      revertText:
        'Tool Security Policies change did not save. The server still has the previous value',
    });
    return;
  }
  // Re-read the truth from the server. If that read can't confirm, fall
  // back to the value the save echoed — still the server telling us what
  // it wrote. Only when neither is available does the switch go to the
  // unknown treatment; reverting to the pre-flip value would assert a
  // state the server no longer has.
  await loadPolicyState();
  if (!policyState.enabledKnown) {
    const applied = appliedFlagValue(saved, 'enable_tool_security_policies');
    if (applied !== undefined) {
      policyState.enabled = applied;
      policyState.enabledKnown = true;
    }
  }
  paintPolicyGlobalToggles();
  // render() reads policyState for the per-tool security-gate treatment, so
  // the rows go stale unless it runs after the flag settles.
  render();
});

// Policy-editing tool toggle (enable_security_policy_tool) — same
// save-then-verify flow as the master above. The tool only appears in or
// disappears from the MCP catalog on the next restart, which
// saveFeatureFlag's restart-required banner already tells the user.
document.getElementById('policy-manage-tool-toggle').addEventListener('change', async (e) => {
  const previous = !e.target.checked;  // user just flipped; previous is the OPPOSITE.
  const saved = await saveFeatureFlag('enable_security_policy_tool', e.target.checked);
  if (!saved) {
    await handleFailedFlagSave(e.target, previous, saved, {
      readState: () => ({value: policyState.manageToolEnabled, known: policyState.manageToolKnown}),
      revertKey: 'policies.errors.manage_tool_save',
      revertText:
        'Policy-editing tool change did not save. The server still has the previous value',
    });
    return;
  }
  // Same confirm-then-fall-back order as the master toggle above.
  await loadPolicyState();
  if (!policyState.manageToolKnown) {
    const applied = appliedFlagValue(saved, 'enable_security_policy_tool');
    if (applied !== undefined) {
      policyState.manageToolEnabled = applied;
      policyState.manageToolKnown = true;
    }
  }
  paintPolicyGlobalToggles();
  // render() reads policyState for the per-tool security-gate treatment, so
  // the rows go stale unless it runs after the flag settles.
  render();
});

// Read Only Mode toggle (Tools tab, above the search box) — same flag
// plumbing as the policy master toggle: persist via
// /api/settings/features, re-read server truth, revert on failure.
document.getElementById('read-only-mode-toggle').addEventListener('change', async (e) => {
  const previous = !e.target.checked;  // user just flipped; previous is the OPPOSITE.
  const saved = await saveFeatureFlag('read_only_mode', e.target.checked);
  if (!saved) {
    await handleFailedFlagSave(e.target, previous, saved, {
      readState: () => ({value: readOnlyState.enabled, known: readOnlyState.enabledKnown}),
      revertKey: 'tools.read_only.save_failed',
      revertText:
        'Read Only Mode change did not save. The server still has the previous value',
    });
    return;
  }
  // Re-read the truth from the server; if that read couldn't confirm,
  // fall back to the value the save echoed rather than reverting to a
  // pre-flip state the server no longer has. Neither available means the
  // switch goes unknown (indeterminate + the #roUnknownNotice).
  await loadPolicyState();
  if (!readOnlyState.enabledKnown) {
    const applied = appliedFlagValue(saved, 'read_only_mode');
    if (applied !== undefined) {
      readOnlyState.enabled = applied;
      readOnlyState.enabledKnown = true;
    }
  }
  syncReadOnlyToggle();
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

