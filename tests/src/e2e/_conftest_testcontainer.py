"""Testcontainer setup for the E2E session: config staging, container build and readiness."""

import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest
import requests
from testcontainers.core.container import DockerContainer

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))  # tests/src/ for haos_runtime
from haos_runtime import inject_hacs_token

# Import test constants
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from test_constants import HA_TEST_IMAGE

from ._conftest_embedded import (
    _EMBEDDED_DOMAIN,
    _EMBEDDED_WEBHOOK_ID,
    EMBEDDED_FREEZE_AFTER,
    EMBEDDED_FREEZE_BEFORE,
    EMBEDDED_HA_CONSTRAINTS_COPY,
    _build_embedded_server_wheel,
    _install_embedded_server,
    _is_no_tools_entry_selected,
    _wait_for_embedded_webhook_ready,
)
from ._conftest_readiness import (
    _dump_ha_readiness_diagnostics,
    _log_readiness_timing,
    _wait_for_core_state_running,
    _wait_for_entries_loaded,
    _wait_for_ha_api_ready,
)
from ._conftest_seed import (
    _collect_manifest_requirements,
    _ensure_hacs_frontend,
    _install_custom_component,
    _refresh_recorder_timestamps,
    _seed_legacy_yaml_backups,
    _seed_non_yaml_package_file,
    _seed_yaml_package_scene,
    _setup_config_permissions,
)

logger = logging.getLogger(__name__)


# Boot budgets for the embedded path. The wheel + its dependency tree is
# preinstalled in the container entrypoint BEFORE HA's /init (so bring-up is
# fast + deterministic), which delays /api/ liveness by the pip window — hence a
# much larger API-ready timeout than the default 60s. ``_EMBEDDED_BRINGUP_TIMEOUT``
# then covers the background bring-up (force-install of the local wheel with deps
# already satisfied, token provisioning, server thread start, webhook register).
_EMBEDDED_API_READY_TIMEOUT = 480
_EMBEDDED_BRINGUP_TIMEOUT = 300


def _reset_ha_in_process_caches() -> None:
    """Clear in-process caches that reference the previous HA container.

    Called from the initial fixture setup so subsequent
    ``HomeAssistantClient`` lookups go through the new container's URL
    instead of stale references to a prior container. (A second call
    site lived in a container-restart retry path that was dropped as a
    post-#1262 simplification; the helper is kept for a future caller.)

    ``ha_mcp.config._settings`` caches the URL + token. ``WebSocketManager``
    pools live connections keyed by URL: ``_clients`` (plural dict) and
    ``_last_used`` are the real attribute names — a direct
    ``websocket_manager._client = None`` (singular) would create a no-op
    attribute on the singleton without clearing the connection pool, since
    ``_client`` is not declared on the class.
    """
    from ha_mcp import config
    from ha_mcp.client.websocket_client import websocket_manager

    config._settings = None
    websocket_manager._clients.clear()
    websocket_manager._last_used.clear()
    websocket_manager._current_loop = None


def _detect_docker_host() -> dict:
    """Detect the correct host address and extra_hosts config for the Docker environment.

    Docker Desktop (WSL2 / Mac / Windows) embeds a DNS server that resolves
    ``host.docker.internal`` inside containers automatically.  On plain Linux
    Docker (GitHub Actions CI) that DNS is absent, so we must inject the
    mapping via ``--add-host host.docker.internal:host-gateway``.

    Strategy: run a minimal probe container and ask it to resolve
    ``host.docker.internal``.  If it resolves, Docker Desktop DNS is active and
    we must NOT override the entry (doing so breaks the internal routing).  If
    it does not resolve, we are on plain Linux Docker and must add extra_hosts.

    Returns a dict with:
    - ``hostname`` - hostname that Docker containers use to reach the host
    - ``extra_hosts`` - dict passed to ``container.with_kwargs`` (may be empty)
    """
    try:
        import docker as docker_sdk

        client = docker_sdk.from_env()
        output = client.containers.run(
            "alpine",
            [
                "sh",
                "-c",
                "getent hosts host.docker.internal 2>/dev/null | awk '{print $1}'",
            ],
            remove=True,
        )
        if output.strip():
            # Docker Desktop DNS resolved the name — use hostname, no override needed
            logger.info(
                "🔍 Docker Desktop DNS detected — using host.docker.internal as-is"
            )
            return {"hostname": "host.docker.internal", "extra_hosts": {}}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Docker Desktop DNS probe failed: {exc}")

    # Plain Linux Docker — inject the mapping so the hostname resolves in the container
    logger.info(
        "🔍 Plain Linux Docker detected — injecting host.docker.internal via extra_hosts"
    )
    return {
        "hostname": "host.docker.internal",
        "extra_hosts": {"host.docker.internal": "host-gateway"},
    }


