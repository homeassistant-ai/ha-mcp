"""The LLM API's schema conversion, proved INSIDE Home Assistant (issue #2361).

``test_embedded_server.py::test_llm_api_client_path_full_catalog`` drives the
same transport, but its conversion runs in the pytest process against the test
host's ``voluptuous_openapi``. Home Assistant resolves its own converter
(``probatio.from_openapi`` since Core 2026.9) and, before a conversation turn
reaches the model, re-emits every converted schema through
``probatio.to_openapi``. That round trip is invisible from the host and is
exactly where the reported defect lived: an exclusive bound came back in the
draft-4 ``{"minimum": 0, "exclusiveMinimum": true}`` spelling, which is not
valid JSON Schema draft 2020-12, and every Anthropic turn failed.

So these tests run the component's own client path in Home Assistant's own
interpreter through ``utilities.llm_api_probe``, and assert on the report it
prints. The probe never decides pass or fail; the assertions below do.

The two tests cover the two lanes where the in-process server is the session
backend: the container embedded lane (``embedded_only``) and the HAOS embedded
lane (``haos_embedded_only``). Both use the session fixture's already-running
server rather than booting one of their own.

NOTE (skip-ceiling coupling): both tests are marker-gated, so each adds a
collection-time skip on every lane it does not run on. That consumes
``_SKIP_CEILING_PER_LANE`` budget in
tests/src/e2e/basic/test_backend_dispatch_smoke.py — see that module's
docstring; the ceilings were bumped when these landed.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import pytest
from haos_runtime import HA_MCP_SERVER_WEBHOOK_ID, ssh_exec

from ...conftest import _EMBEDDED_WEBHOOK_ID
from ...utilities.llm_api_probe import (
    LLM_API_SCHEMA_PROBE,
    PROBE_URL_ENV,
    parse_probe_report,
)

LOG = logging.getLogger(__name__)

# The component logs this on a successful ``llm.async_register_api`` (llm_api.py
# keeps the prefix stable for exactly these assertions).
_REGISTRATION_LINE = "Registered the HA-MCP toolset as LLM API"
_REGISTRATION_TIMEOUT_S = 60
_REGISTRATION_POLL_S = 2

# The probe's own budget is 120s; give the exec wrapper room above it so a hang
# surfaces as the probe's timeout rather than an opaque kill.
_PROBE_EXEC_TIMEOUT_S = 180

# Home Assistant answers its own webhook on loopback from inside the container /
# VM; the host-facing URL in ``container_info`` is not reachable from there.
_CONTAINER_WEBHOOK_URL = f"http://127.0.0.1:8123/api/webhook/{_EMBEDDED_WEBHOOK_ID}"
_HAOS_WEBHOOK_URL = f"http://127.0.0.1:8123/api/webhook/{HA_MCP_SERVER_WEBHOOK_ID}"

# The HAOS core container, addressed the same way the other HAOS tests address
# it (``docker restart homeassistant`` in test_zz_reentrant_log_deadlock.py).
_HAOS_CORE_CONTAINER = "homeassistant"


def _assert_report_clean(report: dict[str, Any]) -> None:
    """Fail with the offending tool names when the in-HA conversion misbehaved."""
    converter = report.get("converter")
    context = (
        f"converter={converter!r}, probatio={report.get('probatio')!r}, "
        f"inclusive-bounds normaliser="
        f"{report.get('inclusive_bounds_normaliser')!r}, "
        f"HA {report.get('ha_version')}"
    )

    assert not report.get("timed_out"), (
        f"the in-HA probe hit its own timeout ({context}); partial report: {report}"
    )

    tool_count = report.get("tool_count", 0)
    assert tool_count > 60, (
        f"expected the full tool inventory from inside HA, got {tool_count} "
        f"({context}) — a handful would mean a truncated or wrong server"
    )

    failures = report.get("conversion_failures") or []
    assert not failures, (
        f"{len(failures)}/{tool_count} tool schemas failed to convert inside "
        f"Home Assistant ({context}). At runtime each of these is skipped with "
        "only a warning, so the toolset would silently shrink:\n" + "\n".join(failures)
    )

    if not report.get("probatio"):
        # Core <= 2026.8: no re-emission step exists, so conversion success is
        # the whole contract on this image.
        LOG.info("probatio is absent in this HA image; re-emission checks skipped")
        return

    boolean_exclusive = report.get("boolean_exclusive") or []
    assert not boolean_exclusive, (
        "probatio.to_openapi re-emitted a BOOLEAN exclusiveMinimum/"
        "exclusiveMaximum (the draft-4 spelling) for "
        f"{boolean_exclusive} ({context}) — that is what breaks every "
        "conversation turn on an agent that validates draft 2020-12"
    )

    integer_lost = report.get("integer_lost") or []
    assert not integer_lost, (
        f"the round trip through {converter!r} and probatio.to_openapi lost "
        f"integer typing for {integer_lost} ({context}) — the model would be "
        "handed a looser schema than the tool actually accepts"
    )

    draft_invalid = report.get("draft2020_invalid") or []
    assert not draft_invalid, (
        "the re-emitted schema is not valid JSON Schema draft 2020-12 for "
        f"{len(draft_invalid)} tool(s) ({context}):\n" + "\n".join(draft_invalid)
    )


@pytest.mark.embedded_only
def test_llm_api_schemas_survive_core_reemission_container(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Run the probe in the embedded lane's container and assert its report.

    The session backend of this lane IS the in-process server, so the probe
    talks to the same server the rest of the suite drives — through the
    component's own ``_mcp_session``, with Home Assistant's own interpreter,
    converter and probatio.
    """
    info = ha_container_with_fresh_config
    container = info["container"]
    assert container is not None, (
        "the embedded lane must expose its testcontainer handle; got "
        f"container=None with backend={info.get('backend')!r}"
    )

    result = container.get_wrapped_container().exec_run(
        ["python3", "-c", LLM_API_SCHEMA_PROBE],
        environment={PROBE_URL_ENV: _CONTAINER_WEBHOOK_URL},
    )
    output = (result.output or b"").decode("utf-8", "replace")
    assert result.exit_code == 0, (
        "the in-HA LLM-API probe exited "
        f"{result.exit_code} instead of completing:\n{output[-4000:]}"
    )
    # Registration first: it is the cheaper, lane-plumbing half, and it must
    # keep being exercised while the report assertion is expectedly red on a
    # Core that carries the #2361 defect.
    _assert_registration_logged_in_container(Path(info["config_path"]))
    _assert_report_clean(parse_probe_report(output))


