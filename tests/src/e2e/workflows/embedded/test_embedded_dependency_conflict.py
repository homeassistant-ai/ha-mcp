"""End-to-end reproduction of the #2239 dependency-downgrade incident.

A third-party custom integration pins ``mcp==1.14.1`` in its manifest. Home
Assistant reinstalls that pin into the one shared site-packages tree at every
startup, dropping ``mcp`` below the floor fastmcp declares — and the in-process
server's first ``ha_mcp`` import then dies on a name ``mcp.types`` no longer
exports. fastmcp swallows that ``ImportError`` and re-raises the generic hint
"FastMCP server support is not installed", which names neither the package, nor
the version, nor the integration that moved it.

This test boots a Home Assistant container in exactly that state and asserts
the two surfaces the fix adds both name the whole causal chain:

* the pre-flight audit's WARNING, logged between the install step and the
  worker's first import, and
* the bring-up failure detail — the string
  ``embedded_setup.async_bring_up_server`` logs at ERROR *and* hands to the
  ``server_package_install_failed`` repair issue as its ``{detail}``
  placeholder, so asserting the log asserts the repair issue's text too.

Why a dedicated, module-scoped container (like its two neighbours): the
scenario deliberately corrupts the container's Python environment, and the
session container is the server-under-test for every other test on the embedded
lane.

**How the corruption is staged, and why it survives.** The wrapped entrypoint
installs the checkout's wheel plus its whole dependency tree, then downgrades
``mcp`` alone (``--no-deps``, so nothing else in the tree moves) before HA's
``/init``. The seeded config entry then pins ``pip_spec`` to the exact version
that wheel installed and stores the same spec as ``last_pip_spec``, which is
what puts ``_async_ensure_package`` on its FAST path: an unchanged, non-URL,
already-satisfied index spec is handed to HA's requirements manager, which
runs no pip at all. That is the production shape of this incident — a
force-install would re-resolve the graph and quietly repair ``mcp``, which is
precisely why the bug only bites installs that have nothing left to install.
``test_the_pinned_downgrade_survived_bring_up`` re-reads the version afterwards
so a future change to that branch surfaces as "the repro premise broke" rather
than as a mystifying assertion on a message that was never produced.

The fake integration is a manifest (and an inert ``__init__.py``) with no
config entry and no ``configuration.yaml`` entry, so Home Assistant never sets
it up and never runs its requirement itself. The entrypoint downgrade stands in
for that boot-time reinstall: same end state, no network dependency on HA's
requirements manager, no ordering race against the background bring-up.
``find_pinning_integrations`` reads ``custom_components/*/manifest.json``
directly, so a manifest is the whole fixture its scan needs.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
import requests
from test_constants import HA_TEST_IMAGE, TEST_TOKEN

# ``container_only``: the scenario needs a throwaway HA Core container it can
# corrupt, which the HAOS lanes have no cheap way to provide (and where the
# same code is already covered by the container + embedded lanes). Deliberately
# NOT ``not_on_embedded``: that marker means "provably redundant with the
# embedded lane's own session backend", and this failure path is the one thing
# that backend never exercises — it only ever brings the server up healthy.
#
# NOTE (skip-ceiling coupling): every test in this module skips on the haos,
# haos_stdio, haos_inaddon and haos_embedded lanes, and each such skip counts
# toward ``_SKIP_CEILING_PER_LANE`` in
# tests/src/e2e/basic/test_backend_dispatch_smoke.py. Adding or removing a test
# here moves those four ceilings by the same amount.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.container_only,
]

_DOMAIN = "ha_mcp_tools"
# unique_id of the single-instance server entry (config_flow's _SERVER_UNIQUE_ID).
_UNIQUE_ID = "ha_mcp_tools-server"
_ENTRY_ID = "e2e_dependency_conflict_server_entry"
# Stable webhook id + secret seeded into entry.data so no .storage read-back is
# needed; this module never speaks MCP, but the component requires both present.
_WEBHOOK_ID = "mcp_e2e_dep_conflict_0123456789abcdef"
_SECRET_PATH = "/private_e2e_dependency_conflict_secret"
_SERVER_PORT = 9584
# entry.data key the component stores the last-installed spec under
# (const.DATA_LAST_PIP_SPEC). Seeding it equal to ``pip_spec`` is what selects
# the fast path — see the module docstring.
_DATA_LAST_PIP_SPEC = "last_pip_spec"

# The pin from the incident report. Any version below fastmcp's floor would do;
# this one is the reported value, so the assertions read as the real story.
_PINNED_MCP_VERSION = "1.14.1"
_PINNER_DOMAIN = "fake_pinner"
_PINNER_NAME = "Fake MCP Pinner"

# The entrypoint's pip work (manifest requirements, the ha-mcp wheel + its whole
# dependency tree, the downgrade) all runs BEFORE HA's /init, so /api/ liveness
# is delayed by that whole window — the same reason conftest's embedded backend
# raises its own API budget far above the 60s default.
_API_READY_TIMEOUT_S = 600
# Bring-up is scheduled after CoreState RUNNING and installs nothing (fast
# path), so this covers HA finishing its startup and the worker crashing on its
# first import — not a package download.
_BRINGUP_TIMEOUT_S = 420
_LOG_POLL_S = 3

_REPO_ROOT = Path(__file__).resolve().parents[5]
_INITIAL_STATE = _REPO_ROOT / "tests" / "initial_test_state"
_INTEGRATION_SRC = _REPO_ROOT / "custom_components" / _DOMAIN

# Logged by embedded_setup.async_bring_up_server's ERROR handler. Present in the
# component today independently of the diagnostics under test, so the fixture's
# readiness gate does not depend on the wording this module asserts.
_FAILURE_NEEDLE = "HA-MCP in-process server failed to start"
# Logged by _async_wait_until_ready when the server DOES come up. Polled
# alongside the failure needle so a healed environment fails fast and says so,
# instead of burning the whole bring-up budget on a message that will never come.
_SUCCESS_NEEDLE = "HA-MCP in-process server is listening on"
# Opening clause of _async_warn_on_dependency_conflicts' WARNING.
_PREFLIGHT_NEEDLE = "the installed dependency tree is inconsistent"

_MCP_VERSION_PROBE = """
from importlib.metadata import PackageNotFoundError, version