def _copy_local_blueprint_to_www(config_path: Path) -> dict[str, str]:
    """Copy the E2E blueprint fixture into HA's /local static file directory."""
    blueprint_name = "e2e_test_blueprint.yaml"
    source = (
        Path(__file__).parent.parent.parent / "assets" / "blueprints" / blueprint_name
    )
    if not source.exists():
        pytest.fail(f"Blueprint test asset not found at {source}")

    www_dir = config_path / "www"
    www_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, www_dir / blueprint_name)

    return {
        "base_url": "http://localhost:8123/local",
        "filename": blueprint_name,
        "local_dir": str(www_dir),
    }


def _prepare_testcontainer_config(
    embedded: bool,
) -> tuple[Path, str, dict, str | None]:
    """Stage the fresh HA config dir (custom components, seeds, recorder)."""
    embedded_wheel_name: str | None = None

    # Create temporary directory for this test session
    temp_dir = tempfile.mkdtemp(prefix="ha_e2e_test_")

    # Copy initial test state to temporary directory
    initial_state_path = Path(__file__).parent.parent.parent / "initial_test_state"
    config_path = Path(temp_dir)

    if not initial_state_path.exists():
        pytest.fail(f"Initial test state not found at {initial_state_path}")

    # Ensure HACS frontend is downloaded (if HACS is present)
    _ensure_hacs_frontend(initial_state_path)

    # Copy all files from initial_test_state
    shutil.copytree(initial_state_path, config_path, dirs_exist_ok=True)

    # Inject GITHUB_TOKEN into HACS config entry if available.
    # Without a valid token HACS disables itself, causing flaky test skips.
    # In CI the automatic GITHUB_TOKEN provides sufficient read access.
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        storage_file = config_path / ".storage" / "core.config_entries"
        if storage_file.exists():
            ce_data = json.loads(storage_file.read_text())
            if inject_hacs_token(ce_data, github_token):
                logger.info("Injected GITHUB_TOKEN into HACS config entry")
            storage_file.write_text(json.dumps(ce_data, indent=2))

    # Install custom components from repo source
    repo_root = Path(__file__).parent.parent.parent.parent
    if _install_custom_component(
        config_path,
        repo_root / "homeassistant-addon-webhook-proxy" / "mcp_proxy",
        "mcp_proxy",
        "MCP Webhook Proxy",
    ):
        # mcp_proxy needs a config file pointing at HA's own API
        proxy_config = {
            "target_url": "http://localhost:8123/api/",
            "webhook_id": "mcp_e2e_test_webhook_proxy",
        }
        (config_path / ".mcp_proxy_config.json").write_text(json.dumps(proxy_config))
    # No-tools lanes (#2292): the File & YAML Tools entry is absent, in the two
    # shapes a real install can take. Embedded still needs the component FILES
    # (its in-process server entry loads from them), so the files are copied
    # with no tools entry seeded — the exact server-entry-only topology. The
    # plain container installs nothing at all: a user who never installed the
    # component. The mcp_proxy install above is untouched either way.
    no_tools_entry = _is_no_tools_entry_selected()
    if not no_tools_entry or embedded:
        _install_custom_component(
            config_path,
            repo_root / "custom_components" / "ha_mcp_tools",
            "ha_mcp_tools",
            "HA-MCP File & YAML Tools",
            seed_entry=not no_tools_entry,
        )

    # Embedded backend: build the checkout's wheel into /config, install the
    # in-process MCP server entry, seed its config entry + feature-flag overrides.
    # Runs before _setup_config_permissions so the wheel, integration, and the
    # .ha_mcp data dir all get the same readable perms as the rest of the
    # config. The wheel's dep tree is preinstalled in the container entrypoint.
    if embedded:
        wheel = _build_embedded_server_wheel(config_path)
        embedded_wheel_name = wheel.name
        _install_embedded_server(config_path, embedded_wheel_name)

    # Pre-#1579 legacy backups for the legacy-restore e2e: seed before boot so
    # the bind-mounted .ha_mcp_tools_backups/ is populated when the component
    # reads it (a post-boot host write doesn't propagate in CI).
    _seed_legacy_yaml_backups(config_path)

    # A non-YAML file in the bound packages folder for the glob warn-and-continue
    # e2e (#1788): same pre-boot reason, and no tool can write it there.
    _seed_non_yaml_package_file(config_path)

    # A YAML-package scene with an ``id`` for the not-storage-scene e2e (#1971):
    # registers in the registry yet 404s on the config API. Same pre-boot reason.
    _seed_yaml_package_scene(config_path)

    # Shift the pre-baked recorder timestamps forward so the seeded rows
    # look "recent" to history queries with a 24h window. The recorder DB in
    # initial_test_state is baked offline (see scripts/bake_pagination_seed.py)
    # and its rows have whatever timestamps were captured at bake time. Without
    # this shift, the pagination tests in test_history.py/test_logbook.py would
    # silently skip again the moment the seed gets more than 24h old.
    _refresh_recorder_timestamps(config_path / "home-assistant_v2.db")
    local_blueprint = _copy_local_blueprint_to_www(config_path)

    # Ensure proper permissions for Home Assistant
    _setup_config_permissions(config_path)

    logger.info(
        f"📁 Fresh HA config prepared at: {config_path} with proper permissions"
    )
    return config_path, temp_dir, local_blueprint, embedded_wheel_name


