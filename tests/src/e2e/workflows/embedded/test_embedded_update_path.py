"""End-to-end test for the in-process server UPDATE path (issues #1783, #1785).

Reproduces the exact production sequence those issues regressed on: a Home
Assistant container running the ``ha_mcp_tools`` custom component with the ha-mcp
server installed **from PyPI** (the newest published stable), then the
component's REAL in-process update mechanism — the options-flow ``pip_spec``
override, which reloads the entry, force-installs the new requirement, and
restarts the worker thread — swaps the server for a wheel built from THIS
checkout, and the server must come back up and keep serving tools.

Two scenarios run, parametrized over the component source (see the
``update_path_ha`` fixture params):

- ``component@stable``: the component checked out at the released ``stable`` git
  tag. Proves a NEW server release won't break EXISTING installs — the #1783 /
  #1785 shipping risk, where a component version already in the field is updated
  to the server this checkout produces.
- ``component@working-tree``: the component as it stands in THIS checkout. Proves
  the PR itself doesn't regress the component's own update machinery (purge,
  reload, force-install) — caught on the introducing PR instead of surfacing
  later, once ``stable`` moves to include it.

Both start the server from PyPI and drive the same update to the checkout wheel;
they differ only in which component code performs the update.

Why a dedicated, module-scoped container (like ``test_embedded_server.py``) and
NOT the shared session fixture: the session embedded backend installs the
working-tree component and seeds the checkout wheel as the INITIAL pip spec — the
server is already the new code before any update runs, which is the opposite of
the PyPI-server starting state this lane exists to prove.

Why the sentinel version: the checkout's static version equals the current PyPI
stable, so a plain rebuild would be version-invisible — the update could "succeed"
while still serving the first-imported PyPI code and no assertion could tell. The
fixture temporarily rewrites the ``[project]`` version to ``<base>.post999`` for
the build (restoring the file in ``finally``), so a distinct version proves which
code is live afterwards.

Why the MCP ``initialize`` handshake's ``serverInfo.version`` is the version
surface (not the ``update`` entity's ``installed_version`` attribute):
``serverInfo`` is the version of the ha_mcp package as imported by the
CURRENTLY-RUNNING worker thread, and the component purges the cached ``ha_mcp``
modules from ``sys.modules`` before restarting that worker — so the value is
direct evidence from inside the restarted worker (a fresh, post-purge import),
reflecting the on-disk install rather than the first-imported build. It needs no
entity polling or coordinator-refresh timing assumptions, and it is the exact
surface the #1783 / #1785 incident corrupted (that purge-and-reimport is the
precise mechanism those issues broke), which makes it the most authoritative
in-container proof the update actually took.

Scope: this lane deliberately drives the options-flow ``pip_spec`` injection, NOT
the automatic coordinator update path. A locally built wheel cannot ride
auto-update (the coordinator only ever installs the published PyPI dist), and the
auto-update decision logic — including the #1792 hold-back gate — is unit-tested.
Both triggers converge on the same reload → install → purge → restart pipeline,
which is what this lane proves end to end.

Gating: this lane is expensive (a full container bring-up plus a PyPI install of
the whole fastmcp dependency tree per scenario, run for both scenarios). It runs
ONLY when ``E2E_UPDATE_PATH=1`` (its dedicated CI job sets it, selecting
``-m update_path``); the broad e2e lanes collect it and skip at zero cost.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
import requests
from test_constants import HA_TEST_IMAGE, TEST_TOKEN

from ...utilities.streamable_http import parse_mcp_response

_UPDATE_PATH_ENV = "E2E_UPDATE_PATH"

# Phase 1 runtime-installs ha-mcp AND the whole fastmcp dependency tree from PyPI
# (no entrypoint preinstall, exactly like this module's neighbor
# test_embedded_server.py), so a minutes-scale budget.
_PHASE1_READY_TIMEOUT_S = 600
# Phase 2 force-installs only the ha-mcp wheel (its deps are already satisfied
# from phase 1) and restarts the worker; bounded like conftest's
# _EMBEDDED_BRINGUP_TIMEOUT.
_PHASE2_READY_TIMEOUT_S = 300
_READY_POLL_S = 5
_API_READY_TIMEOUT_S = 120

# ``update_path``: selected by the dedicated CI job with ``-m update_path``.
# ``skipif``: the broad e2e lanes do NOT set E2E_UPDATE_PATH, so they collect
# this module and skip it for free (no container, no wheel build). Deliberately
# NO ``container_only`` / ``not_on_embedded`` / ``external_only``: this test boots
# and drives its OWN container end to end and touches no session fixture, so it
# is backend-agnostic and must run whatever E2E_BACKEND the CI job selects (its
# dedicated job leaves it unset so the unavoidable autouse session backend boots
# the cheap container variant) — those markers would skip it on some backends.
# ``timeout``: pytest.ini sets ``timeout_func_only = true``, so this per-test
# marker bills ONLY the test FUNCTION — fixture setup (the phase-1 bring-up and
# its PyPI install) is never counted against it, and the fixture's own poll
# deadlines self-enforce there. The ceiling therefore only needs to cover the
# IN-TEST phase-2 work: the options-flow submit that triggers the reload, the
# phase-2 reinstall wait (``_PHASE2_READY_TIMEOUT_S``), and the tools/list
# round-trip. Sized comfortably above their summed request budgets so the test's
# own AssertionErrors (which attach container logs) fire before pytest-timeout.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.update_path,
    pytest.mark.skipif(
        os.environ.get(_UPDATE_PATH_ENV) != "1",
        reason=(
            "update-path e2e is a dedicated, expensive lane; set "
            f"{_UPDATE_PATH_ENV}=1 (its CI job does, with -m update_path) to run it"
        ),
    ),
    pytest.mark.timeout(_PHASE2_READY_TIMEOUT_S + 300),
]

_DOMAIN = "ha_mcp_tools"
# unique_id of the single-instance server entry (config_flow's _SERVER_UNIQUE_ID).
_UNIQUE_ID = "ha_mcp_tools-server"
_ENTRY_ID = "e2e_update_path_server_entry"
# Stable webhook id + secret seeded into entry.data so the test knows the connect
# URL up front (async_setup_entry would otherwise generate them into .storage).
_WEBHOOK_ID = "mcp_e2e_update_path_0123456789abcdef"
_SECRET_PATH = "/private_e2e_update_path_secret"
_SERVER_PORT = 9584

# Options-flow keys, mirrored as LITERALS from the released component's
# config_flow options schema (const.OPT_* values). They are hardcoded rather
# than imported because the component is a custom_component copied into the
# container, not a package importable from this test process — and pinning the
# literal wire keys means a rename in the component surfaces here as a failure
# (a breaking options-flow change) instead of being silently followed.
_OPT_CHANNEL = "channel"
_OPT_AUTO_UPDATE = "auto_update"
_OPT_SERVER_PORT = "server_port"
_OPT_BIND_HOST = "bind_host"
_OPT_WEBHOOK_AUTH = "webhook_auth"
_OPT_PIP_SPEC = "pip_spec"
_OPT_SERVER_URL = "server_url"
_OPT_ENABLE_WEBHOOK = "enable_webhook"
_OPT_EXTERNAL_URL = "external_url"
_OPT_WEBHOOK_ID_OVERRIDE = "webhook_id_override"
_OPT_SECRET_PATH_OVERRIDE = "secret_path_override"
_OPT_REGENERATE_SECRETS = "regenerate_secrets"

_REPO_ROOT = Path(__file__).resolve().parents[5]
_INITIAL_STATE = _REPO_ROOT / "tests" / "initial_test_state"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_STABLE_TAG = "stable"


def _docker_probe_error() -> str | None:
    """Return None when the Docker daemon answers a ping, else the failure text.

    The module-level ``skipif`` guarantees this lane only runs with
    ``E2E_UPDATE_PATH=1`` — the dedicated CI job — and that lane REQUIRES Docker,
    so a probe failure is a hard error, not a skip. The exception text is
    captured and returned (the previous bare-except discarded WHY) so the
    fixture's ``pytest.fail`` is actionable.
    """
    try:
        import docker as docker_sdk

        docker_sdk.from_env().ping()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _export_released_component(dest_dir: Path) -> Path:
    """Extract the ``ha_mcp_tools`` component at the ``stable`` tag into ``dest_dir``.

    Uses ``git archive`` piped through Python's ``tarfile`` (no ``tar`` binary
    required, portable across CI runners), so the component under test is the
    RELEASED code a production user actually runs — not the working tree. Fails
    loudly if the ``stable`` tag is missing (the CI job checks out with
    fetch-depth 0 so it is present).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "git",
            "-C",
            str(_REPO_ROOT),
            "archive",
            "--format=tar",
            _STABLE_TAG,
            "--",
            f"custom_components/{_DOMAIN}",
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        pytest.fail(
            f"could not `git archive` the {_STABLE_TAG!r} tag — is it fetched? "
            f"(the CI job needs fetch-depth 0):\n{stderr}"
        )
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as tar:
        # filter="data" rejects absolute/traversing member paths (safe extract).
        tar.extractall(dest_dir, filter="data")
    component = dest_dir / "custom_components" / _DOMAIN
    if not component.is_dir():
        pytest.fail(
            f"the {_STABLE_TAG!r} archive did not contain custom_components/{_DOMAIN}"
        )
    return component


