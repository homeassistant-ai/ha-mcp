// The component serves these routes inside the authenticated HA sidebar proxy.
// Direct connections to the server's settings UI have no such routes.
function initSettingsTransfer() {
  const section = document.getElementById('settingsTransfer');
  if (!section || location.pathname !== '/api/ha_mcp_tools/ui/app/settings') return;
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
    if (response.status === 401 || response.status === 403) return report('session', true);
    if (response.status === 413) {
      return report(fallback === 'export_failed' ? 'export_too_large' : 'too_large', true);
    }
    // Import validation errors can concern credentials. Display only known codes.
    const failure = await response.json().catch(() => null);
    if (failure?.error === 'server_unavailable') return report('server_unavailable', true);
    if (fallback === 'export_failed') {
      if (failure?.error === 'invalid_backup') return report('invalid_source_entries', true);
      if (failure?.error === 'invalid_server_settings') return report('invalid_server_settings', true);
    }
    const importErrors = ['invalid_backup', 'incompatible_version', 'webhook_in_use', 'decision_pin_required'];
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
    } catch {
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
    if (file.size > 4 * 1024 * 1024) return report('too_large', true);
    setBusy(true);
    try {
      let contents;
      try {
        contents = await file.text();
      } catch {
        return report('read_failed', true);
      }
      try {
        if (JSON.parse(contents)?.format !== 'ha-mcp-settings') return report('invalid_backup', true);
      } catch {
        return report('invalid_backup', true);
      }
      if (!confirm(t('transfer.confirm'))) return;
      report('importing');
      const response = await post('import', contents);
      if (!response.ok) return await reportFailure(response, 'restore_failed');
      const result = await response.json();
      if (result?.success !== true) return report('restore_failed', true);
      input.value = '';
      report(result.restart_required ? 'restart' : 'imported');
    } catch {
      report('restore_failed', true);
    } finally {
      setBusy(false);
    }
  });
}

initSettingsTransfer();