def _build_ha_testcontainer(
    config_path: Path, embedded: bool, embedded_wheel_name: str | None
) -> DockerContainer:
    """Construct the HA DockerContainer with ports, mounts, and preinstall entrypoint."""
    # Create testcontainer with port configuration
    container = DockerContainer(HA_TEST_IMAGE)

    # Check for custom port via environment variable
    custom_port = os.environ.get("HA_TEST_PORT")
    if custom_port:
        try:
            port = int(custom_port)
            container = container.with_bind_ports(8123, port)
            logger.info(f"🔌 Using fixed port {port} (from HA_TEST_PORT)")
        except ValueError:
            logger.warning(
                f"⚠️ Invalid HA_TEST_PORT '{custom_port}', using dynamic port"
            )
            container = container.with_exposed_ports(8123)
    else:
        container = container.with_exposed_ports(8123)  # Dynamic port assignment
    container = container.with_volume_mapping(
        str(config_path), "/config", "rw"
    )  # Ensure read-write mount
    container = container.with_env("TZ", "UTC")
    # Add privileged mode for Home Assistant hardware access.
    container_kwargs: dict = {"privileged": True}

    # Pre-install custom-component manifest requirements into the HA
    # container's Python env before HA boots. HA's own runtime manifest-
    # install does not reliably fire when ``_install_custom_component``
    # pre-injects a config entry via ``.storage/core.config_entries`` —
    # observed on PR #1268 ARM E2E (2026-05-12) where ``ruamel.yaml``
    # was never installed by HA on that run and the integration ended up
    # in ``state=setup_error``. The wrapped entrypoint runs ``pip
    # install`` first; ``&&`` short-circuits to ``/init`` only on success,
    # so install failures surface as container-exit-non-zero rather than
    # as an HA boot with missing dependencies.
    # Commands to run in the wrapped entrypoint before ``exec /init``. Each is
    # ``&&``-chained so a failure surfaces as container-exit-non-zero rather than
    # an HA boot with missing dependencies.
    preinit_cmds: list[str] = []

    manifest_reqs = _collect_manifest_requirements(config_path)
    if manifest_reqs:
        quoted = " ".join(shlex.quote(r) for r in manifest_reqs)
        # PyPI-first with wheels-index fallback. The image env pins
        # PIP_EXTRA_INDEX_URL=wheels.home-assistant.io, and when that index
        # is down pip stalls through 5 read-timeout retries per lookup —
        # blowing the readiness budget long before HA even boots (took
        # every E2E CI job down during the 2026-07-02 outage). All current
        # manifest requirements ship musllinux wheels on PyPI, so attempt 1
        # drops the extra index and forbids sdist builds (fail fast, no
        # surprise compiles); the fallback restores the image's stock pip
        # env for any future requirement only the wheels index carries.
        # The ``env -u`` is load-bearing: it assumes the image pins the extra
        # index via the PIP_EXTRA_INDEX_URL env var (true today). If that
        # ever moves into pip.conf, attempt 1 silently reverts to hitting the
        # wheels index — the ``||`` fallback still keeps installs working.
        preinit_cmds.append(
            f"(env -u PIP_EXTRA_INDEX_URL pip install --no-cache-dir "
            f"--only-binary=:all: {quoted} "
            f"|| pip install --no-cache-dir {quoted})"
        )
        logger.info(
            f"📦 Pre-installing {len(manifest_reqs)} custom-component "
            f"requirement(s) before HA boots: {manifest_reqs}"
        )

    if embedded and embedded_wheel_name is not None:
        # Preinstall the ha-mcp wheel + its whole dependency tree (fastmcp etc.)
        # BEFORE HA's /init, so the in-process MCP server bring-up is fast and
        # deterministic — its force-install of the local wheel then finds every
        # dependency already satisfied, and the mcp_client fixture's webhook-
        # readiness poll doesn't have to sit through a multi-minute PyPI download.
        # The heavy download happens during container boot instead, which is why
        # the embedded path uses a much larger HA-API-ready budget below.
        # Resolve under HA's OWN constraints file so the preinstall cannot
        # mutate the image's pinned dependency set (a real HA install applies
        # the same constraints) — this is exactly what surfaced the
        # cryptography-floor incompatibility with HA 2026.6 (live-found).
        # ``pip list --format=freeze`` snapshots bracket the install so
        # workflows/embedded/test_embedded_no_stomp.py can assert the install
        # replaced NOTHING the image already shipped (#2135/#2146: an exact
        # ha-mcp pin above an image-shipped version forces a non-atomic
        # in-place replacement — interrupt it and the package is torn). The
        # snapshots land in the bind-mounted /config so the test reads them
        # host-side.
        quoted_wheel = shlex.quote(f"/config/{embedded_wheel_name}")
        constraints_probe = (
            "HACONS=\"$(python3 -c 'import homeassistant, os; "
            "print(os.path.join(os.path.dirname(homeassistant.__file__), "
            '"package_constraints.txt"))\')"'
        )
        preinit_cmds.append(
            f"pip list --format=freeze > /config/{EMBEDDED_FREEZE_BEFORE} && "
            f"{constraints_probe} && "
            f'if [ -f "$HACONS" ]; then '
            f'cp "$HACONS" /config/{EMBEDDED_HA_CONSTRAINTS_COPY} && '
            f'pip install --no-cache-dir --constraint "$HACONS" {quoted_wheel}; '
            f"else pip install --no-cache-dir {quoted_wheel}; fi && "
            f"pip list --format=freeze > /config/{EMBEDDED_FREEZE_AFTER}"
        )
        logger.info(
            "📦 Embedded backend: preinstalling ha-mcp wheel %s (+deps) before "
            "HA boots",
            embedded_wheel_name,
        )

    if preinit_cmds:
        container_kwargs["entrypoint"] = [
            "sh",
            "-c",
            " && ".join(preinit_cmds) + " && exec /init",
        ]

    container = container.with_kwargs(**container_kwargs)

    # Remove any .HA_RESTORE file that might cause issues
    restore_file = config_path / ".HA_RESTORE"
    if restore_file.exists():
        restore_file.unlink()
        logger.info("🗑️ Removed .HA_RESTORE file from config")
    return container


