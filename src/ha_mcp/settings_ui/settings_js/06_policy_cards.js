// ===== Tool Security Policies tab =====
// Live approval routes (pending/approve/deny) are only available from
// the main server (in-process ApprovalQueue). The sidecar serves
// config GET/PUT but returns 503 for the live endpoints — the UI
// degrades to "Live approvals unavailable in this mode."
//
// The card UI keeps an in-memory copy of each tool's rules
// (policyRuleEdits[tool_name]). Condition and remember-minutes changes save
// without a separate Save step, through the policyWriteOnce queue:
// savePolicyRule() GETs the current policy, replaces the tool's rules, and
// PUTs, like syncPolicyRule() for the Tools-tab toggle.
let policyRuleEdits = {};

async function syncPolicyGlobalToggles() {
  // The master toggle on this tab is just a UI mirror of the same
  // `enable_tool_security_policies` feature flag the Server Settings
  // tab exposes — the addon-config flag is the single source of truth.
  // We rely on loadPolicyState() to have populated policyState.enabled
  // (it fetches /api/settings/features) so the only work here is to
  // reflect that bit into the checkbox.
  //
  // The policy-editing tool toggle (enable_security_policy_tool) rides
  // the same fetch. It is NOT nested under the master: registering
  // ha_manage_security_policy is independent of whether the rules are
  // enforced, so the row stays usable with the master off.
  await loadPolicyState();
  paintPolicyGlobalToggles();
}

// Paint-only half of the sync above: both switches plus the unknown-state
// notice, from whatever policyState currently holds. Split out so a save
// handler that has just re-read the state can repaint without refetching.
function paintPolicyGlobalToggles() {
  applyFlagToggle({
    toggleId: 'policy-master-toggle',
    noteId: 'policy-master-locked',
    fieldName: 'enable_tool_security_policies',
    value: policyState.enabled,
    known: policyState.enabledKnown,
    flag: policyState.masterFlag,
  });
  applyFlagToggle({
    toggleId: 'policy-manage-tool-toggle',
    noteId: 'policy-manage-tool-locked',
    fieldName: 'enable_security_policy_tool',
    value: policyState.manageToolEnabled,
    known: policyState.manageToolKnown,
    flag: policyState.manageToolFlag,
  });
  // When a flag could not be read — the whole fetch failed, or the payload
  // simply didn't carry that entry — an unchecked-and-editable switch would
  // claim "off" for a server that may well have it on. Surface that
  // uncertainty whenever EITHER switch is unknown (same treatment the
  // Tools-tab read-only notice gets). Function-scope lookup (guarded) so
  // this id need not be a top-level handler binding.
  const notice = document.getElementById('policyUnknownNotice');
  if (notice) {
    notice.classList.toggle(
      'show',
      !policyState.enabledKnown || !policyState.manageToolKnown
    );
  }
}

// Paint one hand-written feature-flag switch — the two on this tab and the
// Tools-tab Read Only Mode one — giving them what renderFeatureRows gives
// the generated Server Settings rows:
//   unknown (the features fetch failed) -> indeterminate + disabled, so
//     the checkbox cannot be read as the server's answer;
//   env-pinned (editable:false) -> disabled with the same locked note,
//     because every save of a pinned flag is rejected server-side;
//   otherwise -> checked from the value, editable.
function applyFlagToggle({toggleId, noteId, fieldName, value, known, flag}) {
  const cb = document.getElementById(toggleId);
  if (!cb) {
    // Part of a static panel template; a missing element means template
    // drift. Warn so the desync is debuggable instead of a silent no-op.
    console.warn('applyFlagToggle: #' + toggleId + ' not found');
    return;
  }
  const locked = known && !!flag && flag.editable === false;
  cb.checked = !!value;
  cb.indeterminate = !known;
  cb.disabled = !known || locked;
  const row = cb.closest('.feature-row');
  if (row) row.classList.toggle('locked', locked);
  const note = document.getElementById(noteId);
  if (!note) {
    console.warn('applyFlagToggle: #' + noteId + ' not found');
    return;
  }
  if (!locked) {
    note.innerHTML = '';
    note.style.display = 'none';
    return;
  }
  // Same two-branch copy renderFeatureRows uses: env vars get the
  // addon-aware "unset it to edit" banner, other non-editable origins
  // their own note. tHtml/escapeHtml keep catalog strings safe in innerHTML.
  note.innerHTML = flag.origin === 'env'
    ? envLockedNoteHtml(flag.env_var, fieldName)
    : escapeHtml(ORIGIN_LOCKED_NOTE[flag.origin] || '');
  note.style.display = '';
}

