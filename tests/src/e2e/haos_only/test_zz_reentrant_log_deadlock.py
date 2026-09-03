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

pytestmark = [pytest.mark.system, pytest.mark.slow]

PROBE_DOMAIN = "reentrant_log_probe"

# How long Home Assistant gets to come back after the probe restart. A healthy
# boot on the runner is ~60-120s; the pre-fix deadlock never returns, so this is
# the wait that turns a freeze into a failure rather than an infinite hang.
HA_RETURN_TIMEOUT_S = 300.0

# Candidate mounts for HA's config dir inside the Advanced SSH addon container.
CONFIG_DIR_CANDIDATES = (
    "/homeassistant",
    "/config",
    "/mnt/data/supervisor/homeassistant",
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

PROBE_YAML = """
# --- ha-mcp #2357 probe (removed by the test) ---
reentrant_log_probe:
logger:
  logs:
    custom_components.reentrant_log_probe: debug
# --- end ha-mcp #2357 probe ---
"""


def _sh(script: str, *, timeout: float = 60.0) -> str:
    """Run a shell script inside the VM via the Advanced SSH addon."""
    result = ssh_exec(["sh", "-c", script], timeout=timeout)
    if result.returncode != 0:
        raise AssertionError(
            f"in-VM command failed ({result.returncode}): {script}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout


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


def _restart_core() -> None:
    """Restart HA Core from inside the VM.

    Driven through the Supervisor CLI rather than HA's own restart service so
    it still works when Core's event loop is wedged — which is exactly the
    state this test's cleanup has to recover from. Falls back to restarting the
    container directly, and never raises: the caller polls for readiness itself,
    and this runs in a ``finally`` where an exception would mask the real
    failure.
    """
    try:
        result = ssh_exec(["sh", "-c", "ha core restart"], timeout=300.0)
        if result.returncode == 0:
            return
        logger.warning(
            "`ha core restart` exited %s (%s); restarting the container",
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
    except Exception as exc:
        logger.warning("`ha core restart` failed (%s); restarting the container", exc)
    try:
        ssh_exec(["sh", "-c", "docker restart homeassistant"], timeout=300.0)
    except Exception as exc:
        logger.error("container restart also failed: %s", exc)


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
                "docker run --rm --pid host --privileged python:3.13 sh -c "
                "'pip install -q py-spy && py-spy dump --pid "
                "$(docker inspect --format {{.State.Pid}} homeassistant)'",
            ],
            timeout=420.0,
        )
        return result.stdout or result.stderr
    except Exception as exc:
        return f"(py-spy dump unavailable: {type(exc).__name__}: {exc})"


@pytest.mark.timeout(1800)
def test_reentrant_debug_log_does_not_freeze_home_assistant(
    ha_container_with_fresh_config,
):
    """HA must survive an integration whose debug logging re-enters logging.

    Fails by TIMEOUT on the pre-fix handler: Core never answers again, which is
    the reported #2357 symptom (port 8123 dead, recorder silent, no log line).
    """
    container = ha_container_with_fresh_config
    if container.get("backend") != "haos_embedded":
        pytest.skip(
            "ha_mcp's log handler only shares a process (and an event loop) "
            "with Home Assistant on the embedded lane"
        )

    base_url = container["base_url"]
    ready_url = f"{base_url}/manifest.json"
    config_dir = _find_config_dir()
    probe_dir = f"{config_dir}/custom_components/{PROBE_DOMAIN}"
    config_yaml = f"{config_dir}/configuration.yaml"
    backup_yaml = f"{config_dir}/configuration.yaml.2357.bak"

    existing = _sh(f"grep -c '^logger:' {shlex.quote(config_yaml)} || true").strip()
    assert existing in ("", "0"), (
        "the seeded configuration.yaml already declares `logger:` — appending a "
        "second block would be a duplicate YAML key. Merge the probe's logger "
        "entry into the existing block instead."
    )

    try:
        _sh(f"cp {shlex.quote(config_yaml)} {shlex.quote(backup_yaml)}")
        _write_in_vm(f"{probe_dir}/__init__.py", PROBE_INIT_PY)
        _write_in_vm(f"{probe_dir}/manifest.json", PROBE_MANIFEST)
        _write_in_vm(config_yaml, _sh(f"cat {shlex.quote(backup_yaml)}") + PROBE_YAML)

        logger.info("Restarting Core with the re-entrant log probe installed")
        restarted_at = time.monotonic()
        _restart_core()

        try:
            _wait_http_ok(ready_url, timeout=HA_RETURN_TIMEOUT_S)
        except TimeoutError as exc:
            raise AssertionError(
                "Home Assistant never came back after a re-entrant debug log "
                f"record ({HA_RETURN_TIMEOUT_S:.0f}s) — the event loop is "
                f"frozen inside logging (#2357).\n\n{exc}\n\n"
                f"py-spy dump of the frozen process:\n{_py_spy_dump()}"
            ) from exc

        # Stay past the collector's 60s window so the probe (firing every 2s
        # from setup) is guaranteed to have been formatted by it while it was
        # active, and confirm HA is still answering afterwards.
        while time.monotonic() - restarted_at < 120.0:
            time.sleep(5.0)
        _wait_http_ok(ready_url, timeout=60.0)

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
    finally:
        _sh(
            f"rm -rf {shlex.quote(probe_dir)}; "
            f"[ -f {shlex.quote(backup_yaml)} ] && "
            f"mv {shlex.quote(backup_yaml)} {shlex.quote(config_yaml)} || true"
        )
        # Hand a healthy VM back to the rest of the worker's session — the
        # Supervisor CLI restarts Core even when its loop is wedged.
        _restart_core()
        try:
            _wait_http_ok(ready_url, timeout=HA_RETURN_TIMEOUT_S)
        except TimeoutError:
            logger.error("Home Assistant did not recover after probe cleanup")