try:
    print("MCP_VERSION " + version("mcp"))
except PackageNotFoundError:
    print("MCP_VERSION <not-installed>")
"""


def _docker_available() -> bool:
    try:
        import docker as docker_sdk

        docker_sdk.from_env().ping()
        return True
    except Exception:
        return False


def _build_wheel(dest_dir: Path) -> tuple[Path, str]:
    """Build a ha-mcp wheel from the checkout; return ``(path, version)``.

    ``uv build`` rather than ``python -m pip wheel``, for the reason spelled out
    in this module's neighbours: the suite runs in a uv venv that ships no pip,
    so the pip form exits non-zero and would turn this lane green-by-skip.

    The version is read back out of the wheel FILENAME rather than from
    ``pyproject.toml``: it is the version the container will actually have
    installed, and the seeded ``pip_spec`` has to pin exactly that or the fast
    path this scenario depends on is not taken.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dest_dir), str(_REPO_ROOT)],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    wheels = list(dest_dir.glob("ha_mcp-*.whl"))
    if not wheels:
        raise AssertionError(f"no ha_mcp wheel built in {dest_dir}")
    wheel = wheels[0]
    match = re.match(r"^ha_mcp-([^-]+)-", wheel.name)
    if match is None:
        raise AssertionError(f"cannot read a version out of the wheel {wheel.name!r}")
    return wheel, match.group(1)


def _manifest_requirements(config_path: Path) -> list[str]:
    """Every staged custom integration's manifest requirements, minus the pinner's.

    Preinstalled by the entrypoint for the reason conftest's own
    ``_collect_manifest_requirements`` documents: HA's runtime manifest-install
    does not reliably fire for a config entry the fixture pre-injected into
    ``.storage``, and the integration then lands in ``setup_error`` with no
    bring-up at all — which would fail this module for a reason that has
    nothing to do with #2239.

    The fake pinner is excluded by name. Its requirement is the thing under
    test and is staged last, after the wheel, so that nothing installed on its
    behalf here can be undone by the dependency resolution that follows.
    """
    cc_dir = config_path / "custom_components"
    requirements: list[str] = []
    for manifest_path in sorted(cc_dir.glob("*/manifest.json")):
        if manifest_path.parent.name == _PINNER_DOMAIN:
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest.get("requirements", []):
            if isinstance(entry, str) and entry not in requirements:
                requirements.append(entry)
    return requirements


