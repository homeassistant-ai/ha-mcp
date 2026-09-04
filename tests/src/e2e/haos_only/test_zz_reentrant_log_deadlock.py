"""Live proof for #2357: a re-entrant log record must not freeze Home Assistant.

``StartupLogCollector`` sits on the ROOT logger at DEBUG for the first minute of
the process. In embedded mode that process IS Home Assistant, so every debug
record from every integration is formatted on the event loop. Formatting runs
third-party code — ``%s`` of an entity reaches its ``state`` property — and an
integration whose property logs at debug re-enters the handler on the same
thread. Before the fix that re-entry blocked forever on a non-reentrant
``threading.Lock`` held by the outer frame: the entire HA process froze, with no
log line to show for it (the freeze is inside logging itself).

The unit coverage in ``tests/src/unit/test_startup_log_collector.py`` pins the
handler's behaviour. This test pins the consequence: a probe integration that
reproduces the reporter's shape (tapo_control's ``latest_version`` property
logging at debug, reached via an entity repr) is installed into the real HAOS
VM, Core is restarted so the collector's window reopens, and Home Assistant is
required to come back.

Run it against the pre-fix handler and HA never returns — that is the whole
point of the test, and the reason it restores the config and restarts Core in a
``finally`` no matter how it ends.
"""

import logging
import shlex
import time
from base64 import b64encode
from posixpath import dirname

import pytest
from haos_runtime import _wait_http_ok, ssh_exec

logger = logging.getLogger(__name__)

# ``haos_embedded_only``: ha_mcp's log handler only shares a process (and an
# event loop) with Home Assistant on the embedded lane. Gated at collection so
# the other HAOS lanes do not boot a VM just to skip this module.
pytestmark = [pytest.mark.haos_embedded_only, pytest.mark.system, pytest.mark.slow]

PROBE_DOMAIN = "reentrant_log_probe"

# How long Home Assistant gets to come back after the probe restart. A healthy
# boot on the runner is ~60-120s; the pre-fix deadlock never returns, so this is
# the wait that turns a freeze into a failure rather than an infinite hang.
HA_RETURN_TIMEOUT_S = 300.0

# How long to keep the probe firing after HA answers again, and how long HA then
# gets to prove it is still alive. The freeze does not have to happen during
# startup: the reported instance served requests first and locked up later.
SOAK_SECONDS = 120.0
SOAK_RECHECK_TIMEOUT_S = 60.0

# Candidate mounts for HA's config dir inside the Advanced SSH addon container.
CONFIG_DIR_CANDIDATES = (
    "/homeassistant",
    "/config",
    "/mnt/data/supervisor/homeassistant",
)

# The reporter's recipe: a throwaway python container sharing the host pid
# namespace dumps every thread of the frozen Core process.
PY_SPY_DUMP_COMMAND = (
    "docker run --rm --pid host --privileged python:3.13 sh -c "
    + "'pip install -q py-spy && py-spy dump --pid "
    + "$(docker inspect --format {{.State.Pid}} homeassistant)'"
)

PROBE_INIT_PY = '''\
"""Probe integration for ha-mcp issue #2357 (test fixture, not shipped).

Mirrors the reported chain: a debug record whose formatting reaches a property
that itself logs at debug. In the real report the outer record came from
tapo_control's coordinator and the inner one from its ``latest_version``
property, reached through ``Entity.__repr__`` -> ``_stringify_state`` ->
``state``. The shape is what matters, not the integration.
"""

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

DOMAIN = "reentrant_log_probe"

_LOGGER = logging.getLogger(__name__)


class _LogsOnRepr:
    """Stands in for an entity whose repr reaches a property that logs."""

    def __repr__(self) -> str:
        _LOGGER.debug("reentrant inner record")
        return "<probe-entity>"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Emit the re-entrant record on the event loop, repeatedly.

    Repeatedly, not once: ha_mcp's startup collector only formats records for
    the first 60 seconds after ITS module import, and component setup order
    between this probe and the embedded server is not guaranteed. Firing every
    two seconds guarantees at least one record lands while that window is open.
    """

    async def _fire(_now=None) -> None:
        _LOGGER.debug("reentrant outer record %s", _LogsOnRepr())

    await _fire()
    async_track_time_interval(hass, _fire, timedelta(seconds=2))
    return True
'''

PROBE_MANIFEST = """\
{
  "domain": "reentrant_log_probe",
  "name": "Reentrant Log Probe",
  "version": "1.0.0",
  "documentation": "https://github.com/homeassistant-ai/ha-mcp/issues/2357",
  "codeowners": [],
  "dependencies": [],
  "iot_class": "calculated"
}
"""

