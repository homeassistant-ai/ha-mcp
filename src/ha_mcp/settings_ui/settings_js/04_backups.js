// ===== Backups tab =====
let backupEntries = [];
let backupConfigFields = [];

const BACKUP_FIELD_LABELS = {
  enable_snapshot_actions: {
    label: 'Allow full HA snapshot actions',
    help: 'Allow AI assistants to manage full Home Assistant snapshots through ha_manage_backup. Turning this off blocks every snapshot action, including listing. Snapshot deletion also requires Allow snapshot deletion. Edit backups and human actions in this page remain available.',
  },
  backup_read_only: {
    label: 'Make backup management read-only',
    help: 'Allow AI assistants to list, view, and diff edit backups, and list full HA snapshots when snapshot actions are enabled. Block manual backup create, restore (including edit restores), and delete. Automatic pre-edit backups continue. Human actions in this page remain available.',
  },
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
    help: 'Leave empty for the default: /data/ha_mcp_backups in the App (add-on), otherwise the backups/ subdirectory of the ha-mcp data directory; an install that already holds snapshots under the earlier default keeps using it. The directory in use is shown in the backup status. Or enter an absolute path.',
  },
  auto_backup_calendar_lookahead_days: {
    label: 'Calendar lookahead (days)',
    help: 'How far ahead to query for calendar events when capturing pre-edit snapshots. Range 1–365.',
  },
  enable_snapshot_delete: {
    label: 'Allow snapshot deletion',
    help: 'Lets ha_manage_backup delete full HA snapshot tarballs. Off by default: a snapshot may be the last recovery point after a mistaken change. Even when on, scheduled backups, the newest remaining snapshot, and anything younger than the age floor below stay protected.',
  },
  snapshot_delete_min_age_days: {
    label: 'Minimum snapshot age to delete (days)',
    help: 'A snapshot must be at least this old before it can be deleted. Range 0–365; 0 disables the floor (the newest-snapshot and scheduled-backup protections still apply).',
  },
};

const BACKUP_ORIGIN_LABELS = {
  addon: escapeHtml(t('backup.origins.addon', {}, 'Synced to Supervisor. Restart required after save.')),
  env: null,  // banner generated dynamically with the env var name
  file: escapeHtml(t('backup.origins.file', {}, 'Persisted locally; takes effect immediately.')),
  default: escapeHtml(t('backup.origins.default', {}, 'Using default; first save creates a local override file.')),
};