def _build_sentinel_wheel(dest_dir: Path) -> tuple[Path, str]:
    """Build a ha-mcp wheel from THIS checkout carrying a distinct sentinel version.

    The checkout's static version equals the current PyPI stable, so the version
    is temporarily rewritten to ``<base>.post999`` for the build to make the wheel
    identifiable at runtime. The original ``pyproject.toml`` bytes are read first
    and restored in ``finally`` — even if the build raises — so the tree is never
    left mutated. Returns ``(wheel_path, sentinel_version)``.

    ``uv build`` (not ``python -m pip wheel``): the suite runs under ``uv run`` in
    a venv that ships no ``pip``, so ``pip wheel`` would exit non-zero; ``uv`` is
    always on PATH and builds in an isolated env (matches the neighbor helpers).
    """
    original = _PYPROJECT.read_bytes()
    base_version = str(tomllib.loads(original.decode("utf-8"))["project"]["version"])
    sentinel = f"{base_version}.post999"
    patched, count = re.subn(
        rb'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{sentinel}"'.encode(),
        original,
        count=1,
    )
    if count != 1:
        pytest.fail("could not rewrite the [project] version line in pyproject.toml")

    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        _PYPROJECT.write_bytes(patched)
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(dest_dir), str(_REPO_ROOT)],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    finally:
        _PYPROJECT.write_bytes(original)

    wheels = list(dest_dir.glob(f"ha_mcp-{sentinel}-*.whl"))
    if not wheels:
        built = sorted(p.name for p in dest_dir.glob("*.whl"))
        pytest.fail(f"sentinel wheel for {sentinel!r} not built; got {built}")
    return wheels[0], sentinel