def _wait_for_testcontainer_sun(
    base_url: str,
    headers: dict[str, str],
    container: DockerContainer,
    SUN_WAIT: int,
) -> None:
    """Poll sun.sun until it leaves 'unknown'; warn + dump diagnostics on timeout."""
    logger.info("⏳ Waiting for sun.sun to reach a known state...")
    sun_start = time.monotonic()
    while time.monotonic() - sun_start < SUN_WAIT:
        try:
            sun_resp = requests.get(
                f"{base_url}/api/states/sun.sun", timeout=5, headers=headers
            )
            if sun_resp.status_code == 200:
                sun_state = sun_resp.json().get("state", "unknown")
                if sun_state != "unknown":
                    elapsed = time.monotonic() - sun_start
                    logger.info(f"✅ sun.sun is '{sun_state}' after {elapsed:.1f}s")
                    _log_readiness_timing("sun", elapsed, state=sun_state)
                    break
        except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
            logger.debug(f"sun.sun check failed: {exc}")
        time.sleep(1)
    else:
        _dump_ha_readiness_diagnostics(
            container,
            base_url,
            headers,
            label="sun-wait-warn",
            config_entry_domain="sun",
        )
        logger.warning(
            f"⚠️ sun.sun still 'unknown' after {SUN_WAIT}s — template tests may fail"
        )