def _entrypoint(config_path: Path, wheel_name: str) -> list[str]:
    """The wrapped entrypoint: install, downgrade ``mcp``, then hand over to /init.

    Every step is ``&&``-chained, so a failure surfaces as a non-zero container
    exit rather than as an HA that boots into a half-staged environment.
    """
    quoted_wheel = shlex.quote(f"/config/{wheel_name}")
    pinned = shlex.quote(f"mcp=={_PINNED_MCP_VERSION}")
    commands: list[str] = []

    requirements = _manifest_requirements(config_path)
    if requirements:
        quoted = " ".join(shlex.quote(entry) for entry in requirements)
        # PyPI-first with a wheels-index fallback, copied from conftest: the
        # image pins PIP_EXTRA_INDEX_URL at wheels.home-assistant.io, and pip
        # stalls through five read-timeout retries per lookup whenever that
        # index is down.
        commands.append(
            f"(env -u PIP_EXTRA_INDEX_URL pip install --no-cache-dir "
            f"--only-binary=:all: {quoted} || pip install --no-cache-dir {quoted})"
        )

    # Resolve the wheel's dependency tree under HA's OWN constraints file, the
    # way a real in-process install does, so the tree this scenario audits is
    # the tree production gets.
    constraints_probe = (
        "HACONS=\"$(python3 -c 'import homeassistant, os; "
        "print(os.path.join(os.path.dirname(homeassistant.__file__), "
        '"package_constraints.txt"))\')"'
    )
    commands.append(
        f"{constraints_probe} && "
        f'if [ -f "$HACONS" ]; then '
        f'pip install --no-cache-dir --constraint "$HACONS" {quoted_wheel}; '
        f"else pip install --no-cache-dir {quoted_wheel}; fi"
    )

    # The downgrade runs WITHOUT HA's constraints file on purpose: a constraint
    # on mcp would veto the very version this scenario needs installed. It also
    # runs with --no-deps, so the only thing that moves in the tree is mcp
    # itself — the incident's shape, and the one variable the assertions name.
    commands.append(
        f"(env -u PIP_EXTRA_INDEX_URL pip install --no-cache-dir --no-deps "
        f"--only-binary=:all: {pinned} "
        f"|| pip install --no-cache-dir --no-deps {pinned})"
    )
    return ["sh", "-c", " && ".join(commands) + " && exec /init"]