PROBE_COMPONENT_YAML = """
# --- ha-mcp #2357 probe (removed by the test) ---
reentrant_log_probe:
# --- end ha-mcp #2357 probe ---
"""
PROBE_LOGGER_ENTRY = (
    "    custom_components.reentrant_log_probe: debug  # ha-mcp #2357 probe\n"
)


def _with_probe_config(existing: str) -> str:
    """Return ``existing`` with the probe's component key and debug logger.

    The seeded configuration.yaml already carries a ``logger:`` block (it
    raises the ha_mcp_tools component to INFO for test_llm_api_in_ha.py), and
    a second top-level ``logger:`` would be a duplicate YAML key, so the
    probe's entry is merged under that block's ``logs:`` mapping. A seed
    without the block gets a whole one appended.
    """
    lines = existing.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("logger:"):
            continue
        for inner in range(index + 1, len(lines)):
            if lines[inner].startswith("  logs:"):
                lines.insert(inner + 1, PROBE_LOGGER_ENTRY)
                return "".join(lines) + PROBE_COMPONENT_YAML
            if lines[inner].strip() and not lines[inner].startswith((" ", "#")):
                break
        raise AssertionError(
            "the seeded `logger:` block has no `logs:` mapping to merge the "
            "probe's debug entry into"
        )
    return existing + PROBE_COMPONENT_YAML + "logger:\n  logs:\n" + PROBE_LOGGER_ENTRY


def _sh(script: str, *, timeout: float = 60.0) -> str:
    """Run a shell script inside the VM via the Advanced SSH addon.

    ``ssh_exec`` runs with ``check=True`` and re-raises a failed remote command
    as ``RuntimeError`` (carrying stdout+stderr); it never returns a non-zero
    exit. Normalised to ``AssertionError`` here so the test body and its
    cleanup handle one exception type.
    """
    try:
        return ssh_exec(["sh", "-c", script], timeout=timeout).stdout
    except RuntimeError as exc:
        raise AssertionError(f"in-VM command failed: {script}\n{exc}") from exc


def _write_in_vm(path: str, content: str) -> None:
    """Write ``content`` to ``path`` inside the VM.

    Base64 rather than a heredoc: ssh_exec joins the command into a single
    shell string, so anything with quotes or newlines has to survive two
    rounds of tokenisation.
    """
    encoded = b64encode(content.encode()).decode()
    _sh(
        f"mkdir -p {shlex.quote(dirname(path))} && "
        f"echo {encoded} | base64 -d > {shlex.quote(path)}"
    )


def _find_config_dir() -> str:
    for candidate in CONFIG_DIR_CANDIDATES:
        found = _sh(
            f"[ -f {shlex.quote(candidate)}/configuration.yaml ] "
            f"&& echo {shlex.quote(candidate)} || true"
        ).strip()
        if found:
            return found
    raise AssertionError(
        "could not locate HA's configuration.yaml inside the VM; tried "
        f"{', '.join(CONFIG_DIR_CANDIDATES)}"
    )