async function loadBackupConfig() {
  const formEl = document.getElementById('backupConfigForm');
  const actionsEl = document.getElementById('backupConfigActions');
  try {
    const resp = await fetch('./api/settings/backup-config');
    if (!resp.ok) {
      formEl.innerHTML = `<div class="backup-empty">${escapeHtml(t('backup.errors.load_settings', {}, 'Could not load backup settings.'))}</div>`;
      actionsEl.style.display = 'none';
      return;
    }
    const data = await resp.json();
    backupConfigFields = data.fields || [];
    if (typeof data.is_addon === 'boolean') {
      IS_ADDON_MODE = data.is_addon;
    }
  } catch (_e) {
    formEl.innerHTML = `<div class="backup-empty">${escapeHtml(t('backup.errors.unavailable', {}, 'Backup settings unavailable.'))}</div>`;
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
    const meta = localizeMeta(
      'backup.fields',
      f.field,
      BACKUP_FIELD_LABELS[f.field] || { label: f.field, help: '' }
    );
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
      else if (f.field === 'snapshot_delete_min_age_days') { min = 0; max = 365; }
      controlHtml = `<input type="number" name="backup:${escapeHtml(f.field)}" data-field="${escapeHtml(f.field)}" aria-labelledby="label-backup-${escapeHtml(f.field)}" value="${Number(f.value)}" min="${min}" max="${max}" ${f.editable ? '' : 'disabled'}>`;
    }
    let originMsg;
    if (f.origin === 'env') {
      originMsg = envLockedNoteHtml(f.env_var, f.field);
    } else {
      originMsg = BACKUP_ORIGIN_LABELS[f.origin] || '';
    }
    const lockedBadge = f.editable ? '' : `<span class="backup-field-locked">${escapeHtml(t('common.env_locked', {}, 'env-locked'))}</span>`;
    const originSeparator = meta.help && !/[.!?…]$/.test(meta.help.trim()) ? '. ' : ' ';
    row.innerHTML =
      `<span class="backup-field-label" id="label-backup-${escapeHtml(f.field)}">${escapeHtml(meta.label)}</span>` +
      `<span class="backup-field-control">${controlHtml}</span>` +
      lockedBadge +
      `<span class="backup-field-help">${escapeHtml(meta.help)}${originMsg ? originSeparator + originMsg : ''}</span>`;
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
    statusEl.textContent = t('status.nothing_editable', {}, 'Nothing editable.');
    return;
  }
  btn.disabled = true;
  setStatusAlert(statusEl, false);
  statusEl.textContent = t('status.saving', {}, 'Saving…');
  try {
    const resp = await fetch('./api/settings/backup-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      btn.disabled = false;
      let msg = t('errors.save_failed', {}, 'Save failed');
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
      statusEl.textContent = t('status.saved_restart', {}, 'Saved. Restart required.');
      showToast(t('status.saved_restart', {}, 'Saved. Restart required.'));
      markRestartRequired();
    } else {
      statusEl.textContent = t('status.saved', {}, 'Saved.');
      showToast(t('status.saved', {}, 'Saved.'));
      // Refresh display so origins update (default → file, etc.).
      loadBackupConfig();
      loadBackups();
    }
  } catch (err) {
    btn.disabled = false;
    setStatusAlert(statusEl, true);
    const message = t('errors.network', {message: String(err)}, 'Network error: ' + String(err));
    statusEl.textContent = message;
    showToast(message, {isError: true});
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
      reason: t('errors.network', {message: String(err)}, 'Network error: ' + String(err)),
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
    `<div class="feature-name">${escapeHtml(t('filesystem.custom.title', {}, 'Custom filesystem paths (advanced)'))}</div>` +
    `<div class="feature-help">${tHtml('filesystem.custom.help', {}, 'Extra paths (one per line) that file tools may <strong>read and write</strong>. Config-relative paths, including exact filenames such as <code>sensor.yaml</code>, and paths under <code>/share</code>, <code>/media</code>, <code>/ssl</code>, and <code>/backup</code> are supported. Each entry allows that path and anything below it. You can also manage these paths in Home Assistant under Settings → Devices & Services → HA-MCP Custom Component → HA-MCP File & YAML Tools → Configure; both locations edit the same setting and apply changes immediately.')}</div>` +
    `<div class="feature-help">${tHtml('filesystem.custom.blocked', {paths: `<code>${escapeHtml(denyList)}</code>`}, 'Always blocked (cannot be added): {paths}, path traversal (<code>..</code>), and any absolute path outside the HAOS sibling volumes.')}</div>`;

  const control = document.createElement('div');
  control.className = 'feature-control';

  if (lockedByGate) {
    const note = document.createElement('div');
    note.className = 'feature-locked-note';
    note.textContent = t('filesystem.custom.enable_first', {}, 'Enable beta features and filesystem tools above to edit.');
    control.appendChild(note);
  } else if (!d) {
    const note = document.createElement('div');
    note.className = 'feature-help';
    note.textContent = t('status.loading_ellipsis', {}, 'Loading…');
    control.appendChild(note);
  } else if (!d.available) {
    const note = document.createElement('div');
    note.className = 'feature-locked-note';
    note.textContent =
      d.reason || t('filesystem.custom.unavailable', {}, 'Custom paths are currently unavailable.');
    control.appendChild(note);
  } else {
    const ta = document.createElement('textarea');
    ta.id = 'fsCustomPathsInput';
    ta.rows = 4;
    ta.value = (d.paths || []).join('\n');
    const btn = document.createElement('button');
    btn.id = 'fsCustomPathsSave';
    btn.className = 'adv-save-btn';
    btn.textContent = t('filesystem.custom.save', {}, 'Save paths');
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
  statusEl.textContent = t('status.saving', {}, 'Saving…');
  try {
    const resp = await fetch('./api/settings/fs-custom-paths', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    });
    const data = await resp.json();
    btn.disabled = false;
    if (!resp.ok || !data.success) {
      let msg = t('errors.save_failed', {}, 'Save failed');
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
      ? t(
          'filesystem.custom.saved_rejected',
          {paths: rejected.join(', ')},
          `Saved. Rejected (blocked or invalid): ${rejected.join(', ')}`
        )
      : t('status.saved', {}, 'Saved.');
  } catch (err) {
    btn.disabled = false;
    setStatusAlert(statusEl, true);
    statusEl.textContent = t('errors.network', {message: String(err)}, 'Network error: ' + String(err));
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
      stateEl.innerHTML = `<span class="diff-rem">${escapeHtml(t('backup.errors.load_list', {}, 'Error loading backups'))}</span>`;
      listEl.innerHTML = '';
      return;
    }
    backupEntries = data.backups || [];
    stateEl.innerHTML =
      `<span>${escapeHtml(t('backup.state.status', {}, 'Status'))}: <strong>${escapeHtml(data.enabled ? t('common.enabled', {}, 'enabled') : t('common.disabled', {}, 'disabled'))}</strong></span>` +
      `<span>${escapeHtml(t('backup.state.throttle', {}, 'Throttle'))}: <strong>${escapeHtml(t('common.minutes_short', {count: data.throttle_minutes}, `${data.throttle_minutes} min`))}</strong></span>` +
      `<span>${escapeHtml(t('backup.state.retain', {}, 'Retain per entity'))}: <strong>${data.retain_per_entity}</strong></span>` +
      `<span>${escapeHtml(t('backup.state.directory', {}, 'Directory'))}: <strong>${escapeHtml(data.backup_dir)}</strong></span>` +
      `<span>${escapeHtml(t('backup.state.total', {}, 'Total'))}: <strong>${data.count}</strong></span>`;
    renderBackups();
  } catch (err) {
    stateEl.innerHTML = `<span class="diff-rem">${escapeHtml(t('errors.network', {message: String(err)}, 'Network error: ' + String(err)))}</span>`;
    listEl.innerHTML = '';
  }
}