def _seed_pinning_integration(config_path: Path) -> None:
    """Drop a custom integration whose manifest pins ``mcp``.

    Never set up by Home Assistant (no config entry, absent from
    ``configuration.yaml``), so its requirement is never installed by HA — the
    entrypoint stages that end state directly. The manifest carries the keys HA
    demands of a custom integration so its startup scan accepts the directory
    rather than logging it as malformed.
    """
    domain_dir = config_path / "custom_components" / _PINNER_DOMAIN
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "manifest.json").write_text(
        json.dumps(
            {
                "domain": _PINNER_DOMAIN,
                "name": _PINNER_NAME,
                "codeowners": [],
                "documentation": "https://example.invalid/fake-pinner",
                "iot_class": "local_polling",
                "requirements": [f"mcp=={_PINNED_MCP_VERSION}"],
                "version": "1.0.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (domain_dir / "__init__.py").write_text(
        '"""Inert stand-in for a third-party integration that pins mcp (#2239)."""\n\n'
        "async def async_setup(hass, config):\n"
        '    """Never called: nothing configures this integration."""\n'
        "    return True\n",
        encoding="utf-8",
    )


def _seed_config(config_path: Path, wheel_name: str, wheel_version: str) -> None:
    """Install the component + seed a server entry pinned to the installed wheel."""
    shutil.copytree(_INITIAL_STATE, config_path, dirs_exist_ok=True)

    dest = config_path / "custom_components" / _DOMAIN
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_INTEGRATION_SRC, dest, dirs_exist_ok=True)
    _seed_pinning_integration(config_path)

    pip_spec = f"ha-mcp=={wheel_version}"
    storage_file = config_path / ".storage" / "core.config_entries"
    data = json.loads(storage_file.read_text())
    entries = data.setdefault("data", {}).setdefault("entries", [])
    entries.append(
        {
            "created_at": "2025-09-07T23:56:28.040744+00:00",
            "data": {
                "entry_type": "server",
                "webhook_id": _WEBHOOK_ID,
                "secret_path": _SECRET_PATH,
                # Equal to the option below: an UNCHANGED spec is half of what
                # puts _async_ensure_package on its no-install fast path.
                _DATA_LAST_PIP_SPEC: pip_spec,
            },
            "disabled_by": None,
            "discovery_keys": {},
            "domain": _DOMAIN,
            "entry_id": _ENTRY_ID,
            "minor_version": 1,
            "modified_at": "2025-09-07T23:56:28.040747+00:00",
            "options": {
                # An exact index pin on the version the entrypoint installed:
                # an explicit override (so it counts as "stable"), not a URL
                # (which always force-installs), and already satisfied (so HA's
                # requirements manager runs no pip).
                "pip_spec": pip_spec,
                "channel": "stable",
                "auto_update": True,
                "server_port": _SERVER_PORT,
                "bind_host": "127.0.0.1",
                "webhook_auth": "none",
            },
            "pref_disable_new_entities": False,
            "pref_disable_polling": False,
            "source": "import",
            "subentries": [],
            "title": "HA-MCP Server",
            "unique_id": _UNIQUE_ID,
            "version": 1,
        }
    )
    storage_file.write_text(json.dumps(data, indent=2))

    # This HA writes only WARNING+ to home-assistant.log without a `logger:`
    # block. The two lines asserted here are WARNING and ERROR, so they would
    # land either way; raising the component to INFO additionally captures the
    # success line the fixture polls for as its "the repro premise is gone"
    # tripwire, and the surrounding bring-up context when something else fails.
    config_yaml = config_path / "configuration.yaml"
    config_yaml.write_text(
        config_yaml.read_text(encoding="utf-8")
        + "\n# e2e: surface the component's INFO lines"
        " (#2239 dependency-conflict e2e).\n"
        "logger:\n  logs:\n    custom_components.ha_mcp_tools: info\n",
        encoding="utf-8",
    )

    # HA runs as uid 0 in the test image but the bind mount must be traversable.
    for path in config_path.rglob("*"):
        try:
            path.chmod(0o777 if path.is_dir() else 0o666)
        except OSError:
            pass  # Best-effort chmod; some testcontainer mounts refuse it.


def _dump_logs(container: Any) -> Any:
    """Best-effort container logs for an assertion message; never raises."""
    logs: Any = ""
    with contextlib.suppress(Exception):
        logs = container.get_logs()
    return logs


def _wait_http_ok(
    url: str, headers: dict[str, str], timeout: int, container: Any
) -> None:
    """Poll ``url`` until it answers 200, or fail with the container's logs.

    The whole entrypoint chain (component requirement, the wheel and its
    dependency tree, the downgrade) runs before HA's ``/init``, and a failure in
    any of them short-circuits to a container that never starts HA at all — so
    the logs are the only place the reason exists.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, headers=headers, timeout=5).status_code == 200:
                return
        except requests.exceptions.RequestException:
            pass  # HA still booting; retry until the deadline.
        time.sleep(2)
    raise AssertionError(
        f"{url} not ready within {timeout}s. The entrypoint installs the wheel "
        "and stages the downgrade before HA boots, so a failure there shows up "
        f"here.\nContainer logs:\n{_dump_logs(container)}"
    )


def _in_container(container: Any, source: str) -> str:
    """Run a ``python3 -c`` probe in the container and return its output."""
    result = container.get_wrapped_container().exec_run(["python3", "-c", source])
    output = (result.output or b"").decode("utf-8", "replace")
    assert result.exit_code == 0, (
        f"in-container probe exited {result.exit_code}:\n{output}"
    )
    return output


def _wait_for_bringup_outcome(log_file: Path, container: Any) -> str:
    """Poll ``home-assistant.log`` until bring-up has failed; return the log text.

    Fails immediately — rather than waiting out the budget — if the server
    comes up instead, because that means the staged environment no longer
    reproduces #2239 and every assertion below would be about a message that
    was never written.
    """
    deadline = time.monotonic() + _BRINGUP_TIMEOUT_S
    log_text = ""
    while time.monotonic() < deadline:
        if log_file.exists():
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
            if _FAILURE_NEEDLE in log_text:
                return log_text
            if _SUCCESS_NEEDLE in log_text:
                raise AssertionError(
                    "the in-process server started successfully, so the #2239 "
                    "repro premise is gone: either the seeded pin no longer "
                    "reaches _async_ensure_package's fast path (something "
                    "reinstalled the dependency tree and repaired mcp) or "
                    f"mcp {_PINNED_MCP_VERSION} no longer breaks the fastmcp "
                    "import. Re-read the fast-path rationale in this module's "
                    f"docstring.\nContainer logs:\n{_dump_logs(container)}"
                )
        time.sleep(_LOG_POLL_S)
    raise AssertionError(
        f"neither a bring-up failure nor a successful start appeared in "
        f"home-assistant.log within {_BRINGUP_TIMEOUT_S}s. Log tail:\n"
        f"{log_text[-4000:]}\nContainer logs:\n{_dump_logs(container)}"
    )


@pytest.fixture(scope="module")
def conflicted_ha():
    """Boot HA with the ha-mcp tree installed and ``mcp`` pinned back to 1.14.1.

    Yields a dict once the in-process server's bring-up has failed, carrying
    the full ``home-assistant.log`` text plus the container handle.
    """
    if not _docker_available():
        pytest.skip("Docker is not available for the dependency-conflict e2e")

    from testcontainers.core.container import DockerContainer

    wheel_dir = Path(tempfile.mkdtemp(prefix="ha_mcp_dep_conflict_wheel_"))
    try:
        wheel, wheel_version = _build_wheel(wheel_dir)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        # FAIL, don't skip: a wheel that cannot be built is a real problem, and
        # skipping is what once hid the pip-less-venv bug on every CI run.
        stderr = (getattr(err, "stderr", "") or "").strip()
        pytest.fail(f"could not build the ha-mcp wheel: {err}\nstderr:\n{stderr}")
    except AssertionError as err:
        pytest.fail(str(err))

    config_path = Path(tempfile.mkdtemp(prefix="ha_mcp_dep_conflict_cfg_"))
    shutil.copy2(wheel, config_path / wheel.name)
    _seed_config(config_path, wheel.name, wheel_version)

    container = (
        DockerContainer(HA_TEST_IMAGE)
        .with_exposed_ports(8123)
        .with_volume_mapping(str(config_path), "/config", "rw")
        .with_env("TZ", "UTC")
        .with_kwargs(entrypoint=_entrypoint(config_path, wheel.name))
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8123)
        base_url = f"http://{host}:{port}"
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        _wait_http_ok(f"{base_url}/api/", headers, _API_READY_TIMEOUT_S, container)
        log_text = _wait_for_bringup_outcome(
            config_path / "home-assistant.log", container
        )
        yield {"container": container, "log_text": log_text}
    finally:
        with contextlib.suppress(Exception):
            container.stop()
        # The container is stopped, so the /config bind mount is released.
        shutil.rmtree(config_path, ignore_errors=True)
        shutil.rmtree(wheel_dir, ignore_errors=True)


def _failure_detail(log_text: str) -> str:
    """The bring-up failure log line, i.e. the repair issue's ``{detail}``.

    ``embedded_setup.async_bring_up_server`` logs ``str(err)`` and hands the
    SAME string to ``_create_issue`` as the ``{detail}`` translation
    placeholder, so this one line is both surfaces at once.
    """
    lines = [line for line in log_text.splitlines() if _FAILURE_NEEDLE in line]
    assert lines, f"no bring-up failure line in the log; tail:\n{log_text[-4000:]}"
    return lines[-1]


class TestEmbeddedDependencyConflict:
    def test_the_pinned_downgrade_survived_bring_up(self, conflicted_ha):
        """``mcp`` is still at the pinned version after bring-up ran.

        The premise every other assertion rests on. If a future change puts
        bring-up back on a force-installing path, the resolver repairs ``mcp``
        on the way past and the conflict this module is about never exists —
        which must read as "the scenario stopped reproducing", not as a
        confusing failure about missing message text.
        """
        output = _in_container(conflicted_ha["container"], _MCP_VERSION_PROBE)
        assert f"MCP_VERSION {_PINNED_MCP_VERSION}" in output, (
            f"expected mcp {_PINNED_MCP_VERSION} to still be installed after "
            f"bring-up, got {output.strip()!r} — something reinstalled the "
            "dependency tree, so this module's environment no longer "
            "reproduces #2239 (see the fast-path rationale in the module "
            "docstring)"
        )

    def test_bringup_failure_names_the_conflict_and_its_pinner(self, conflicted_ha):
        """The failure detail names package, versions, requirement and pinner.

        This is the whole point of #2239: the user previously got "FastMCP
        server support is not installed", which names nothing actionable. All
        four facts are asserted together — any one of them missing leaves the
        user without a next step.
        """
        detail = _failure_detail(conflicted_ha["log_text"])

        # The message leads with the REAL error rather than fastmcp's generic
        # "FastMCP server support is not installed" hint. This clause exists
        # only on the path that found an ImportError in the exception chain, so
        # its presence IS the extraction working — asserting the exception's
        # class name instead would break on a ModuleNotFoundError, which is an
        # ImportError under a different __name__.
        assert "The underlying failure is:" in detail, (
            "the failure detail does not lead with the underlying error, so "
            "the root cause was never dug out of the exception chain and the "
            f"user is back to the generic hint:\n{detail}"
        )

        # Package + installed version + the requirement it violates + who
        # declared that requirement, in one sentence.
        violation = re.search(
            rf"Installed mcp {re.escape(_PINNED_MCP_VERSION)} does not satisfy "
            r"'([^']+)' required by (\S+)",
            detail,
        )
        assert violation is not None, (
            "the failure detail does not name the installed mcp version and "
            f"the requirement it violates:\n{detail}"
        )
        requirement, required_by = violation.groups()
        assert re.search(r"\d", requirement), (
            f"the violated requirement {requirement!r} carries no version, so "
            "the message does not tell the user which one they need"
        )
        assert required_by.startswith("fastmcp"), (
            "expected the mcp floor to be attributed to a fastmcp "
            f"distribution, got {required_by!r} — if the dependency graph "
            f"changed, update this assertion:\n{detail}"
        )

        # The integration that keeps putting the wrong version back.
        assert f"({_PINNER_DOMAIN}) pins 'mcp=={_PINNED_MCP_VERSION}'" in detail, (
            "the failure detail does not name the pinning custom integration "
            f"or its manifest requirement:\n{detail}"
        )
        assert "Update or uninstall the conflicting integration" in detail, (
            f"the failure detail closes with no action for the user:\n{detail}"
        )

    def test_preflight_audit_warns_before_the_worker_import(self, conflicted_ha):
        """A WARNING names the same conflict before the import that trips on it.

        The audit is the half of the fix that still helps when the import
        happens to succeed (a good ``mcp`` cached in ``sys.modules`` from
        before the downgrade). Its ordering ahead of the failure is what makes
        it a *pre-flight* check rather than a second copy of the error.
        """
        log_text = conflicted_ha["log_text"]
        audit_lines = [
            line for line in log_text.splitlines() if _PREFLIGHT_NEEDLE in line
        ]
        assert audit_lines, (
            "the pre-flight dependency audit logged nothing, although the "
            "installed tree is inconsistent by construction; log tail:\n"
            f"{log_text[-4000:]}"
        )
        warning = audit_lines[0]
        assert "WARNING" in warning, (
            f"the pre-flight audit did not log at WARNING:\n{warning}"
        )
        assert f"Installed mcp {_PINNED_MCP_VERSION} does not satisfy" in warning, (
            f"the pre-flight warning does not name the conflict:\n{warning}"
        )
        assert f"({_PINNER_DOMAIN}) pins 'mcp=={_PINNED_MCP_VERSION}'" in warning, (
            f"the pre-flight warning does not name the pinning integration:\n{warning}"
        )
        # The audit reports no root exception (there is none yet); the failure
        # detail does. That difference is what proves this line is the
        # pre-flight audit and not the bring-up failure being matched twice.
        assert "The underlying failure is" not in warning, (
            "the pre-flight warning carries a root-exception clause, so it was "
            f"emitted from the failure path, not ahead of it:\n{warning}"
        )
        assert log_text.index(warning) < log_text.index(_failure_detail(log_text)), (
            "the dependency audit ran after the bring-up failure, not before it"
        )