async function policyLoadConfig() {
  await syncPolicyGlobalToggles();
  const errEl = document.getElementById('policy-load-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  let resp;
  try {
    resp = await fetch('./api/policy/config');
  } catch (e) {
    showPolicyLoadError(t('policies.errors.reach_server', {message: e.message}, 'Could not reach the server: ' + e.message));
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
        detail += t(
          'policies.errors.corrupt_suffix',
          {},
          ' (tool_policy.json appears corrupt; edit or delete it on the App (add-on) /data volume)'
        );
      }
    } catch (_e) { /* keep the HTTP-status fallback */ }
    if (!bodyParsed) {
      // E.g. an HTML error page from a misrouted sidecar — give the
      // operator a hint that the body itself was unparseable, not
      // just the status code.
      detail += t('errors.unparseable_suffix', {}, ' (response body unparseable)');
    }
    showPolicyLoadError(t('policies.errors.load_detail', {detail}, 'Failed to load policy: ' + detail));
    return;
  }
  const p = await resp.json();
  document.getElementById('policy-rule-effect').value = effectOf(p);
  document.getElementById('policy-wait-seconds').value = p.wait_seconds ?? 60;
  document.getElementById('policy-ttl-minutes').value = p.approval_ttl_minutes ?? 5;
  document.getElementById('policy-event-decisions-toggle').checked = !!p.event_decisions_enabled;
  // Whether the switch above may be used at all depends on the PIN, which
  // lives outside the policy document — so it is a second fetch, not a
  // field of the one just read.
  policyRefreshPinStatus();
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
  // The cards (and every card re-rendered in place later) read this.
  policyState.cardsEffect = effectOf(policy);
  const allowList = policyState.cardsEffect === 'allow';
  // Swap the keys, not just the text, so a later applyStaticTranslations()
  // (language switch) keeps the wording of the current mode.
  const titleEl = document.getElementById('policy-rules-title');
  titleEl.dataset.i18n = allowList ? 'policies.rules.title_allow' : 'policies.rules.title';
  titleEl.textContent = allowList
    ? t('policies.rules.title_allow', {}, 'Approved tools')
    : t('policies.rules.title', {}, 'Gated tools');
  emptyEl.dataset.i18nHtml = allowList ? 'policies.rules.empty_allow' : 'policies.rules.empty';
  emptyEl.innerHTML = allowList
    ? tHtml('policies.rules.empty_allow', {}, 'No tools approved, so every tool call needs your approval, except tool search and managing pending approvals. Approve a tool by switching off its gate on the <a href="#" data-panel-link="tools">Tools</a> tab.')
    : tHtml('policies.rules.empty', {}, 'No tools currently security-gated. Enable per-tool gating from the <a href="#" data-panel-link="tools">Tools</a> tab.');
  if (rules.length === 0) {
    emptyEl.style.display = '';
    return;
  }
  emptyEl.style.display = 'none';
  // Each condition is its own rule on disk (OR: the tool gates if ANY rule
  // matches). Collapse all of a tool's rules into ONE card whose "conditions"
  // are the tool's rules — one condition per rule, each being that rule's
  // whole predicate list; the card re-expands to one rule per condition on
  // save (savePolicyRule). Order preserved by first sight.
  const byTool = {};
  const order = [];
  rules.forEach(r => {
    if (!byTool[r.tool_name]) { byTool[r.tool_name] = []; order.push(r.tool_name); }
    byTool[r.tool_name].push(r);
  });
  order.forEach(toolName => {
    const toolRules = byTool[toolName];
    // One card per tool; each RULE is one condition row. A rule's predicates
    // stay together as a unit (a condition with AND-ed sub-parameters), so a
    // card edit round-trips multi-predicate rules intact instead of
    // flattening them.
    const conditions = toolRules.map(r => r.when || []);
    // The card has ONE remember-minutes input; DISPLAY the max across the
    // tool's rules, but keep each condition's own value (remembers[i]) so a
    // save that never touched the input can't silently rewrite heterogeneous
    // per-condition lifetimes (rememberDirty gates which one is persisted).
    const remembers = toolRules.map(r => r.remember_minutes || 0);
    const remember = Math.max(0, ...remembers);
    policyRuleEdits[toolName] = JSON.parse(JSON.stringify(
      {tool_name: toolName, conditions: conditions, remembers: remembers,
       remember_minutes: remember, rememberDirty: false}
    ));
    listEl.appendChild(renderPolicyCard(toolName, policyRuleEdits[toolName]));
  });
}

