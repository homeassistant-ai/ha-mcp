"""The LLM API's schema conversion, proved INSIDE Home Assistant (issue #2361).

``test_embedded_server.py::test_llm_api_client_path_full_catalog`` used to
convert every schema in the pytest process against the test host's
``voluptuous_openapi``; that loop was removed because it proved nothing about
Home Assistant. HA resolves its own converter (``probatio.from_openapi`` since
Core 2026.9 unless the component's manifest requirement restores
``voluptuous_openapi``) and, before a conversation turn reaches the model,
re-emits every converted schema through ``probatio.to_openapi``. That round
trip is invisible from the host and is exactly where the reported defect lived:
an exclusive bound came back in the draft-4 ``{"minimum": 0,
"exclusiveMinimum": true}`` spelling, which is not valid JSON Schema draft
2020-12, and every Anthropic turn failed.

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
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from haos_runtime import HA_MCP_SERVER_WEBHOOK_ID, ssh_exec

from ...conftest import _EMBEDDED_WEBHOOK_ID
from ...utilities.llm_api_probe import (
    LLM_API_SCHEMA_PROBE,
    PROBE_SENTINEL,
    PROBE_URL_ENV,
    assert_report_clean,
    parse_probe_report,
)

LOG = logging.getLogger(__name__)

# The component logs this on a successful ``llm.async_register_api`` (llm_api.py
# keeps the prefix stable for exactly these assertions).
_REGISTRATION_LINE = "Registered the HA-MCP toolset as LLM API"
_REGISTRATION_TIMEOUT_S = 60
_REGISTRATION_POLL_S = 2

# The probe's own budget is 120s (plus a 5s alarm margin). Both callers pass
# this larger wrapper timeout, the docker client on the container lane and
# ssh_exec on HAOS, so a hang surfaces as the probe's own partial report rather
# than as an opaque kill by the wrapper.
_PROBE_EXEC_TIMEOUT_S = 180

# Home Assistant answers its own webhook on loopback from inside the container /
# VM; the host-facing URL in ``container_info`` is not reachable from there.
_CONTAINER_WEBHOOK_URL = f"http://127.0.0.1:8123/api/webhook/{_EMBEDDED_WEBHOOK_ID}"
_HAOS_WEBHOOK_URL = f"http://127.0.0.1:8123/api/webhook/{HA_MCP_SERVER_WEBHOOK_ID}"

# The HAOS core container, addressed the same way the other HAOS tests address
# it (``docker restart homeassistant`` in test_zz_reentrant_log_deadlock.py).
_HAOS_CORE_CONTAINER = "homeassistant"
# Journal window for the HAOS registration check: with the component's logger
# at INFO, a full lane run logs a few thousand Core lines, so this reaches
# back to boot with margin.
_HAOS_LOG_WINDOW_LINES = 20000


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

    # Registration first: it is the cheaper, lane-plumbing half, and it must
    # keep being exercised while the report assertion is expectedly red on a
    # Core that carries the #2361 defect.
    _assert_registration_logged_in_container(Path(info["config_path"]))

    # A client with an explicit timeout above the probe's own budget: the
    # testcontainers handle's client keeps docker-py's 60s default, which sits
    # BELOW the probe's 120s and would kill a slow probe with no report.
    import docker

    client = docker.from_env(timeout=_PROBE_EXEC_TIMEOUT_S)
    result = client.containers.get(container.get_wrapped_container().id).exec_run(
        ["python3", "-c", LLM_API_SCHEMA_PROBE],
        environment={PROBE_URL_ENV: _CONTAINER_WEBHOOK_URL},
    )
    output = (result.output or b"").decode("utf-8", "replace")
    # Report first, exit code second: a timed-out probe exits non-zero AFTER
    # printing a partial report, and that report is the better diagnosis.
    # parse_probe_report raises with the raw output when there is none.
    assert_report_clean(parse_probe_report(output))
    assert result.exit_code == 0, (
        "the in-HA LLM-API probe exited "
        f"{result.exit_code} despite a clean report:\n{output[-4000:]}"
    )


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

    # Same order as the container test: registration plumbing first.
    _assert_registration_logged_in_haos()

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
        # as RuntimeError carrying stdout+stderr. A timed-out probe exits
        # non-zero after printing its partial report, which is the better
        # diagnosis, so parse it out of the error text when it is there.
        text = str(err)
        if PROBE_SENTINEL in text:
            assert_report_clean(parse_probe_report(text))
        raise AssertionError(f"the in-HA LLM-API probe failed in HAOS:\n{err}") from err
    except subprocess.TimeoutExpired as err:
        stdout = (
            err.stdout.decode("utf-8", "replace")
            if isinstance(err.stdout, bytes)
            else err.stdout
        )
        raise AssertionError(
            f"the in-HA LLM-API probe did not return within {_PROBE_EXEC_TIMEOUT_S}s "
            f"over ssh; captured stdout:\n{(stdout or '')[-4000:]}"
        ) from err

    assert_report_clean(parse_probe_report(result.stdout))


def _assert_registration_logged_in_haos() -> None:
    """The HAOS-side counterpart of the container registration assertion.

    Core disables its file log when it runs under the Supervisor
    (``LOG_FILE_DISABLED_REASON_SUPERVISOR`` in bootstrap.py), so there is no
    ``home-assistant.log`` to read on HAOS and the Core journal is the only
    source. ``ha core logs`` alone serves a bounded tail that no longer holds
    the boot-time registration line by the time this test runs, so ask for a
    window large enough to reach back to boot (``--lines`` becomes a Range
    header on the Supervisor's logs endpoint).
    """
    # Only grep's own no-match status (1) is tolerated; a failing
    # ``ha core logs`` propagates as a non-zero exit so ssh_exec raises with
    # its stderr instead of the poll reading it as "0 matches".
    script = (
        f"out=$(ha core logs --lines {_HAOS_LOG_WINDOW_LINES}) || exit $?; "
        f"printf '%s\\n' \"$out\" | grep -c '{_REGISTRATION_LINE}'; "
        "rc=$?; [ $rc -le 1 ] || exit $rc"
    )
    deadline = time.monotonic() + _REGISTRATION_TIMEOUT_S
    count = ""
    while True:
        # Bound each ssh call by what is left of the deadline (with a small
        # floor so a near-expired deadline still gets one real attempt), so a
        # stalled call cannot outlive the deadline it polls for.
        remaining = max(5.0, deadline - time.monotonic())
        try:
            count = ssh_exec(["sh", "-c", script], timeout=remaining).stdout.strip()
        except RuntimeError as err:
            raise AssertionError(
                f"could not read Core's journal on HAOS while checking for "
                f"{_REGISTRATION_LINE!r}:\n{err}"
            ) from err
        except subprocess.TimeoutExpired as err:
            raise AssertionError(
                f"reading Core's journal on HAOS did not return within {remaining:.0f}s "
                f"while checking for {_REGISTRATION_LINE!r}; captured stdout: "
                f"{err.stdout!r}"
            ) from err
        if count.isdigit() and int(count) > 0:
            return
        if time.monotonic() >= deadline:
            break
        LOG.debug("LLM-API registration line not in Core's log yet (count=%r)", count)
        time.sleep(_REGISTRATION_POLL_S)
    assert count.isdigit(), (
        "the journal command on HAOS did not print a bare match count "
        f"({count!r}); the registration check could not run, which says "
        "nothing about whether the toolset was registered"
    )
    raise AssertionError(
        f"{_REGISTRATION_LINE!r} never appeared in the last "
        f"{_HAOS_LOG_WINDOW_LINES} Core journal lines within "
        f"{_REGISTRATION_TIMEOUT_S}s (grep count: {count!r}), so the toolset "
        "was never registered as an LLM API inside this Home Assistant"
    )
