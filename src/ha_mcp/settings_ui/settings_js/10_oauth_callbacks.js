// ===== OAuth callback allowlist (embedded server, #2427) =====
// While remote access uses the secret webhook URL, the server's OAuth sign-in
// returns only to callbacks on this list. The list lives on the HA-MCP
// integration's server entry (also editable on its Configure screen) and
// applies to the next sign-in, so this row saves with its own button and
// needs no restart. Other install types have no list: the GET answers
// available:false without a reason and the section stays hidden.
let _oauthCallbacks = null;

async function loadOAuthCallbacks() {
  try {
    const resp = await fetch('./api/settings/oauth-callbacks');
    if (resp.ok) {
      _oauthCallbacks = await resp.json();
    } else {
      // Keep the section visible with the reason instead of hiding it.
      console.error('loadOAuthCallbacks: HTTP', resp.status);
      _oauthCallbacks = {available: false, reason: t('oauth_callbacks.load_failed',
        {status: resp.status}, `Could not load the callback list (HTTP ${resp.status}). Reload the page to try again.`)};
    }
  } catch (err) {
    console.error('loadOAuthCallbacks failed:', err);
    _oauthCallbacks = null;
  }
  renderOAuthCallbacks();
}

function renderOAuthCallbacks() {
  const section = document.getElementById('oauthCallbacksSection');
  const body = document.getElementById('oauthCallbacksBody');
  if (!section || !body) return;
  const d = _oauthCallbacks;
  if (!d || (!d.available && !d.reason)) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  body.innerHTML = '';

  const row = document.createElement('div');
  row.className = 'feature-row list-editor';
  const info = document.createElement('div');
  info.className = 'feature-info';
  const defaults = (d.default_allowlist || []).map(u => `<code>${escapeHtml(u)}</code>`).join(', ');
  info.innerHTML =
    `<div class="feature-name" id="label-oauth-callbacks">${escapeHtml(t('oauth_callbacks.title', {}, 'Allowed OAuth callback URLs'))}</div>` +
    `<div class="feature-help">${tHtml('oauth_callbacks.help', {}, 'While Authentication mode is the secret webhook URL, a client that signs in with OAuth is sent back only to a callback URL on this list; any other callback is refused with a page explaining how to add it. Enter one URL per line, exactly as the client sends it. An <code>http://</code> loopback callback (127.0.0.1, [::1] or localhost) also matches on any port. Clients that connect with the webhook URL alone need no entry. Changes apply to the next sign-in, without a restart.')}</div>` +
    (defaults
      ? `<div class="feature-help">${tHtml('oauth_callbacks.default', {urls: defaults}, 'Default list: {urls}')}</div>`
      : '') +
    (d.available && !d.applies
      ? `<div class="feature-locked-note">${escapeHtml(t('oauth_callbacks.inactive', {}, 'Not in use right now: the list applies only while remote access is on and Authentication mode is the secret webhook URL.'))}</div>`
      : '');

  const control = document.createElement('div');
  control.className = 'feature-control';
  if (!d.available) {
    const note = document.createElement('div');
    note.className = 'feature-locked-note';
    note.textContent = d.reason;
    control.appendChild(note);
  } else {
    const ta = document.createElement('textarea');
    ta.id = 'oauthCallbacksInput';
    ta.rows = 4;
    ta.spellcheck = false;
    ta.setAttribute('aria-labelledby', 'label-oauth-callbacks');
    ta.value = (d.allowlist || []).join('\n');
    const actions = document.createElement('div');
    actions.className = 'list-editor-actions';
    const save = document.createElement('button');
    save.id = 'oauthCallbacksSave';
    save.className = 'adv-save-btn';
    save.textContent = t('oauth_callbacks.save', {}, 'Save callbacks');
    save.addEventListener('click', () => saveOAuthCallbacks({
      allowlist: ta.value.split('\n').map(s => s.trim()).filter(Boolean),
    }));
    actions.appendChild(save);
    if (d.customized) {
      const reset = document.createElement('button');
      reset.id = 'oauthCallbacksReset';
      reset.className = 'a11y-reset';
      reset.textContent = t('oauth_callbacks.reset', {}, 'Restore default');
      reset.addEventListener('click', () => saveOAuthCallbacks({reset: true}));
      actions.appendChild(reset);
    }
    const status = document.createElement('div');
    status.id = 'oauthCallbacksStatus';
    status.className = 'feature-help';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    control.appendChild(ta);
    control.appendChild(actions);
    control.appendChild(status);
  }
  row.appendChild(info);
  row.appendChild(control);
  body.appendChild(row);
}

async function saveOAuthCallbacks(change) {
  const statusEl = document.getElementById('oauthCallbacksStatus');
  const buttons = ['oauthCallbacksSave', 'oauthCallbacksReset']
    .map(id => document.getElementById(id)).filter(Boolean);
  if (!statusEl) return;
  buttons.forEach(b => { b.disabled = true; });
  setStatusAlert(statusEl, false);
  statusEl.textContent = t('status.saving', {}, 'Saving…');
  let resp, data;
  try {
    resp = await fetch('./api/settings/oauth-callbacks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(change),
    });
  } catch (err) {
    buttons.forEach(b => { b.disabled = false; });
    setStatusAlert(statusEl, true);
    statusEl.textContent = t('errors.network', {message: String(err)}, 'Network error: ' + String(err));
    return;
  }
  buttons.forEach(b => { b.disabled = false; });
  try {
    data = await resp.json();
  } catch (err) {
    // A proxy error page, not our JSON: report the status, not a parse error.
    setStatusAlert(statusEl, true);
    statusEl.textContent = t('errors.save_failed_non_json', {status: resp.status},
      `Save failed (HTTP ${resp.status}, non-JSON body)`);
    return;
  }
  if (!resp.ok || !data.success) {
    const invalid = (data && data.invalid) || [];
    let msg;
    if (invalid.length) {
      msg = t('oauth_callbacks.invalid', {urls: invalid.join(', ')},
        `Not saved. These are not usable callback URLs: ${invalid.join(', ')}. Use https://, or http:// on 127.0.0.1, [::1] or localhost, without a #fragment.`);
    } else {
      msg = (data && data.error && data.error.message) || t('errors.save_failed', {}, 'Save failed');
    }
    setStatusAlert(statusEl, true);
    statusEl.textContent = msg;
    return;
  }
  _oauthCallbacks = {...data, available: true};
  renderOAuthCallbacks();
  const fresh = document.getElementById('oauthCallbacksStatus');
  if (fresh) fresh.textContent = t('oauth_callbacks.saved', {}, 'Saved. Applies to the next sign-in.');
}

loadOAuthCallbacks();