function displayPredicate(p) {
  if (!p || !p.path) return t('policies.predicate.invalid', {}, '(invalid)');
  if (p.op === 'exists') return p.path + ' ' + t('policies.operators.exists', {}, 'exists');
  const val = (p.value === undefined) ? 'null' : JSON.stringify(p.value);
  return p.path + ' ' + t(`policies.operators.${p.op}`, {}, p.op) + ' ' + val;
}

function renderPolicyCard(toolName, rule) {
  const allowList = policyState.cardsEffect === 'allow';
  const card = document.createElement('div');
  card.className = 'policy-rule-card';
  card.dataset.tool = toolName;
  rule.conditions = rule.conditions || [];
  // A condition = one rule's predicate list. Multiple predicates in one
  // condition AND together (sub-parameters); separate conditions OR. Each
  // row shows its predicates joined by AND and every row after the first
  // leads with OR, so the grouping reads off the card. The form edits one
  // predicate at a time: "edit" targets that predicate, "+ AND" appends one
  // to the row's condition.
  const andJoin = t('policies.card.and_join', {}, ' AND ');
  const displayCondition = (preds) => (preds.length
    ? preds.map(displayPredicate).join(andJoin)
    : (allowList
      ? t('policies.card.always_row_allow', {}, '(always — approves every call to this tool)')
      : t('policies.card.always_row', {}, '(always — gates every call to this tool)')));
  // The predicate text rides along hidden in each button's name, after the
  // visible label, so a screen reader or voice control can tell the
  // buttons of one group apart.
  const predicateName = (p) => '<span class="visually-hidden"> ' + escapeHtml(displayPredicate(p)) + '</span>';
  const predicateParts = (preds, i) => (preds.length
    ? preds.map((p, j) => (
      (j > 0 ? '<span class="policy-and-join">' + escapeHtml(andJoin) + '</span>' : '') +
      '<code>' + escapeHtml(displayPredicate(p)) + '</code>' +
      '<button class="policy-edit-predicate" data-idx="' + i + '" data-pred="' + j + '">' + escapeHtml(t('actions.edit', {}, 'edit')) + predicateName(p) + '</button>' +
      // A lone predicate goes with its condition (the row's ×): emptying a
      // condition would leave a rule that matches every call.
      (preds.length > 1
        ? '<button class="policy-remove-part" data-idx="' + i + '" data-pred="' + j + '">' + escapeHtml(t('actions.remove', {}, 'Remove')) + predicateName(p) + '</button>'
        : '')
    )).join('')
    : '<code>' + escapeHtml(displayCondition(preds)) + '</code>');
  // An "equals X" next to a predicate on the same argument that rules X out
  // (equals Y, does not equal X, is NOT one of [..X..], is one of [..no X..])
  // no single value can satisfy. Under an allow list it never matches; in a
  // require-approval list it fires only for a list or comma-separated value
  // that holds both, which "contains" says directly. So the editor refuses
  // it either way (see saveConditions). Strings only (the evaluator compares
  // in Python, where true equals 1), case-insensitive like a require-approval
  // list, and wildcard paths skipped. A leading "args." is optional, as in
  // the evaluator.
  const deadConditionPath = (preds) => {
    const lower = (v) => (typeof v === 'string' ? v.toLowerCase() : null);
    const byPath = new Map();
    for (const p of preds) {
      if (!p || !p.path || p.path.includes('*')) continue;
      const key = p.path.replace(/^args\./, '');
      byPath.set(key, (byPath.get(key) || []).concat([p]));
    }
    for (const group of byPath.values()) {
      for (const e of group) {
        if (e.op !== 'eq' || typeof e.value !== 'string') continue;
        const x = e.value.toLowerCase();
        const strings = (p) => Array.isArray(p.value) && p.value.every(v => typeof v === 'string');
        const listed = (p) => strings(p) && p.value.some(v => v.toLowerCase() === x);
        if (group.some(p => p !== e && (
          (p.op === 'eq' && typeof p.value === 'string' && lower(p.value) !== x)
          || (p.op === 'neq' && lower(p.value) === x)
          || (p.op === 'not_in' && listed(p))
          || (p.op === 'in' && strings(p) && !listed(p))
        ))) return e.path;
      }
    }
    return null;
  };
  const neverMatchesText = (path) => t(
    'policies.card.never_matches', {path: path},
    'No single value of ' + path + ' satisfies all of its predicates in this condition. For a list argument, use "contains" for each value it must hold.'
  );
  // An alert: screen readers announce one inserted on re-render, not a note.
  // Shown for such a condition saved outside the editor (tool_policy.json or
  // the policy tool); the editor refuses to save a changed one.
  const conflictWarning = (preds) => {
    const path = deadConditionPath(preds);
    return path === null ? '' : '<span class="policy-condition-warning" role="alert">' +
      escapeHtml(neverMatchesText(path)) + '</span>';
  };
  const predicateRows = rule.conditions.map((preds, i) => (
    '<li class="policy-predicate-row" data-idx="' + i + '">' +
      (i > 0 ? '<span class="policy-or-join">' + escapeHtml(t('policies.card.or_join', {}, 'OR')) + '</span>' : '') +
      predicateParts(preds, i) +
      // Not on an "(always)" row: that would turn an unconditional rule into
      // a conditional one. "+ Add condition" plus removing the always row
      // says that change explicitly.
      (preds.length
        ? '<button class="policy-and-predicate" data-idx="' + i + '" title="' + escapeHtml(allowList
          ? t('policies.card.add_and_title_allow', {}, 'Add a sub-condition that must also match. This condition then approves fewer calls.')
          : t('policies.card.add_and_title', {}, 'Add a sub-condition that must also match. This condition then matches fewer calls.')) + '">' +
          escapeHtml(t('policies.card.add_and', {}, '+ AND')) + '</button>'
        : '') +
      '<button class="policy-remove-predicate" data-idx="' + i + '" aria-label="' + escapeHtml(t('policies.card.remove_condition', {}, 'Remove condition')) + '">×</button>' +
      conflictWarning(preds) +
    '</li>'
  )).join('');
  const emptyHint = rule.conditions.length === 0
    ? '<li class="policy-predicate-row"><em style="color:var(--text-secondary);font-size:0.8rem">' +
      escapeHtml(t('policies.card.no_conditions', {}, '(no conditions, rule matches every call to this tool)')) + '</em></li>'
    : '';
  card.innerHTML =
    '<div class="policy-rule-header">' +
      '<strong>' + escapeHtml(toolName) + '</strong>' +
      '<button class="policy-rule-remove" title="' + escapeHtml(t('policies.card.remove_title', {}, 'Remove from policy')) + '">×</button>' +
    '</div>' +
    '<div class="policy-rule-predicates">' +
      '<label class="features-sub" style="display:block;margin-bottom:4px">' +
        escapeHtml(allowList
          ? t('policies.card.conditions_intro_allow', {}, 'Approve without asking when ANY condition matches (no conditions = always approve):')
          : t('policies.card.conditions_intro', {}, 'Require approval when ANY of these conditions matches (no conditions = always require approval):')) +
      '</label>' +
      '<ul class="policy-predicate-list">' + emptyHint + predicateRows + '</ul>' +
      '<button class="policy-add-predicate">' + escapeHtml(t('policies.card.add_condition', {}, '+ Add condition')) + '</button>' +
      '<div class="policy-predicate-form" style="display:none;">' +
        '<div class="policy-predicate-form-hint" style="display:none;"></div>' +
        '<div class="policy-form-row">' +
          '<label class="policy-form-label">' + escapeHtml(t('policies.card.argument', {}, 'Argument:')) + '</label>' +
          '<select name="policy:predicate-path" class="policy-predicate-path-select">' +
            '<option value="">' + escapeHtml(t('status.loading_parentheses', {}, '(loading...)')) + '</option>' +
          '</select>' +
          '<input type="text" name="policy:predicate-path-custom" class="policy-predicate-path-custom" ' +
            'placeholder="' + escapeHtml(t('policies.card.argument_placeholder', {}, 'e.g. args.color_temp')) + '" style="display:none">' +
        '</div>' +
        '<div class="policy-form-row">' +
          '<label class="policy-form-label">' + escapeHtml(t('policies.card.match_when', {}, 'Match when:')) + '</label>' +
          '<select name="policy:predicate-op" class="policy-predicate-op">' +
            '<option value="exists">' + escapeHtml(t('policies.operators.exists_long', {}, 'is present (any value)')) + '</option>' +
            '<option value="eq">' + escapeHtml(t('policies.operators.eq', {}, 'equals')) + '</option>' +
            '<option value="neq">' + escapeHtml(t('policies.operators.neq', {}, 'does NOT equal')) + '</option>' +
            '<option value="in">' + escapeHtml(t('policies.operators.in', {}, 'is one of')) + '</option>' +
            '<option value="not_in">' + escapeHtml(t('policies.operators.not_in', {}, 'is NOT one of')) + '</option>' +
            '<option value="contains">' + escapeHtml(t('policies.operators.contains', {}, 'contains')) + '</option>' +
            '<option value="regex">' + escapeHtml(t('policies.operators.regex', {}, 'matches regex')) + '</option>' +
            '<option value="gt">' + escapeHtml(t('policies.operators.gt', {}, 'is greater than')) + '</option>' +
            '<option value="lt">' + escapeHtml(t('policies.operators.lt', {}, 'is less than')) + '</option>' +
          '</select>' +
        '</div>' +
        '<div class="policy-form-row policy-value-row">' +
          '<label class="policy-form-label">' + escapeHtml(t('policies.card.value', {}, 'Value:')) + '</label>' +
          '<span class="policy-predicate-value-slot"></span>' +
        '</div>' +
        '<div class="policy-form-row">' +
          '<button class="policy-predicate-form-save">' + escapeHtml(t('policies.card.save_condition', {}, 'Save condition')) + '</button>' +
          '<button class="policy-predicate-form-cancel">' + escapeHtml(t('actions.cancel', {}, 'Cancel')) + '</button>' +
        '</div>' +
        '<div class="policy-predicate-form-error" style="display:none;"></div>' +
      '</div>' +
    '</div>' +
    // An approving rule never raises an approval, so it has nothing to
    // remember; the stored value is kept but not offered.
    '<div class="policy-rule-lifetime"' + (allowList ? ' style="display:none"' : '') + '>' +
      '<label>' + escapeHtml(t('policies.card.remember_for', {}, 'Remember approval for:')) +
        '<input type="number" name="policy:remember-minutes" min="0" max="1440" class="policy-remember-minutes" ' +
          'value="' + (rule.remember_minutes || 0) + '">' +
        escapeHtml(t('policies.card.minutes_single', {}, 'minutes (0 = single-shot)')) +
      '</label>' +
    '</div>' +
    '<span class="policy-save-status" style="font-size:0.78rem;color:var(--text-secondary)"></span>';

  // Saves the card's rules; there is no separate Save button. Returns
  // whether the save landed so callers skip re-rendering a card that no
  // longer reflects the server. Callers run it through policyWriteOnce, so
  // saves never overlap. The status goes to the card on the page, which a
  // re-render may have replaced since this closure was made.
  const autoSave = async (ruleToSave = rule) => {
    const shown = Array.from(document.querySelectorAll('.policy-rule-card'))
      .find(c => c.dataset.tool === toolName) || card;
    const status = shown.querySelector('.policy-save-status');
    status.textContent = t('status.saving', {}, 'Saving…');
    try {
      await savePolicyRule(toolName, ruleToSave);
      status.textContent = t('status.saved', {}, 'Saved.');
      return true;
    } catch (err) {
      // A failed save must be LOUD and must not leave the card displaying a
      // condition the server never persisted — a phantom SECURITY rule the
      // user would trust (the #1990 failure shape). The tiny status text is
      // missable, so toast like every other save surface, then resync every
      // card from the server's actual policy.
      const message = t('errors.save_failed_detail', {message: err.message}, 'Save failed: ' + err.message);
      status.textContent = message;
      showToast(message, {isError: true});
      try {
        await policyLoadConfig();
      } catch (_e) {
        // Reload failed too (e.g. network down) — the toast already fired.
      }
      return false;
    }
  };

  // Re-render the card in place after a condition-list mutation so the
  // rows reflect the new in-memory rule object.
  const rerenderCard = () => {
    const replacement = renderPolicyCard(toolName, rule);
    card.replaceWith(replacement);
  };

  // A queued write whose card was rebuilt in the meantime (a reload, a
  // failed save's resync, or a Tools-tab gate change for this tool, see
  // syncPolicyRule) carries a rule the page no longer shows, and saving it
  // would put the old rules back. It is dropped, and said so, unless the
  // tool has left the policy, which is what the user just asked for.
  const staleCard = () => {
    if (policyRuleEdits[toolName] === rule) return false;
    if (!(toolName in policyRuleEdits)) return true;
    showToast(t('policies.card.stale_not_saved', {}, 'The policy was reloaded before this change was saved. Make the change again.'), {isError: true});
    return true;
  };

  // Condition edits save a changed copy and adopt it (re-rendering) only
  // once it lands, inside the write (policyWriteOnce), so the next write
  // sends it. A condition click while any policy write is pending is refused
  // with a toast: it was built on a rule that pending write may replace,
  // and sent after it would undo that write's change. `loosens` marks the
  // changes that can approve more calls under an allow list (removing one
  // predicate, editing one, adding an OR-ed condition): there they are
  // never saved without asking. Adding an AND predicate narrows a condition,
  // and removing a whole condition narrows the card, except removing the
  // last one, which leaves an approve-every-call rule that savePolicyRule
  // asks about. Only changed conditions are checked for never matching, so
  // a card holding such conditions can still be fixed one at a time.
  const saveConditions = async (conditions, remembers, loosens) => {
    if (policyWritesPending) return showToast(t('policies.card.save_pending', {}, 'A change is still being saved or the policy is reloading. Try again in a moment.'));
    for (const preds of conditions.filter(c => !rule.conditions.includes(c))) {
      const dead = deadConditionPath(preds);
      if (dead !== null) return showToast(neverMatchesText(dead), {isError: true});
    }
    if (loosens && allowList && !confirm(t(
      'policies.card.confirm_loosen', {tool: toolName},
      'This change can approve calls to "' + toolName + '" that needed your approval before. Save it?'
    ))) return;
    await policyWriteOnce(async () => {
      if (staleCard()) return;
      if (await autoSave({...rule, conditions, remembers})) { Object.assign(rule, {conditions, remembers}); rerenderCard(); }
    });
  };

  card.querySelector('.policy-rule-remove').addEventListener('click', async () => {
    if (!confirm(t('policies.card.confirm_remove', {tool: toolName}, 'Remove "' + toolName + '" from the security policy?'))) return;
    await policyWriteOnce(async () => { try {
      await removePolicyRule(toolName);
      delete policyRuleEdits[toolName];
      card.remove();
      // Refresh card list + empty state from server (also refreshes
      // Tools-tab gated state on next visit via loadPolicyState).
      await policyLoadConfig();
    } catch (err) {
      alert(t('policies.errors.remove_rule', {message: err.message}, 'Failed to remove rule: ' + err.message));
    } });
  });

  // remember-minutes is a number input; debounce so typing "30" doesn't fire
  // three saves (3, 30, arrow keys). The save queues behind any pending
  // write and goes out unless the cards were rebuilt from the server first
  // (staleCard), which a re-render after this card's own save is not: that
  // keeps the same rule object.
  let rmDebounce = null;
  card.querySelector('.policy-remember-minutes').addEventListener('input', (e) => {
    rule.remember_minutes = parseInt(e.target.value, 10) || 0;
    // Only an explicit touch of this input rewrites every condition's
    // lifetime on save; otherwise per-condition values are preserved.
    rule.rememberDirty = true;
    if (rmDebounce) clearTimeout(rmDebounce);
    rmDebounce = setTimeout(() => {
      policyWriteOnce(async () => { if (!staleCard()) await autoSave(); });
    }, 500);
  });

  const formEl = card.querySelector('.policy-predicate-form');
  const opEl = formEl.querySelector('.policy-predicate-op');
  const pathSelectEl = formEl.querySelector('.policy-predicate-path-select');
  const pathCustomEl = formEl.querySelector('.policy-predicate-path-custom');
  const valueSlotEl = formEl.querySelector('.policy-predicate-value-slot');
  const errorEl = formEl.querySelector('.policy-predicate-form-error');
  const hintEl = formEl.querySelector('.policy-predicate-form-hint');
  // What the form writes on save: a new condition (condIdx -1), a predicate
  // appended to condition condIdx (predIdx -1), or predicate predIdx of it.
  let condIdx = -1;
  let predIdx = -1;
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
      'title="' + escapeHtml(t('policies.editor.any_argument_title', {}, 'Match against every argument of the call. Combine with equals/is one of to gate on any argument having a given value.')) + '">' +
      escapeHtml(t('policies.editor.any_argument', {}, '(any argument)')) + '</option>';
    for (const p of paths) {
      const tip = p.description ? ' title="' + escapeHtml(p.description) + '"' : '';
      html += '<option value="' + escapeHtml(p.path) + '"' + tip + '>' +
        escapeHtml(p.label) +
        (p.required ? ' *' : '') +
        (p.type ? ' (' + escapeHtml(p.type) + ')' : '') +
        '</option>';
    }
    html += '<option value="' + FREE_TEXT_OPT + '">' + escapeHtml(t('policies.editor.other_path', {}, '(other, type a path)')) + '</option>';
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
        lastValueSourceError = t(
          'policies.editor.value_source_http',
          {status: r.status},
          'value-source fetch failed (HTTP ' + r.status + '); falling back to free-text'
        );
        return null;
      }
      const data = await r.json();
      const values = Array.isArray(data.values) ? data.values : [];
      valueChoiceCache[sourceKey] = values;
      lastValueSourceError = null;
      return values;
    } catch (e) {
      lastValueSourceError = t(
        'policies.editor.value_source_error',
        {message: e.message},
        'value-source fetch failed (' + e.message + '); falling back to free-text'
      );
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
      return t('policies.editor.hint.exists', {}, 'Leave blank. This op gates on the argument being present at all, regardless of value.');
    }
    if (op === 'in' || op === 'not_in') {
      return t('policies.editor.hint.list', {}, 'Pick one or more values, or type a JSON list. Leave blank to gate on any value.');
    }
    if (op === 'regex') {
      return t('policies.editor.hint.regex', {}, 'A regular expression to match the argument against.');
    }
    if (op === 'contains') {
      return t('policies.editor.hint.contains', {}, 'A substring (for strings) or item (for lists). Leave blank to gate on any value.');
    }
    if (op === 'gt' || op === 'lt') {
      return t('policies.editor.hint.number', {}, 'A number to compare against.');
    }
    return t('policies.editor.hint.equals', {}, 'The value the argument must equal. Leave blank to gate on any value.');
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
        escapeHtml(t('policies.editor.loading_choices', {}, 'Loading choices…')) + '</em>';
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
      html += '<option value="">' + escapeHtml(t('policies.editor.pick_value', {}, '(pick a value)')) + '</option>';
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
      placeholder = t('policies.editor.placeholder.blank', {}, 'usually left blank');
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
    // The save handler has checked that the control exists.
    const ctrl = valueSlotEl.querySelector('.policy-predicate-value-control');
    if (ctrl.tagName === 'SELECT') {
      if (ctrl.multiple) {
        const picked = Array.from(ctrl.selectedOptions).map(o => o.value);
        if (picked.length === 0) {
          if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
          return {ok: false, error: t('policies.editor.validation.pick_one_or_more', {}, 'pick at least one value')};
        }
        return {ok: true, value: picked};
      }
      if (!ctrl.value) {
        if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
        return {ok: false, error: t('policies.editor.validation.pick_value', {}, 'pick a value')};
      }
      return {ok: true, value: ctrl.value};
    }
    const raw = ctrl.value.trim();
    if (!raw) {
      if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
      return {ok: false, error: t('policies.editor.validation.value_required', {operator: op}, 'value is required for op=' + op)};
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
          return {ok: false, error: t('policies.editor.validation.empty_list', {operator: op}, 'empty list for op=' + op)};
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
        lastValueSourceError = t(
          'policies.editor.schema_http',
          {status: r.status},
          'tool-schema fetch failed (HTTP ' + r.status + '); falling back to free-text'
        );
      }
    } catch (e) {
      toolSchema = {paths: [], value_sources: {}};
      lastValueSourceError = t(
        'policies.editor.schema_error',
        {message: e.message},
        'tool-schema fetch failed (' + e.message + '); falling back to free-text'
      );
    }
    return toolSchema;
  };

  opEl.addEventListener('change', () => renderValueControl(undefined));
  pathSelectEl.addEventListener('change', () => {
    pathCustomEl.style.display = (pathSelectEl.value === FREE_TEXT_OPT) ? '' : 'none';
    renderValueControl(undefined);
  });
  pathCustomEl.addEventListener('input', () => renderValueControl(undefined));

  // Two openings can overlap while the first tool-schema fetch is pending;
  // only the latest may fill the form, or it shows one predicate while Save
  // targets another.
  let openSeq = 0;
  const openForm = async (cond, pred) => {
    const mySeq = ++openSeq;
    condIdx = cond;
    predIdx = pred;
    errorEl.style.display = 'none';
    errorEl.textContent = '';
    // Name the condition's other predicates, so the form shows which group
    // it edits or extends.
    const others = cond >= 0
      ? rule.conditions[cond].filter((_p, j) => j !== pred)
      : [];
    if (others.length) {
      hintEl.textContent = t('policies.card.and_hint',
        {condition: displayCondition(others)},
        'Must also match: ' + displayCondition(others));
      hintEl.style.display = '';
    } else {
      hintEl.textContent = '';
      hintEl.style.display = 'none';
    }
    formEl.style.display = '';
    await fetchToolSchema();
    if (mySeq !== openSeq) return;
    if (pred >= 0) {
      const p = rule.conditions[cond][pred];
      opEl.value = p.op || 'eq';
      populatePathSelect(p.path || '');
      await renderValueControl(p.value);
    } else {
      opEl.value = 'eq';
      populatePathSelect('');
      await renderValueControl(undefined);
    }
  };

  card.querySelector('.policy-add-predicate').addEventListener('click', () => openForm(-1, -1));

  card.querySelectorAll('.policy-edit-predicate').forEach(btn => {
    btn.addEventListener('click', () => openForm(
      parseInt(btn.dataset.idx, 10), parseInt(btn.dataset.pred, 10)));
  });

  card.querySelectorAll('.policy-and-predicate').forEach(btn => {
    btn.addEventListener('click', () => openForm(parseInt(btn.dataset.idx, 10), -1));
  });

  card.querySelectorAll('.policy-remove-part').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx, 10);
      const pred = parseInt(btn.dataset.pred, 10);
      await saveConditions(rule.conditions.map((preds, i) => (
        i === idx ? preds.filter((_p, j) => j !== pred) : preds)), rule.remembers, true);
    });
  });

  card.querySelectorAll('.policy-remove-predicate').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx, 10);
      await saveConditions(rule.conditions.filter((_c, i) => i !== idx),
        rule.remembers && rule.remembers.filter((_m, i) => i !== idx), false);
    });
  });

  formEl.querySelector('.policy-predicate-form-cancel').addEventListener('click', () => {
    formEl.style.display = 'none';
    condIdx = -1;
    predIdx = -1;
  });

  formEl.querySelector('.policy-predicate-form-save').addEventListener('click', async () => {
    // No value control yet means the form is still loading: the argument
    // list may hold only its "(loading...)" placeholder, and a blank value
    // here would make Save turn the predicate into "exists".
    if (!valueSlotEl.querySelector('.policy-predicate-value-control')) {
      errorEl.textContent = t('policies.editor.validation.still_loading', {}, 'still loading, try again in a moment');
      errorEl.style.display = '';
      return;
    }
    let op = opEl.value;
    const path = currentPath();
    if (!path) {
      errorEl.textContent = t('policies.editor.validation.argument_required', {}, 'argument is required');
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
    const conditions = rule.conditions.slice();
    let remembers = rule.remembers;
    // Only appending to a condition is sure to narrow it (saveConditions).
    const loosens = !(condIdx >= 0 && predIdx < 0);
    if (condIdx >= 0 && predIdx >= 0) {
      conditions[condIdx] = conditions[condIdx].map((p, j) => (j === predIdx ? predicate : p));
    } else if (condIdx >= 0) {
      conditions[condIdx] = conditions[condIdx].concat([predicate]);
    } else {
      conditions.push([predicate]);
      // A new condition takes the card's current lifetime value.
      remembers = (rule.remembers || []).concat([rule.remember_minutes || 0]);
    }
    await saveConditions(conditions, remembers, loosens);
  });

  return card;
}