def _seed_config(config_path: Path, component_src: Path) -> None:
    """Install the RELEASED component + seed a server entry that installs from PyPI.

    The seeded ``pip_spec`` is EMPTY: ``_resolve_pip_spec`` then resolves the bare
    ``ha-mcp`` distribution, so the first bring-up installs the newest PyPI stable
    — exactly a fresh production install, and the starting point for the update.
    """
    shutil.copytree(_INITIAL_STATE, config_path, dirs_exist_ok=True)

    dest = config_path / "custom_components" / _DOMAIN
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(component_src, dest, dirs_exist_ok=True)

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
            },
            "disabled_by": None,
            "discovery_keys": {},
            "domain": _DOMAIN,
            "entry_id": _ENTRY_ID,
            "minor_version": 1,
            "modified_at": "2025-09-07T23:56:28.040747+00:00",
            "options": {
                # Empty => bare ``ha-mcp`` dist => newest PyPI stable on bring-up.
                _OPT_PIP_SPEC: "",
                _OPT_CHANNEL: "stable",
                _OPT_AUTO_UPDATE: True,
                _OPT_SERVER_PORT: _SERVER_PORT,
                _OPT_BIND_HOST: "127.0.0.1",
                _OPT_WEBHOOK_AUTH: "none",
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

    # HA runs as uid 0 in the test image but the bind mount must be traversable.
    for path in config_path.rglob("*"):
        try:
            path.chmod(0o777 if path.is_dir() else 0o666)
        except OSError:
            pass  # Best-effort chmod; some testcontainer mounts refuse it.


def _wait_http_ok(url: str, headers: dict[str, str], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, headers=headers, timeout=5).status_code == 200:
                return
        except requests.exceptions.RequestException:
            pass  # HA still booting; retry until the deadline.
        time.sleep(2)
    raise AssertionError(f"{url} not ready within {timeout}s")


def _mcp_post(
    base_url: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return requests.post(
        f"{base_url}/api/webhook/{_WEBHOOK_ID}",
        headers=headers,
        data=json.dumps(payload),
        timeout=60,
    )


def _server_version(base_url: str) -> tuple[str | None, str | None]:
    """Run an MCP ``initialize`` and return ``(serverInfo.version, session_id)``.

    ``version`` is None when the server has not answered with a valid JSON-RPC
    result yet (still installing / restarting). See the module docstring for why
    ``serverInfo.version`` is the authoritative live-code surface.
    """
    resp = _mcp_post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ha_mcp_update_path-e2e", "version": "1.0"},
            },
        },
    )
    parsed = parse_mcp_response(resp.headers.get("Content-Type", ""), resp.content)
    if not parsed or "result" not in parsed:
        return None, None
    version = (parsed["result"].get("serverInfo") or {}).get("version")
    session_id = resp.headers.get("Mcp-Session-Id")
    if session_id:
        # Some servers want the initialized notification before further requests.
        _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
    return version, session_id