def _restart_core() -> bool:
    """Restart HA Core from inside the VM; True if a restart was issued.

    Driven through the Supervisor CLI rather than HA's own restart service so
    it still works when Core's event loop is wedged — which is exactly the
    state this test's cleanup has to recover from. Falls back to restarting the
    container directly, and never raises: this runs in a ``finally`` where an
    exception would mask the real failure. The caller must not treat a later
    readiness check as proof of a restart — the OLD process answers it just as
    well — so a double failure is reported through the return value.
    """
    try:
        result = ssh_exec(["sh", "-c", "ha core restart"], timeout=300.0)
        if result.returncode == 0:
            return True
        logger.warning(
            "`ha core restart` exited %s (%s); restarting the container",
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
    except Exception as exc:
        logger.warning("`ha core restart` failed (%s); restarting the container", exc)
    try:
        result = ssh_exec(["sh", "-c", "docker restart homeassistant"], timeout=300.0)
    except Exception as exc:
        logger.error("container restart also failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.error(
            "container restart also failed (%s): %s",
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return False
    return True


def _py_spy_dump() -> str:
    """Best-effort in-VM py-spy dump of the frozen Core process.

    The same recipe the reporter used on #2357. Purely diagnostic: if the image
    pull or the dump fails, the test failure stands on its own.
    """
    try:
        result = ssh_exec(
            [
                "sh",
                "-c",
                PY_SPY_DUMP_COMMAND,
            ],
            timeout=420.0,
        )
        return result.stdout or result.stderr
    except Exception as exc:
        return f"(py-spy dump unavailable: {type(exc).__name__}: {exc})"


def _require_ha_responsive(ready_url: str, timeout: float, what: str) -> None:
    """Poll HA, and on timeout fail with an in-VM stack dump of the freeze.

    Both readiness checks in this test go through here: the pre-fix handler can
    freeze the loop either during startup or a few records into normal running,
    and either way the useful evidence is the stack of the blocked thread.
    """
    try:
        _wait_http_ok(ready_url, timeout=timeout)
    except TimeoutError as exc:
        raise AssertionError(
            f"{what} ({timeout:.0f}s) — the event loop is frozen inside "
            f"logging (#2357).\n\n{exc}\n\n"
            f"py-spy dump of the frozen process:\n{_py_spy_dump()}"
        ) from exc


@pytest.mark.timeout(1800)
def test_reentrant_debug_log_does_not_freeze_home_assistant(
    ha_container_with_fresh_config,
):
    """HA must survive an integration whose debug logging re-enters logging.

    Fails by TIMEOUT on the pre-fix handler: Core never answers again, which is
    the reported #2357 symptom (port 8123 dead, recorder silent, no log line).
    """
    base_url = ha_container_with_fresh_config["base_url"]
    ready_url = f"{base_url}/manifest.json"
    config_dir = _find_config_dir()
    probe_dir = f"{config_dir}/custom_components/{PROBE_DOMAIN}"
    config_yaml = f"{config_dir}/configuration.yaml"
    backup_yaml = f"{config_dir}/configuration.yaml.2357.bak"

    body_failed = False
    try:
        _sh(f"cp {shlex.quote(config_yaml)} {shlex.quote(backup_yaml)}")
        _write_in_vm(f"{probe_dir}/__init__.py", PROBE_INIT_PY)
        _write_in_vm(f"{probe_dir}/manifest.json", PROBE_MANIFEST)
        _write_in_vm(
            config_yaml, _with_probe_config(_sh(f"cat {shlex.quote(backup_yaml)}"))
        )

        logger.info("Restarting Core with the re-entrant log probe installed")
        assert _restart_core(), "could not restart Core to load the probe"

        _require_ha_responsive(
            ready_url,
            HA_RETURN_TIMEOUT_S,
            "Home Assistant never came back after the probe restart",
        )
        # The soak is measured from readiness, not from the restart: a slow
        # boot must not eat the window in which continued responsiveness is
        # what is being checked.
        ready_at = time.monotonic()

        # Stay past the collector's 60s window so the probe (firing every 2s
        # from setup) is guaranteed to have been formatted by it while it was
        # active, then confirm HA is STILL answering. Both checks matter: the
        # pre-fix handler can let the boot complete and freeze the loop a few
        # records later, which is what the reported instance did.
        while time.monotonic() - ready_at < SOAK_SECONDS:
            time.sleep(5.0)
        _require_ha_responsive(
            ready_url,
            SOAK_RECHECK_TIMEOUT_S,
            "Home Assistant stopped answering during the probe soak",
        )

        # Guard against a vacuous pass: if the probe never logged, the test
        # proved nothing about re-entrant records.
        fired = _sh(
            "ha core logs 2>/dev/null | grep -c 'reentrant outer record' || true",
            timeout=180.0,
        ).strip()
        assert fired.isdigit() and int(fired) > 0, (
            "the probe integration never emitted its debug record, so this run "
            f"did not exercise the re-entrant path (grep count: {fired!r})"
        )
    except BaseException:
        body_failed = True
        raise
    finally:
        # Hand a healthy VM back to the rest of the worker's session. Later
        # modules on this xdist worker share it, so a restore that leaves the
        # probe config behind or a restart that leaves Core down is a failure
        # of this test — unless the body already failed, in which case that
        # failure must stay the one reported.
        # Each step is attempted regardless of the previous one — a failed
        # probe removal must not skip the config restore, and vice versa — and
        # the first failure is what gets reported.
        cleanup_error: Exception | None = None
        for step in (
            f"rm -rf {shlex.quote(probe_dir)}",
            # The backup exists from the moment the body's first step ran; a
            # failed mv here must not be papered over.
            f"if [ -f {shlex.quote(backup_yaml)} ]; then "
            f"mv {shlex.quote(backup_yaml)} {shlex.quote(config_yaml)}; fi",
        ):
            try:
                _sh(step)
            except AssertionError as exc:
                cleanup_error = cleanup_error or exc
        # The Supervisor CLI restarts Core even when its loop is wedged. A
        # restart that never happened leaves the probe loaded even though the
        # old process still answers the readiness check below.
        if not _restart_core():
            cleanup_error = cleanup_error or AssertionError(
                "Core restart failed during cleanup; the probe is still loaded"
            )
        try:
            _wait_http_ok(ready_url, timeout=HA_RETURN_TIMEOUT_S)
        except TimeoutError as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            if body_failed:
                logger.error("probe cleanup did not fully recover: %s", cleanup_error)
            else:
                raise AssertionError(
                    "probe cleanup did not fully recover the worker's VM, which "
                    f"later modules share: {cleanup_error}"
                ) from cleanup_error