function renderBackups() {
  const listEl = document.getElementById('backupList');
  if (!backupEntries.length) {
    listEl.innerHTML = `<div class="backup-empty">${escapeHtml(t('backup.empty', {}, 'No backups yet. Enable auto-backup in the App (add-on) config and edit an entity to create one.'))}</div>`;
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
          `${escapeHtml(b.entity_id)} · ${tsFmt} · ${escapeHtml(t('common.bytes', {count: b.size}, `${b.size} bytes`))}` +
        `</div>` +
      `</div>` +
      `<div class="backup-row-actions">` +
        `<button data-act="view">${escapeHtml(t('actions.view', {}, 'View'))}</button>` +
        `<button data-act="diff" class="secondary">${escapeHtml(t('actions.diff', {}, 'Diff'))}</button>` +
        `<button data-act="restore">${escapeHtml(t('actions.restore', {}, 'Restore'))}</button>` +
        `<button data-act="delete" class="danger">${escapeHtml(t('actions.delete', {}, 'Delete'))}</button>` +
      `</div>`;
    row.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', () => backupAction(btn.dataset.act, b.name));
    });
    listEl.appendChild(row);
  });
}

function backupRestoreOutcomeMessage(outcome = {}) {
  let message;
  if (outcome.apply_status === 'not_applied') {
    message = t('backup.restore.not_applied', {}, 'Restore was not applied. Nothing was changed.');
  } else if (outcome.apply_status === 'applied') {
    if (outcome.verification_status === 'mismatched') {
      message = t('backup.restore.mismatched', {}, 'Restore was applied, but the current configuration does not match the backup.');
    } else if (outcome.verification_status === 'matched') {
      message = t('backup.restore.verified', {}, 'Restore was applied and verified.');
    } else {
      message = t('backup.restore.unverified', {}, 'Restore was applied, but verification is unavailable.');
    }
  } else {
    message = t('backup.restore.unknown', {}, 'Whether this restore changed Home Assistant could not be confirmed. Inspect the current configuration and backup list before retrying.');
  }
  const reasons = {
    unsupported_form: t('backup.restore.reason.unsupported_form', {}, 'Home Assistant did not provide a form suitable for this restore.'),
    unsupported_fields: t('backup.restore.reason.unsupported_fields', {}, 'Some snapshot fields are not accepted by the current form.'),
    validation_failed: t('backup.restore.reason.validation_failed', {}, 'Home Assistant rejected the restored configuration as invalid.'),
    flow_aborted: t('backup.restore.reason.flow_aborted', {}, 'Home Assistant aborted the restore flow.'),
  };
  if (Object.hasOwn(reasons, outcome.reason)) {
    message += '\n\n' + reasons[outcome.reason];
    if (Array.isArray(outcome.fields) && outcome.fields.length) {
      message += '\n' + t('backup.restore.fields', {fields: outcome.fields.join(', ')}, 'Fields: ' + outcome.fields.join(', '));
    }
  }
  if (outcome.restore_mode === 'recreated') {
    const result = outcome.result || outcome;
    const entryId = outcome.entry_id || result.entry_id;
    if (entryId) {
      message += '\n\n' + t('backup.restore.recreated_entry', {entry_id: entryId}, 'Recreated config entry: ' + entryId);
    }
    const mapping = outcome.entity_id_mapping || result.entity_id_mapping || {};
    if (mapping.restored_entity_id) {
      message += '\n' + t('backup.restore.entity_mapping', {created: mapping.created_entity_id, restored: mapping.restored_entity_id}, 'Entity ID: ' + mapping.created_entity_id + ' → ' + mapping.restored_entity_id);
    } else if (mapping.target_entity_id) {
      message += '\n' + t('backup.restore.entity_mapping_unknown', {created: mapping.created_entity_id, target: mapping.target_entity_id}, 'Entity rename could not be confirmed: ' + mapping.created_entity_id + ' → ' + mapping.target_entity_id + '. Inspect the new entry before retrying.');
    }
    if (result.entity_ids_restored === false) {
      message += '\n' + t('backup.restore.mapping_unavailable', {}, 'This snapshot has no entity mapping; the recreated helper may have a new entity ID.');
    }
  }
  if (outcome.conflicting_entity_id) {
    message += '\n' + t('backup.restore.entity_collision', {entity_id: outcome.conflicting_entity_id}, 'The saved entity ID ' + outcome.conflicting_entity_id + ' is already in use and was not overwritten.');
  }
  if (outcome.safety_backup) {
    message += '\n\n' + t('backup.restore.safety', {name: outcome.safety_backup}, 'Safety backup: ' + outcome.safety_backup + '. Inspect the current configuration before restoring this safety backup to recover the previous state.');
  }
  return message;
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
      showModal(t('backup.modal.view', {name}, 'View: ' + name), '<pre>' + escapeHtml(yamlStringify(data.data)) + '</pre>');
    } catch (err) {
      showToast(t('backup.errors.load_one', {name, message: String(err)}, 'Could not load backup "' + name + '": ' + String(err)), {isError: true});
    }
  } else if (act === 'diff') {
    try {
      const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name) + '/diff');
      const data = await resp.json();
      if (!resp.ok) { alert(JSON.stringify(data)); return; }
      const html = (data.diff || t('backup.identical', {}, '(identical)')).split('\n').map(line => {
        let cls = '';
        if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) cls = 'diff-hdr';
        else if (line.startsWith('+')) cls = 'diff-add';
        else if (line.startsWith('-')) cls = 'diff-rem';
        return `<span class="${cls}">${escapeHtml(line)}</span>`;
      }).join('\n');
      showModal(t('backup.modal.diff', {name}, 'Diff: ' + name), '<pre>' + html + '</pre>');
    } catch (err) {
      showToast(t('backup.errors.diff', {name, message: String(err)}, 'Could not diff backup "' + name + '": ' + String(err)), {isError: true});
    }
  } else if (act === 'restore') {
    if (!confirm(t('backup.confirm.restore', {name}, 'Restore ' + name + '?\n\nThis overwrites existing configuration or recreates a deleted Template helper. Existing Template helpers require a fresh safety backup. Other restores use the current auto-backup settings and may proceed without a new safety backup.'))) return;
    let stage = 'request';
    let httpStatus = null;
    try {
      const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name) + '/restore', {method: 'POST'});
      httpStatus = resp.status;
      stage = 'response_json';
      const data = await resp.json();
      stage = 'outcome';
      if (!resp.ok || !data.success) {
        const outcome = data.data || {};
        let message = backupRestoreOutcomeMessage(outcome);
        if (data.error?.message) message += '\n\n' + data.error.message;
        alert(message);
        if (outcome.safety_backup || outcome.apply_status !== 'not_applied') await loadBackups();
        return;
      }
      const safetyBackup = data.data && data.data.safety_backup ? data.data.safety_backup : t('common.none', {}, '(none)');
      alert(data.data?.restore_mode === 'recreated'
        ? backupRestoreOutcomeMessage(data.data)
        : t('backup.restored', {name: safetyBackup}, 'Restored. Safety backup: ' + safetyBackup));
      await loadBackups();
    } catch (err) {
      // JSON parse errors can contain the upstream body. Retain the failure
      // stage/type/status for diagnosis without logging raw errors or options.
      const errorType = ['TypeError', 'SyntaxError', 'AbortError', 'NetworkError', 'TimeoutError'].includes(err?.name) ? err.name : 'Error';
      console.warn('Backup restore response unavailable',
        'stage=' + stage, 'error_type=' + errorType,
        'http_status=' + (Number.isInteger(httpStatus) ? httpStatus : null));
      const message = backupRestoreOutcomeMessage();
      showToast(t('backup.errors.restore', {name, message}, 'Restore of "' + name + '" failed: ' + message), {isError: true});
      await loadBackups();
    }
  } else if (act === 'delete') {
    if (!confirm(t('backup.confirm.delete', {name}, 'Delete ' + name + '? This cannot be undone.'))) return;
    try {
      const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name), {method: 'DELETE'});
      if (!resp.ok) {
        const data = await resp.json();
        const detail = data.data?.reason === 'snapshot_in_use'
          ? t('backup.delete.in_use', {}, 'This backup is in use. Retry after the active capture or restore finishes.')
          : data.error?.message || JSON.stringify(data);
        alert(t('backup.errors.delete_detail', {detail}, 'Delete failed: ' + detail));
        return;
      }
      loadBackups();
    } catch (err) {
      showToast(t('backup.errors.delete', {name, message: String(err)}, 'Delete of "' + name + '" failed: ' + String(err)), {isError: true});
    }
  }
}