def _wait_for_server(
    base_url: str, timeout: int, *, expected_version: str | None = None
) -> tuple[str | None, str | None]:
    """Poll ``initialize`` until the server answers.

    When ``expected_version`` is given, keep polling until the server reports
    exactly that version (the reload window can briefly still serve the old
    build). Returns ``(version, session_id)`` on success, or ``(last_seen, None)``
    on timeout so the caller can fail with the version it actually saw.
    """
    deadline = time.monotonic() + timeout
    last_seen: str | None = None
    while time.monotonic() < deadline:
        try:
            version, session_id = _server_version(base_url)
        except requests.exceptions.RequestException:
            version, session_id = None, None
        if version is not None:
            last_seen = version
            if expected_version is None or version == expected_version:
                return version, session_id
        time.sleep(_READY_POLL_S)
    return last_seen, None


def _dump_logs(container: Any) -> Any:
    """Return the container's logs for an assertion message; never raises.

    Mirrors the ``contextlib.suppress`` get_logs pattern used at the phase-2
    failure so any error path can embed container logs without a secondary
    exception masking the original assertion.
    """
    logs: Any = ""
    with contextlib.suppress(Exception):
        logs = container.get_logs()
    return logs


def _submit_options_update(
    base_url: str, entry_id: str, user_input: dict[str, Any], container: Any
) -> dict[str, Any]:
    """Drive the component's options flow over HA's REST API; return the result.

    Hits the same "Configure" endpoints as ha_mcp's
    ``HomeAssistantClient.start_options_flow`` / ``submit_options_flow_step`` but
    with plain ``requests`` (like the rest of this module), pointed at THIS
    dedicated container — the shared session client speaks to a different HA and
    cannot reach it. Sync ``requests`` also keeps this off the pytest-asyncio
    session loop that conftest's autouse session fixture runs on.

    ``container`` is used only to attach best-effort logs to the AssertionErrors
    raised on an HTTP error, so an options-flow failure is diagnosable from the
    test output alone (the flow runs the reload/install/purge/restart pipeline —
    when it fails, the container log is where the reason is).
    """
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Content-Type": "application/json",
    }
    start = requests.post(
        f"{base_url}/api/config/config_entries/options/flow",
        headers=headers,
        data=json.dumps({"handler": entry_id}),
        timeout=60,
    )
    if start.status_code >= 400:
        raise AssertionError(
            f"options-flow start failed: {start.status_code} {start.text[:500]}\n"
            f"Container logs:\n{_dump_logs(container)}"
        )
    flow_id = start.json()["flow_id"]
    submit = requests.post(
        f"{base_url}/api/config/config_entries/options/flow/{flow_id}",
        headers=headers,
        data=json.dumps(user_input),
        timeout=120,
    )
    if submit.status_code >= 400:
        raise AssertionError(
            f"options-flow submit failed: {submit.status_code} {submit.text[:500]}\n"
            f"Container logs:\n{_dump_logs(container)}"
        )
    return submit.json()


