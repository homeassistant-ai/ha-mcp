"""Settings UI backup action toasts and restore outcome messages (JSDOM)."""

from __future__ import annotations

import json

import pytest

from . import test_settings_ui_js_behavior as _settings_ui
from ._js_harness import run_script
from .test_settings_ui_js_behavior import (
    DEFAULT_FETCHES,
    MIN_DOM,
    _assert_clean_init,
    _probe,
)

settings_script = _settings_ui.settings_script  # the module-scoped fixture


class TestBackupActionErrorToast:
    """A network drop on a destructive backup action surfaces a visible
    error toast instead of silently no-opping (the bare rejection would
    only reach the visually-hidden #status region).
    """

    @pytest.mark.parametrize(
        "act,verb,name,throw_pattern",
        [
            ("restore", "Restore", "snap_a", "/restore"),
            ("delete", "Delete", "snap_b", "/backups/snap_b"),
        ],
    )
    def test_network_error_shows_error_toast(
        self,
        settings_script: str,
        act: str,
        verb: str,
        name: str,
        throw_pattern: str,
    ) -> None:
        fetches = {**DEFAULT_FETCHES, throw_pattern: {"throw": "network down"}}
        invoke = (
            (
                "await new Promise(r => setTimeout(r, 100));\n"
                "await window.backupAction('__ACT__', '__NAME__');\n"
                "const t = document.querySelector('#ha-toast-region .ha-toast');\n"
                "document.body.setAttribute('data-toast',\n"
                "  t ? (t.className + '::' + "
                "(t.querySelector('.ha-toast-msg')?.textContent || '')) : 'NONE');\n"
            )
            .replace("__ACT__", act)
            .replace("__NAME__", name)
        )
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=fetches,
            settle_ms=300,
            invoke=invoke,
        )
        _assert_clean_init(result)
        toast = _probe(result, "toast") or ""
        assert "ha-toast-error" in toast, (
            f"expected an error toast for a failed {act}; got {toast!r}"
        )
        assert verb in toast and name in toast and "failed" in toast, (
            f"error toast missing action/name context; got {toast!r}"
        )


class TestBackupRestoreDiagnostics:
    """Unknown restore results retain diagnostics without echoing upstream bodies."""

    @pytest.mark.parametrize(
        ("reply", "stage", "error_type", "status"),
        [
            ({"throw": "option-value-secret"}, "request", "TypeError", None),
            (
                {"status": 502, "body": "<html>option-value-secret</html>"},
                "response_json",
                "SyntaxError",
                502,
            ),
        ],
    )
    def test_uncertain_response_logs_safe_context_and_refreshes_once(
        self, settings_script: str, reply: dict, stage: str, error_type: str, status
    ) -> None:
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            settle_ms=300,
            fetch_map={
                **DEFAULT_FETCHES,
                "/restore": reply,
                "/backups?": {"status": 200, "json": {"success": True, "backups": []}},
            },
            invoke=(
                "await new Promise(resolve => setTimeout(resolve, 100));"
                "await window.backupAction('restore', 'snap_a');"
            ),
        )
        _assert_clean_init(result)
        assert len(result.fetches_to("/restore")) == 1
        assert len(result.fetches_to("/backups?")) == 1
        assert "could not be confirmed" in result.dom
        assert "before retrying" in result.dom
        diagnostics = [
            row
            for row in result.console
            if row["level"] == "warn"
            and row["args"][0] == "Backup restore response unavailable"
        ]
        assert len(diagnostics) == 1
        assert diagnostics[0]["args"] == [
            "Backup restore response unavailable",
            f"stage={stage}",
            f"error_type={error_type}",
            f"http_status={status if status is not None else 'null'}",
        ]
        assert "option-value-secret" not in result.dom + json.dumps(result.console)

    @pytest.mark.parametrize(
        ("mapping", "extra", "expected"),
        [
            (
                {"created_entity_id": "sensor.new", "restored_entity_id": "sensor.old"},
                {},
                "Entity ID: sensor.new → sensor.old",
            ),
            (
                {"created_entity_id": "sensor.new", "target_entity_id": "sensor.old"},
                {},
                "Entity rename could not be confirmed: sensor.new → sensor.old",
            ),
            ({}, {"entity_ids_restored": False}, "This snapshot has no entity mapping"),
            (
                # A flow helper with several entities reports one pair each.
                [
                    {"created_entity_id": "sensor.a", "restored_entity_id": "sensor.b"},
                    {"created_entity_id": "sensor.c", "restored_entity_id": "sensor.d"},
                ],
                {},
                "Entity ID: sensor.a → sensor.b\nEntity ID: sensor.c → sensor.d",
            ),
        ],
    )
    def test_recreated_outcome_renders_mapping_and_safety_as_plain_text(
        self, settings_script: str, mapping: dict | list, extra: dict, expected: str
    ) -> None:
        outcome = {
            "apply_status": "applied",
            "verification_status": "matched",
            "restore_mode": "recreated",
            "result": {"entry_id": "new-entry", "entity_id_mapping": mapping, **extra},
            "conflicting_entity_id": "sensor.occupied",
            "safety_backup": '<img src="x" onerror="alert(1)">',
        }
        result = run_script(
            settings_script,
            initial_html=MIN_DOM,
            fetch_map=DEFAULT_FETCHES,
            settle_ms=300,
            invoke=(
                f"showToast(backupRestoreOutcomeMessage({json.dumps(outcome)}));"
                "document.body.setAttribute('data-message', "
                "document.querySelector('.ha-toast-msg').textContent);"
                "document.body.setAttribute('data-injected', "
                "document.querySelectorAll('.ha-toast-msg img').length);"
            ),
        )
        _assert_clean_init(result)
        message = _probe(result, "message") or ""
        assert "Restore was applied and verified" in message
        assert "Recreated config entry: new-entry" in message
        assert expected in message
        assert "sensor.occupied is already in use" in message
        assert "Safety backup:" in message
        assert _probe(result, "injected") == "0"
