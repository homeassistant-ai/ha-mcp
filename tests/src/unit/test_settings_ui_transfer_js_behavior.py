"""The Home Assistant sidebar panel's settings export and import controls."""

from __future__ import annotations

import json
import re

import pytest

from ha_mcp.settings_ui import render_settings_html

from ._js_harness import HarnessResult, extract_script_body, run_script
from ._settings_ui_dom import DEFAULT_FETCHES

_PANEL = "/api/ha_mcp_tools/ui/app/settings"
_TRANSFER = "./api/settings/transfer/"
_BUNDLE = '{"format":"ha-mcp-settings","version":1,"entries":{"server":{"webhook_id":"test-private-id"}}}'


def _run(
    invoke: str,
    *,
    path: str = _PANEL,
    responses: dict | None = None,
    prelude: str = "",
) -> HarnessResult:
    page = render_settings_html()
    result = run_script(
        extract_script_body(page),
        initial_html=page,
        prelude=f"history.replaceState(null, '', {json.dumps(path)});" + prelude,
        fetch_map={**DEFAULT_FETCHES, **(responses or {})},
        invoke=invoke
        + """
            await new Promise(resolve => setTimeout(resolve, 0));
            const region = document.getElementById('transferStatus');
            __captureStatus(region.getAttribute('role') + ': ' + region.textContent);
        """,
    )
    assert not result.errors
    return result


def _choose(contents: str, size: int = 100) -> str:
    # JSDOM does not implement the native file picker or File.text().
    return f"""
        const input = document.getElementById('transferFile');
        Object.defineProperty(input, 'files', {{
          get: () => [{{size: {size}, text: async () => {json.dumps(contents)}}}],
        }});
        input.dispatchEvent(new Event('change'));
        document.getElementById('transferImport').click();
    """


def test_direct_server_has_no_transfer_controls_or_requests() -> None:
    result = _run("", path="/mcp/settings")
    assert re.search(r'<section id="settingsTransfer"[^>]* hidden', result.dom)
    assert not result.fetches_to(_TRANSFER)


def test_download_preserves_the_export_without_rendering_credentials() -> None:
    result = _run(
        "document.getElementById('transferExport').click();",
        responses={_TRANSFER + "export": {"body": _BUNDLE}},
        prelude="""
            // Observe the browser download instead of navigating in JSDOM.
            URL.createObjectURL = blob => {
              document.body.dataset.blobSize = String(blob.size);
              document.body.dataset.mime = blob.type;
              return 'blob:https://test.local/test-download';
            };
            URL.revokeObjectURL = () => { document.body.dataset.revoked = 'yes'; };
            HTMLAnchorElement.prototype.click = function () {
              document.body.dataset.filename = this.download;
            };
        """,
    )
    assert not re.search(r'<section id="settingsTransfer"[^>]* hidden', result.dom)
    assert [r["method"] for r in result.fetches_to(_TRANSFER + "export")] == ["POST"]
    assert f'data-blob-size="{len(_BUNDLE.encode())}"' in result.dom
    assert 'data-mime="application/json"' in result.dom
    assert re.search(
        r'data-filename="ha-mcp-settings-\d{4}-\d{2}-\d{2}\.json"', result.dom
    )
    assert 'data-revoked="yes"' in result.dom
    assert "test-private-id" not in result.dom
    assert result.status == "status: Settings download started."


def test_cancel_import_keeps_the_file_local() -> None:
    result = _run(_choose(_BUNDLE), prelude="window.confirm = () => false;")
    assert not result.fetches_to(_TRANSFER)


@pytest.mark.parametrize("restart", [True, False])
def test_confirmed_import_sends_original_file_and_reports_next_step(
    restart: bool,
) -> None:
    result = _run(
        _choose(_BUNDLE),
        responses={
            _TRANSFER + "import": {
                "json": {"success": True, "restart_required": restart}
            }
        },
    )
    assert len(result.confirms) == 1
    imports = result.fetches_to(_TRANSFER + "import")
    assert [(r["method"], r["body"]) for r in imports] == [("POST", _BUNDLE)]
    assert "test-private-id" not in result.dom
    assert result.status and result.status.startswith("status: Settings imported.")
    assert (
        "Restart Home Assistant" if restart else "Reopen the HA-MCP panel"
    ) in result.status


@pytest.mark.parametrize(
    ("contents", "size", "message"),
    [
        ("{broken", 10, "Choose a valid HA-MCP"),
        ('{"format":"other"}', 18, "Choose a valid HA-MCP"),
        (_BUNDLE, 4 * 1024 * 1024 + 1, "4 MiB or less"),
    ],
)
def test_invalid_file_never_prompts_or_uploads(
    contents: str, size: int, message: str
) -> None:
    result = _run(_choose(contents, size))
    assert not result.confirms
    assert not result.fetches_to(_TRANSFER)
    assert (
        result.status
        and result.status.startswith("alert:")
        and message in result.status
    )


def test_failed_import_never_renders_arbitrary_error_text() -> None:
    result = _run(
        _choose(_BUNDLE),
        responses={
            _TRANSFER + "import": {"status": 400, "json": {"error": "test-private-id"}}
        },
    )
    assert "test-private-id" not in result.dom
    assert result.status and result.status.startswith("alert: HA-MCP could not finish")


def test_pin_policy_explains_how_to_complete_import_on_the_destination() -> None:
    result = _run(
        _choose(_BUNDLE),
        responses={
            _TRANSFER + "import": {
                "status": 400,
                "json": {"success": False, "error": "decision_pin_required"},
            }
        },
    )
    assert result.status and result.status.startswith(
        "alert: This policy requires a decision PIN"
    )
    assert "Tool Security Policies" in result.status