@pytest.fixture(
    scope="module",
    params=["stable", "head"],
    ids=["component@stable", "component@working-tree"],
)
def update_path_ha(request):
    """Boot a dedicated HA container: component + PyPI server, phase-1 ready.

    Parametrized over the component source (rationale in the module docstring):

    - ``stable``: the ``ha_mcp_tools`` component at the released ``stable`` git
      tag (git-archive export) — proves a new server release won't break existing
      installs.
    - ``head``: the working tree's ``custom_components/ha_mcp_tools`` (the same
      source conftest's ``_install_custom_component`` copies) — proves the PR's
      own component update machinery still works.

    Both containers run sequentially in one job. Yields a context dict once the
    PyPI-installed in-process server answers its webhook; the sentinel wheel is
    already staged under ``/config`` for the test's options-flow update.
    """
    docker_error = _docker_probe_error()
    if docker_error is not None:
        # The module-level skipif guarantees this fixture only runs with
        # E2E_UPDATE_PATH=1 — the dedicated lane, which REQUIRES Docker. A skip
        # here would let the required gate go green while the lane tested nothing,
        # so FAIL (with the captured probe error) instead of skipping.
        pytest.fail(
            f"Docker is required for the update-path e2e lane but the daemon "
            f"probe failed: {docker_error}"
        )

    from testcontainers.core.container import DockerContainer

    work_dir = Path(tempfile.mkdtemp(prefix="ha_mcp_update_path_"))
    config_path = Path(tempfile.mkdtemp(prefix="ha_mcp_update_path_cfg_"))
    container: Any = None
    try:
        if request.param == "stable":
            component_src = _export_released_component(work_dir / "released")
        else:
            # Working-tree component, sourced the same way conftest's
            # _install_custom_component does (the copytree in _seed_config below
            # installs it into the container's custom_components).
            component_src = _REPO_ROOT / "custom_components" / _DOMAIN
            if not component_src.is_dir():
                pytest.fail(f"working-tree component not found at {component_src}")

        try:
            wheel, sentinel = _build_sentinel_wheel(work_dir / "wheel")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
            # FAIL, not skip: a wheel that cannot be built is a real regression
            # this lane exists to catch. Surface the build stderr so it is
            # actionable.
            stderr = (getattr(err, "stderr", "") or "").strip()
            pytest.fail(f"could not build the sentinel wheel: {err}\nstderr:\n{stderr}")

        shutil.copy2(wheel, config_path / wheel.name)
        _seed_config(config_path, component_src)

        container = (
            DockerContainer(HA_TEST_IMAGE)
            .with_exposed_ports(8123)
            .with_volume_mapping(str(config_path), "/config", "rw")
            .with_env("TZ", "UTC")
        )
        container.start()

        host = container.get_container_host_ip()
        port = container.get_exposed_port(8123)
        base_url = f"http://{host}:{port}"
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        _wait_http_ok(f"{base_url}/api/", headers, _API_READY_TIMEOUT_S)

        phase1_version, _ = _wait_for_server(base_url, _PHASE1_READY_TIMEOUT_S)
        # Reject the "unknown" fallback as well as None: a server that answers
        # initialize but reports no real version has not finished importing, so
        # its baseline is not a usable transition anchor.
        if phase1_version in (None, "unknown"):
            raise AssertionError(
                "the PyPI-installed in-process server never reported a usable "
                f"version within {_PHASE1_READY_TIMEOUT_S}s (got "
                f"{phase1_version!r}). Container logs:\n{_dump_logs(container)}"
            )
        yield {
            "base_url": base_url,
            "entry_id": _ENTRY_ID,
            "sentinel_version": sentinel,
            "wheel_name": wheel.name,
            "phase1_version": phase1_version,
            "container": container,
        }
    finally:
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop()
        # Container is stopped (or never started), so the /config bind mount is
        # released — safe to drop the host temp dirs. Best-effort: a leftover dir
        # is harmless, so ignore_errors.
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(config_path, ignore_errors=True)


