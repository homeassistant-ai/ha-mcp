// The HA sidebar panel's proxy answers these routes itself so it can also move
// the integration's entries; every other page reaches the server's own routes.
function initSettingsTransfer() {
  const section = document.getElementById('settingsTransfer');
  if (!section) return;
  const exportButton = document.getElementById('transferExport');
  const importButton = document.getElementById('transferImport');
  const input = document.getElementById('transferFile');
  const status = document.getElementById('transferStatus');
  section.hidden = false;

  function report(key, isError = false) {
    setStatusAlert(status, isError);
    status.textContent = key ? t('transfer.' + key) : '';
  }

  function setBusy(busy) {
    exportButton.disabled = busy;
    input.disabled = busy;
    importButton.disabled = busy || !input.files.length;
  }

  async function post(operation, body) {
    return fetch('./api/settings/transfer/' + operation, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      redirect: 'error',
      cache: 'no-store',
      body,
    });
  }

  async function reportFailure(response, fallback) {
    if (response.status === 401) return report('session', true);
    if (response.status === 413) {
      return report(fallback === 'export_failed' ? 'export_too_large' : 'too_large', true);
    }
    // Only codes with their own transfer.* message are shown; others fall back.
    const failure = await response.json().catch(() => null);
    if (response.status === 403) return report(failure?.error === 'origin_denied' ? 'origin_denied' : 'session', true);
    const sharedErrors = ['server_unavailable', 'allowed_paths_store_unavailable', 'extra_yaml_keys_store_unavailable',
      'invalid_server_settings'];
    if (sharedErrors.includes(failure?.error)) return report(failure.error, true);
    if (fallback === 'export_failed' && failure?.error === 'invalid_backup') {
      return report('invalid_source_entries', true);
    }
    const importErrors = ['invalid_backup', 'incompatible_version', 'webhook_in_use', 'decision_pin_required',
      'extra_tools_entry', 'tools_must_be_disabled', 'tools_must_be_enabled', 'tools_entry_busy',
      'tools_entry_unavailable', 'rollback_incomplete'];
    report(fallback === 'restore_failed' && importErrors.includes(failure?.error) ? failure.error : fallback, true);
  }

  exportButton.addEventListener('click', async () => {
    setBusy(true);
    report('exporting');
    try {
      const response = await post('export', '{}');
      if (!response.ok) return await reportFailure(response, 'export_failed');
      const contents = await response.text();
      if (JSON.parse(contents)?.format !== 'ha-mcp-settings') return report('export_failed', true);
      const url = URL.createObjectURL(new Blob([contents], {type: 'application/json'}));
      const link = document.createElement('a');
      link.download = 'ha-mcp-settings-' + new Date().toISOString().slice(0, 10) + '.json';
      link.href = url;
      document.body.appendChild(link);
      try {
        link.click();
      } finally {
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      report('downloaded');
    } catch (err) {
      console.error('HA-MCP settings export failed', err);
      report('export_failed', true);
    } finally {
      setBusy(false);
    }
  });

  input.addEventListener('change', () => {
    setBusy(false);
    report('');
  });
  importButton.addEventListener('click', async () => {
    const file = input.files[0];
    if (!file) return;
    // Mirrors MAX_BYTES in the component's settings_transfer_schema.py.
    if (file.size > 4 * 1024 * 1024) return report('too_large', true);
    setBusy(true);
    try {
      let contents;
      try {
        contents = await file.text();
      } catch (err) {
        console.error('HA-MCP could not read the settings file', err);
        return report('read_failed', true);
      }
      let bundle;
      try {
        bundle = JSON.parse(contents);
      } catch {
        return report('invalid_backup', true);
      }
      if (bundle?.format !== 'ha-mcp-settings') return report('invalid_backup', true);
      const prompt = bundle.contains_credentials === true ? 'confirm_connection' : 'confirm';
      if (!confirm(t('transfer.' + prompt))) return;
      report('importing');
      const response = await post('import', contents);
      if (!response.ok) return await reportFailure(response, 'restore_failed');
      const result = await response.json();
      if (result?.success !== true) return report('restore_failed', true);
      input.value = '';
      const restart = result.restart === 'server' ? 'restart_server' : 'restart';
      report(result.restart_required ? restart : 'imported');
      for (const part of result.skipped || []) {
        if (part === 'connection' || part === 'file_permissions') {
          status.textContent += ' ' + t('transfer.' + part + '_skipped');
        }
      }
    } catch (err) {
      console.error('HA-MCP settings import failed', err);
      report('restore_failed', true);
    } finally {
      setBusy(false);
    }
  });
}

initSettingsTransfer();