def _assert_registration_logged_in_container(config_path: Path) -> None:
    """The component registered the toolset as an LLM API inside this HA.

    The probe proves the schemas convert; this proves Home Assistant was
    actually handed the API. Polled: registration runs right after webhook
    bring-up, so it can trail the readiness gate by a few seconds.
    """
    log_file = config_path / "home-assistant.log"
    deadline = time.monotonic() + _REGISTRATION_TIMEOUT_S
    log_text = ""
    while time.monotonic() < deadline:
        if log_file.exists():
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
            if _REGISTRATION_LINE in log_text:
                return
        time.sleep(_REGISTRATION_POLL_S)
    raise AssertionError(
        f"{_REGISTRATION_LINE!r} never appeared in {log_file} within "
        f"{_REGISTRATION_TIMEOUT_S}s, so the toolset was never registered as "
        f"an LLM API inside this Home Assistant; tail:\n{log_text[-3000:]}"
    )


@pytest.mark.haos_embedded_only
def test_llm_api_schemas_survive_core_reemission_haos(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """The same probe, in the HAOS core container, on the real HAOS image.

    Uses the session fixture rather than ``test_embedded_server_haos.py``'s
    ``embedded_server`` fixture: that module is ``not_on_haos_embedded``
    precisely because this lane's session backend already enables the entry and
    waits for the webhook, and re-running its enable step here would
    double-enable the entry and race that backend.
    """
    info = ha_container_with_fresh_config
    assert info.get("embedded_webhook_url"), (
        "the haos_embedded lane must expose embedded_webhook_url; got "
        f"{info.get('embedded_webhook_url')!r} with "
        f"backend={info.get('backend')!r}"
    )

    try:
        result = ssh_exec(
            [
                "docker",
                "exec",
                "-e",
                f"{PROBE_URL_ENV}={_HAOS_WEBHOOK_URL}",
                _HAOS_CORE_CONTAINER,
                "python3",
                "-c",
                LLM_API_SCHEMA_PROBE,
            ],
            timeout=_PROBE_EXEC_TIMEOUT_S,
        )
    except RuntimeError as err:
        # ssh_exec runs with check=True and re-raises a failed remote command
        # as RuntimeError carrying stdout+stderr.
        raise AssertionError(f"the in-HA LLM-API probe failed in HAOS:\n{err}") from err

    # Same order as the container test: registration plumbing first.
    _assert_registration_logged_in_haos()
    _assert_report_clean(parse_probe_report(result.stdout))


def _assert_registration_logged_in_haos() -> None:
    """The HAOS-side counterpart of the container registration assertion.

    Reads ``home-assistant.log`` inside the Core container, the same file the
    container lane reads through its bind mount. ``ha core logs`` was tried
    first and does not carry the line: it serves a bounded tail of the Core
    journal, and by the time this test runs the boot-time registration line
    is outside that window. The ``|| true`` keeps grep's no-match exit status
    from tripping ssh_exec's ``check=True``.
    """
    script = (
        f"docker exec {_HAOS_CORE_CONTAINER} sh -c "
        f"\"grep -c '{_REGISTRATION_LINE}' /config/home-assistant.log 2>/dev/null"
        ' || true"'
    )
    deadline = time.monotonic() + _REGISTRATION_TIMEOUT_S
    count = ""
    while True:
        # Bound each ssh call by what is left of the deadline (with a small
        # floor so a near-expired deadline still gets one real attempt), so a
        # stalled call cannot outlive the deadline it polls for.
        remaining = max(5.0, deadline - time.monotonic())
        count = ssh_exec(["sh", "-c", script], timeout=remaining).stdout.strip()
        if count.isdigit() and int(count) > 0:
            return
        if time.monotonic() >= deadline:
            break
        LOG.debug("LLM-API registration line not in Core's log yet (count=%r)", count)
        time.sleep(_REGISTRATION_POLL_S)
    raise AssertionError(
        f"{_REGISTRATION_LINE!r} never appeared in /config/home-assistant.log "
        f"inside the {_HAOS_CORE_CONTAINER} container within "
        f"{_REGISTRATION_TIMEOUT_S}s (grep count: {count!r}), so the toolset "
        "was never registered as an LLM API inside this Home Assistant"
    )