class TestEmbeddedUpdatePath:
    def test_component_updates_server_to_local_wheel(self, update_path_ha):
        base_url = update_path_ha["base_url"]
        sentinel = update_path_ha["sentinel_version"]
        wheel_name = update_path_ha["wheel_name"]
        phase1_version = update_path_ha["phase1_version"]
        container = update_path_ha["container"]

        # (1) Phase 1: the component brought the PyPI server up (fixture
        # guaranteed) and it reports a REAL baseline version — not None, not the
        # "unknown" fallback — that is NOT already the sentinel build about to be
        # installed, so the update is a real, observable transition, not a no-op.
        assert phase1_version not in (None, "unknown") and phase1_version != sentinel, (
            f"phase-1 server reported {phase1_version!r}; expected a real PyPI "
            f"baseline version distinct from the sentinel {sentinel!r} (did a "
            "sentinel-versioned wheel leak onto PyPI, or did the server fail to "
            "import a real version?)"
        )

        # Drive the component's REAL update path: save an options-flow pip_spec
        # override pointing at the checkout-built wheel staged under /config.
        # Saving reloads the entry, which force-installs the wheel and restarts
        # the worker — the production sequence from #1783 / #1785. All schema keys
        # are echoed with their defaults; only pip_spec changes.
        user_input = {
            _OPT_CHANNEL: "stable",
            _OPT_AUTO_UPDATE: True,
            _OPT_SERVER_PORT: _SERVER_PORT,
            _OPT_BIND_HOST: "127.0.0.1",
            _OPT_WEBHOOK_AUTH: "none",
            _OPT_PIP_SPEC: f"ha-mcp @ file:///config/{wheel_name}",
            _OPT_SERVER_URL: "http://127.0.0.1:8123",
            _OPT_ENABLE_WEBHOOK: True,
            _OPT_EXTERNAL_URL: "",
            _OPT_WEBHOOK_ID_OVERRIDE: "",
            _OPT_SECRET_PATH_OVERRIDE: "",
            _OPT_REGENERATE_SECRETS: False,
        }
        result = _submit_options_update(base_url, _ENTRY_ID, user_input, container)
        assert result.get("type") == "create_entry", (
            f"options submit did not persist the override: {result}\n"
            f"Container logs:\n{_dump_logs(container)}"
        )

        # (2) + (4) The server comes back up AND reports the sentinel version —
        # proof the freshly force-installed wheel is the code now serving (the
        # component purges cached ha_mcp before the worker restarts, so this
        # reflects the on-disk install, not the first-imported PyPI build).
        version, session_id = _wait_for_server(
            base_url, _PHASE2_READY_TIMEOUT_S, expected_version=sentinel
        )
        if version != sentinel:
            raise AssertionError(
                f"after the options-flow update the server reports {version!r}, "
                f"not the sentinel {sentinel!r} — the in-process update did not "
                f"bring the new code up within {_PHASE2_READY_TIMEOUT_S}s. "
                f"Container logs:\n{_dump_logs(container)}"
            )

        # (3) The reinstalled server serves the full tool inventory over MCP.
        resp = _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
        )
        parsed = parse_mcp_response(resp.headers.get("Content-Type", ""), resp.content)
        assert parsed is not None, (
            f"unparseable tools/list response: {resp.text[:500]}\n"
            f"Container logs:\n{_dump_logs(container)}"
        )
        assert "result" in parsed, f"{parsed}\nContainer logs:\n{_dump_logs(container)}"
        tools = parsed["result"].get("tools", [])
        names = {t.get("name") for t in tools}
        # The full ha-mcp inventory (a handful would mean a truncated/wrong server).
        assert len(tools) > 60, (
            f"expected the full tool inventory, got {len(tools)}\n"
            f"Container logs:\n{_dump_logs(container)}"
        )
        assert "ha_get_state" in names, (
            f"ha_get_state missing from tools/list; got {sorted(names)[:20]}\n"
            f"Container logs:\n{_dump_logs(container)}"
        )