def _wait_for_testcontainer_ready(
    container: DockerContainer,
    base_url: str,
    headers: dict[str, str],
    embedded: bool,
    CORE_STATE_TIMEOUT: int,
    SUN_WAIT: int,
) -> str | None:
    """Run every testcontainer readiness gate; return the embedded webhook URL or None."""
    # Check if container is actually running
    import docker

    docker_client = docker.from_env()
    try:
        container_obj = docker_client.containers.get(
            container.get_wrapped_container().id
        )
        logger.info(f"📋 Container status: {container_obj.status}")
        logger.info(f"🔌 Port mappings: {container_obj.ports}")

        # Get recent logs for debugging
        logs = container_obj.logs(tail=20).decode("utf-8", errors="ignore")
        logger.info(f"📄 Container logs:\n{logs}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"⚠️ Could not inspect container: {e}")

    # Wait for API to be ready via the module-level
    # ``_wait_for_ha_api_ready`` helper. The helper extraction is
    # preserved as a single-contract surface in case a future bounded
    # retry path is reintroduced.
    # NOTE: ``requests`` is imported at module top; do NOT re-import it
    # locally here — Python's scoping rules would then make ``requests``
    # a function-local for the entire ha_container_with_fresh_config,
    # which previously caused UnboundLocalError in the HAOS branch.

    # The embedded backend's entrypoint preinstalls the ha-mcp wheel + its
    # dependency tree before HA's /init, so /api/ liveness is delayed by that
    # pip window and needs a much larger budget than the default 60s.
    api_ready_timeout = _EMBEDDED_API_READY_TIMEOUT if embedded else 60
    logger.info("🔄 Waiting for Home Assistant API to become ready...")
    if not _wait_for_ha_api_ready(base_url, headers, timeout=api_ready_timeout):
        _dump_ha_readiness_diagnostics(
            container, base_url, headers, label="api-not-ready"
        )
        pytest.fail(
            f"Home Assistant API at {base_url} did not become ready within "
            f"{api_ready_timeout} seconds.\n"
            "The container may have failed to start. Check Docker logs for details."
        )

    # Single readiness gate: poll ``/api/core/state`` until
    # ``CoreState.RUNNING``. This replaces five separate polling gates
    # (components/entities/input_boolean/ha_mcp_tools/sun) that were
    # racing ``async_setup_entry`` for slow integrations — see #366
    # thread (Ilya0527 2026-05-18) and the docstring on
    # ``_wait_for_core_state_running`` for the structural rationale.
    # ``CORE_STATE_TIMEOUT`` is defined at the top of this ``with
    # container:`` block alongside ``SUN_WAIT`` for grep-ability.
    logger.info("⏳ Waiting for HA CoreState to reach RUNNING...")
    (
        core_state_ok,
        core_state_elapsed,
        core_state_last,
        entries_loaded,
        entries_total,
        snapshot_ok,
        entries_unloaded,
    ) = _wait_for_core_state_running(base_url, headers, CORE_STATE_TIMEOUT)
    if not core_state_ok:
        _dump_ha_readiness_diagnostics(
            container, base_url, headers, label="core-state-not-running"
        )
        pytest.fail(
            f"HA CoreState did not reach 'RUNNING' within "
            f"{CORE_STATE_TIMEOUT}s. Last observed state: "
            f"{core_state_last!r}. Config entries loaded: "
            f"{entries_loaded}/{entries_total}"
            f"{'' if snapshot_ok else ' (snapshot unavailable)'}. "
            f"Most likely an integration's async_setup_entry hit the "
            f"300s SLOW_SETUP_MAX_WAIT ceiling. Check Docker logs."
        )
    _log_readiness_timing(
        "core_state",
        core_state_elapsed,
        state=core_state_last,
        entries_loaded=entries_loaded,
        entries_total=entries_total,
        snapshot_ok=snapshot_ok,
        # Only stamped when something is actually unloaded, so the common
        # all-loaded line stays compact.
        **({"unloaded": entries_unloaded} if entries_unloaded else {}),
    )

    # Follow-up gate: RUNNING does not imply every async_setup_entry finished
    # (see _wait_for_entries_loaded). Skipped only when the trip-time snapshot
    # POSITIVELY confirmed all-loaded — a failed snapshot (snapshot_ok=False)
    # says nothing about entry state, so it enters the gate too rather than
    # preserving the race behind a transient HTTP/JSON hiccup (Codex review
    # finding on #2040); the gate's own loop keeps polling through snapshot
    # failures and stays bounded by its timeout.
    if entries_unloaded or not snapshot_ok:
        _wait_for_entries_loaded(container, base_url, headers)

    _wait_for_testcontainer_sun(base_url, headers, container, SUN_WAIT)

    # Embedded backend: HA core is up, but the in-process MCP server
    # integration's background bring-up (force-install of the local wheel,
    # token provisioning, worker-thread start, webhook registration) runs
    # after CoreState RUNNING. Wait for its ingress webhook to answer MCP
    # ``initialize`` before yielding so the session mcp_client fixture connects
    # to a live server. This is a REAL bring-up gate — a timeout here means the
    # embedded server genuinely failed to come up, not a flaky environment.
    embedded_webhook_url: str | None = None
    if embedded:
        embedded_webhook_url = f"{base_url}/api/webhook/{_EMBEDDED_WEBHOOK_ID}"
        logger.info("⏳ Waiting for the in-process MCP server webhook to come up...")
        if not _wait_for_embedded_webhook_ready(
            embedded_webhook_url, timeout=_EMBEDDED_BRINGUP_TIMEOUT
        ):
            _dump_ha_readiness_diagnostics(
                container,
                base_url,
                headers,
                label="embedded-webhook-not-ready",
                config_entry_domain=_EMBEDDED_DOMAIN,
            )
            pytest.fail(
                "The in-process MCP server did not answer its ingress "
                f"webhook within {_EMBEDDED_BRINGUP_TIMEOUT}s. Bring-up "
                "(wheel install / token provisioning / server thread / webhook "
                "registration) failed — check the HA log dump above for the "
                f"{_EMBEDDED_DOMAIN} config-entry state and any repair issue."
            )
    return embedded_webhook_url