async function bulkDeleteBackups() {
  const d = document.getElementById('backupDomain').value.trim();
  const e = document.getElementById('backupEntity').value.trim();
  const days = prompt(t('backup.bulk.prompt_days', {}, 'Delete backups older than N days (leave blank to use current filters only):'), '');
  const params = new URLSearchParams();
  if (d) params.set('domain', d);
  if (e) params.set('entity_id', e);
  if (days) params.set('older_than_days', days);
  if (!params.toString()) { alert(t('backup.bulk.filter_required', {}, 'Set at least one filter (Domain, Entity, or age in days).')); return; }
  if (!confirm(t('backup.bulk.confirm', {filters: params.toString()}, 'Delete all backups matching: ' + params.toString() + '?'))) return;
  try {
    const resp = await fetch('./api/settings/backups?' + params.toString(), {method: 'DELETE'});
    const data = await resp.json();
    if (!resp.ok) { alert(t('backup.errors.bulk_delete', {detail: JSON.stringify(data)}, 'Bulk delete failed: ' + JSON.stringify(data))); return; }
    const deleted = data.count || 0;
    const failed = Array.isArray(data.failed) ? data.failed : [];
    let message = failed.length
      ? t('backup.bulk.partial', {deleted, failed: failed.length}, 'Deleted ' + deleted + ' backup(s); failed to delete ' + failed.length + ' backup(s).')
      : t('backup.bulk.deleted', {count: deleted}, 'Deleted ' + deleted + ' backup(s)');
    const inUse = failed.filter(name => data.failure_reasons?.[name] === 'snapshot_in_use').length;
    if (inUse) {
      message += '\n\n' + t('backup.bulk.in_use', {count: inUse}, inUse + ' backup(s) are in use. Retry after the active capture or restore finishes.');
    }
    alert(message);
  } catch {
    const detail = t('backup.bulk.unknown', {}, 'The deletion result could not be confirmed. Check the backup list before retrying.');
    showToast(t('backup.errors.bulk_delete', {detail}, 'Bulk delete failed: ' + detail), {isError: true});
  }
  await loadBackups();
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
