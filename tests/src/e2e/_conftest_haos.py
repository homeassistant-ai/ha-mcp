"""HAOS backend setup for the E2E fixtures: image staging, post-boot setup and log dumps."""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import requests

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))  # tests/src/ for haos_runtime
from haos_runtime import (
    HA_MCP_DEV_ADDON_SLUG,
    HA_MCP_SERVER_DOMAIN,
    HA_MCP_SERVER_ENTRY_ID,
    HA_MCP_SERVER_WEBHOOK_ID,
    HA_MCP_WEBHOOK_PROXY_ADDON_SLUG,
    enable_config_entry,
    inject_hacs_token_in_qcow2,
    login_for_token,
    refresh_dev_addon_source_in_qcow2,
    refresh_recorder_in_qcow2,
    remove_tools_entry_in_qcow2,
    set_default_backup_password,
    stage_embedded_server_feature_flags_in_qcow2,
    stage_embedded_server_wheel_in_qcow2,
    stage_home_assistant_tls_in_qcow2,
    trigger_dev_addon_update,
    wait_for_addon_ha_link_ready,
    wait_for_addon_mcp_ready,
)

# Import test constants
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from test_constants import TEST_PASSWORD, TEST_USER

from ._conftest_embedded import (
    _EMBEDDED_BACKUP_OVERRIDES,
    _EMBEDDED_FEATURE_FLAGS,
    _is_no_tools_entry_selected,
    _wait_for_embedded_webhook_ready,
)
from ._conftest_testcontainer import _reset_ha_in_process_caches

logger = logging.getLogger(__name__)


# The haos_embedded lane's bring-up runs a runtime pip install of the whole
# fastmcp tree INSIDE the resource-constrained HAOS QEMU guest (no entrypoint
# preinstall like the container path), so it needs the same generous budget the
# per-test HAOS embedded smoke fixture uses (test_embedded_server_haos.py's
# _READY_TIMEOUT_S). pyproject.toml's timeout_func_only exempts this
# session-fixture wait from the 300s per-test timeout.
_HAOS_EMBEDDED_BRINGUP_TIMEOUT = 600


def _haos_worker_setup(base_image_path: Path) -> Path:
    """Allocate per-worker ports + qcow2 overlay for parallel HAOS runs (#1350).

    Each pytest-xdist worker gets its own QEMU instance, so it needs its
    own port set (otherwise hostfwd collides on bind) and its own
    writable qcow2 (otherwise the recorder-refresh + dev-addon-staging
    mutations race). On a single-worker (no ``-n``) run, ``worker_id``
    is ``"master"`` and we keep the base ports + base image path — same
    behavior as before the parallel work.

    Per-worker qcow2 is a backing-file overlay rather than a full
    reflink copy: GitHub-hosted Linux runners are ext4 (no reflink),
    so ``cp --reflink=auto`` falls back to a real 12 GB copy that
    overflows the runner's 14 GB SSD when multiplied across workers.
    The overlay is tiny at creation (~200 KB) and grows only as the
    worker mutates state on top of the shared read-only base.
    """
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    # pytest-xdist worker IDs are ``gw0``/``gw1``/.../``gwN``. ``master``
    # (no ``-n``) gets offset 0, identical to the pre-parallel behavior.
    if worker_id.startswith("gw") and worker_id[2:].isdigit():
        worker_idx = int(worker_id[2:])
    else:
        worker_idx = 0
    # +100 per worker is large enough that none of the four port sets
    # (HA / SSH / addon MCP / SSH-debug) collide between adjacent
    # workers, even after future port additions.
    offset = worker_idx * 100
    os.environ["HAOS_TEST_HA_PORT"] = str(18123 + offset)
    os.environ["HAOS_TEST_SSH_PORT"] = str(12222 + offset)
    os.environ["HAOS_TEST_ADDON_PORT"] = str(19583 + offset)
    os.environ["HAOS_TEST_SSH_DEBUG_PORT"] = str(22222 + offset)
    if worker_idx == 0 and worker_id == "master":
        # Single-worker run — no overlay needed, mutate the base image
        # directly (matches the pre-parallel path).
        return base_image_path
    overlay_path = base_image_path.with_name(
        f"{base_image_path.stem}-{worker_id}{base_image_path.suffix}"
    )
    if overlay_path.exists():
        overlay_path.unlink()
    subprocess.run(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base_image_path),
            str(overlay_path),
        ],
        check=True,
        capture_output=True,
    )
    logger.info(
        "HAOS parallel: worker %s using port offset %d, overlay qcow2 %s",
        worker_id,
        offset,
        overlay_path,
    )
    return overlay_path


def _prepare_haos_image(
    base_image_path: Path, inaddon: bool, haos_embedded: bool, haos_stdio: bool
) -> Path:
    """Set up the per-worker qcow2 overlay and stage all pre-boot mutations."""
    # Per-worker port + overlay setup for pytest-xdist parallel HAOS
    # (#1350). Single-worker runs short-circuit and reuse the base
    # image path unchanged.
    image_path = _haos_worker_setup(base_image_path)
    logger.info(
        "HAOS backend selected (mode=%s) — booting qcow2 at %s",
        (
            "inaddon"
            if inaddon
            else "embedded"
            if haos_embedded
            else "stdio"
            if haos_stdio
            else "external"
        ),
        image_path,
    )
    # Shift the baked recorder timestamps forward so seeded rows fall
    # inside history's 24h window (same intent as the testcontainer
    # path's _refresh_recorder_timestamps). Must run before boot
    # because HA Core takes an exclusive lock on the DB.
    refresh_recorder_in_qcow2(image_path)
    # Authenticate HACS with the CI GitHub token (parity with the
    # testcontainer path's injection below) so HACS repo adds don't
    # ride the shared-IP 60 req/h unauthenticated GitHub budget —
    # the long-standing HACS-install flake. Must run before boot.
    inject_hacs_token_in_qcow2(image_path)
    # No-tools lanes (#2292): drop the baked "File & YAML Tools" config entry so
    # the privileged ha_mcp_tools services never register. Pre-boot offline edit
    # on this worker's own overlay (HA Core owns .storage once it boots), and it
    # removes ONLY the tools entry — the staged, still-disabled in-process server
    # entry stays, which is what gives the haos_embedded lane its
    # server-entry-only topology.
    if _is_no_tools_entry_selected():
        remove_tools_entry_in_qcow2(image_path)
    # Deliver a checkout-built ha-mcp wheel into /config and point the baked
    # (disabled) in-process server config entry's pip_spec at it, so the HAOS
    # embedded-server E2E (#1527) exercises the PR's own src/ha_mcp when it
    # enables the entry. Best-effort — a failure only affects that one test.
    # Must run before boot (offline qcow2 edit), like the refreshers above.
    stage_embedded_server_wheel_in_qcow2(image_path)
    # haos_embedded lane only: the WHOLE suite runs through the in-process
    # server, so deliver the same settings overrides the container
    # ``embedded`` backend injects — feature flags (yaml editing, filesystem
    # tools, custom component integration, …) into
    # <config>/.ha_mcp/feature_flags.json, and separately the
    # BACKUP_OVERRIDE_FIELDS values (enable_snapshot_delete, #1861) into
    # <config>/.ha_mcp/backup_settings.json — two different override files
    # since ha_mcp.config reads the two registries separately.
    # Gated to this lane so the external / inaddon lanes (green) are untouched —
    # their only embedded consumer is the smoke test, which needs no overrides.
    # Hard-raises on failure (unlike the best-effort wheel staging): the suite
    # depends on these overrides, so a delivery failure should fail setup loudly.
    if haos_embedded:
        stage_embedded_server_feature_flags_in_qcow2(
            image_path, _EMBEDDED_FEATURE_FLAGS
        )
        stage_embedded_server_feature_flags_in_qcow2(
            image_path,
            _EMBEDDED_BACKUP_OVERRIDES,
            filename="backup_settings.json",
        )
    # Inaddon mode: overwrite the baked addon source with PR's current
    # source + bump config.yaml version so Supervisor detects an
    # update-available on next boot. The Supervisor WS API trigger
    # below applies it via Docker layer cache (#1349 item 7).
    if inaddon:
        refresh_dev_addon_source_in_qcow2(image_path)
    if haos_embedded:
        # Best-effort like the wheel staging above: only the final TLS scenario
        # consumes this certificate, and it asserts on HAOS_TEST_TLS_CA_PATH —
        # a staging failure should fail that one test, not abort the lane.
        try:
            certificate = stage_home_assistant_tls_in_qcow2(image_path)
        except RuntimeError:
            logger.warning(
                "HAOS Core TLS staging failed; the TLS scenario will report it",
                exc_info=True,
            )
        else:
            # Do not alter process-wide trust during ordinary tests. The final
            # TLS scenario trusts this cert only while reproducing the legacy
            # mismatch.
            os.environ["HAOS_TEST_TLS_CA_PATH"] = str(certificate)
    return image_path


def _wait_for_haos_sun_ready(base_url: str, haos_headers: dict[str, str]) -> None:
    """Wait for HAOS sun.sun to exist and leave the 'unknown' state."""
    # Mirrors the sun.sun + entity wait loop in the testcontainer
    # branch of ha_container_with_fresh_config: the first tests reach
    # for sun.sun's state immediately, but HA Core may still be
    # propagating registry → state-machine when boot_haos_qemu's
    # /manifest.json gate releases (manifest.json is served by the
    # frontend before all integrations finish loading). Wait for
    # sun.sun to (a) exist, then (b) leave the "unknown" state so
    # template tests don't race.
    sun_url = f"{base_url}/api/states/sun.sun"
    SUN_WAIT = 60
    sun_start = time.monotonic()
    last_sun_err: Exception | None = None
    last_sun_status: int | None = None
    while time.monotonic() - sun_start < SUN_WAIT:
        try:
            sun_resp = requests.get(sun_url, timeout=5, headers=haos_headers)
            last_sun_status = sun_resp.status_code
            if sun_resp.status_code == 200:
                sun_state = sun_resp.json().get("state", "unknown")
                if sun_state != "unknown":
                    elapsed = time.monotonic() - sun_start
                    logger.info(
                        f"✅ HAOS sun.sun is '{sun_state}' after {elapsed:.1f}s"
                    )
                    break
        except (
            requests.exceptions.RequestException,
            json.JSONDecodeError,
        ) as exc:
            last_sun_err = exc
        time.sleep(1)
    else:
        # Surface what we saw on the LAST attempt so a future
        # operator can tell "HA returned 401 for 60s" from
        # "connection refused for 60s" from "endpoint returned
        # 200 but state was 'unknown'".
        logger.warning(
            "HAOS sun.sun still not ready after %ds "
            "(last_status=%s, last_exc=%r) — template / connection "
            "tests may race",
            SUN_WAIT,
            last_sun_status,
            last_sun_err,
        )


def _wait_for_haos_light_ready(base_url: str, haos_headers: dict[str, str]) -> None:
    """Wait for a seeded HAOS light entity to appear in the state machine."""
    # Sun.sun ready means all *integrations* finished setup, but
    # the demo platform that registers ``light.bed_light`` and the
    # other seeded fixtures publishes its initial states *after*
    # its integration's async_setup returns — the recorder + state
    # machine writes are scheduled tasks. Under the parallel
    # HAOS run (-n2) the first test on each worker hits the search
    # / state API before those tasks complete, and search returns
    # ``total_matches=0`` for ``light`` (verified on PR #1379 CI
    # run 26130708983 diagnostics: core.entity_registry has all 6
    # lights, but the state machine had no light.* entries at the
    # moment the test fired). Poll for one of the known seeded
    # light entities so downstream tests don't race.
    light_url = f"{base_url}/api/states/light.bed_light"
    LIGHT_WAIT = 60
    light_start = time.monotonic()
    last_light_status: int | None = None
    while time.monotonic() - light_start < LIGHT_WAIT:
        try:
            light_resp = requests.get(light_url, timeout=5, headers=haos_headers)
            last_light_status = light_resp.status_code
            if light_resp.status_code == 200:
                elapsed = time.monotonic() - light_start
                logger.info(
                    "HAOS light.bed_light is in state machine after %.1fs",
                    elapsed,
                )
                break
        except (
            requests.exceptions.RequestException,
            json.JSONDecodeError,
        ):
            # Boot-phase polling: state machine not ready — retry (#1266).
            pass
        time.sleep(1)
    else:
        logger.warning(
            "HAOS light.bed_light still not in state machine after "
            "%ds (last_status=%s) — search / state tests may race",
            LIGHT_WAIT,
            last_light_status,
        )


def _haos_post_boot_setup(base_url: str, request) -> tuple[str, dict]:
    """Post-boot HAOS setup: token, env, readiness waits, blueprint rewrite."""
    token = login_for_token(base_url, TEST_USER, TEST_PASSWORD)
    # Mirror the env-var setup the testcontainer path does below at
    # ~line 1077 — feature flags for the in-process MCP server, plus
    # HA URL/token for any code reading from env. The cache reset
    # ensures the WebSocket pool and settings pick up the HAOS URL.
    os.environ["HOMEASSISTANT_URL"] = base_url
    os.environ["HOMEASSISTANT_TOKEN"] = token
    # Beta sub-flags require the master to be on too.
    os.environ["ENABLE_BETA_FEATURES"] = "true"
    os.environ["ENABLE_YAML_CONFIG_EDITING"] = "true"
    # Per-key sub-toggles default OFF; the E2E suite covers the
    # whole packages/*.yaml surface so enable all three.
    os.environ["ENABLE_YAML_PACKAGES_AUTOMATION"] = "true"
    os.environ["ENABLE_YAML_PACKAGES_SCRIPT"] = "true"
    os.environ["ENABLE_YAML_PACKAGES_SCENE"] = "true"
    os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = "true"
    # Operator extra YAML write keys (#1887): widen the allowlist with a
    # single non-built-in key so the success-path write test has a key to
    # exercise. Value mirrors _EMBEDDED_FEATURE_FLAGS["extra_yaml_write_keys"].
    os.environ["HA_MCP_EXTRA_YAML_KEYS"] = "alert2"
    # Strict best-practices gate (#1779) defaults ON with its parent;
    # pin it OFF so the suite's keyless writes aren't hard-blocked. The
    # strict-gate e2e test builds its own server with the flag enabled.
    os.environ["ENABLE_STRICT_MANDATORY_BPS"] = "false"
    # Snapshot deletion (#1861) defaults OFF in production; enabled
    # here so the e2e suite can cover the (snapshot, delete) guard
    # chain against a disposable test HAOS instance.
    os.environ["ENABLE_SNAPSHOT_DELETE"] = "true"
    _reset_ha_in_process_caches()
    haos_headers = {"Authorization": f"Bearer {token}"}
    _wait_for_haos_sun_ready(base_url, haos_headers)
    _wait_for_haos_light_ready(base_url, haos_headers)
    # Set HA Core's default backup-create password via WS so
    # ha_backup_create tests pass without a pre-baked seed. Must
    # run AFTER the sun.sun ready-wait above — sun.sun ready
    # implies all integrations have finished loading, including
    # ``backup`` which registers the ``backup/config/update`` WS
    # command. Calling earlier would hit "Unknown command" before
    # the integration's WS handlers are registered. The helper
    # also retries on unknown_command as a belt-and-braces
    # defence against race conditions on slow CI runners.
    # Idempotent — safe across the inaddon dev-addon update.
    set_default_backup_password(base_url, token)
    blueprint_http_server = request.getfixturevalue("_blueprint_http_server")
    # The session-scope _blueprint_http_server fixture computes its
    # base_url using host.docker.internal — meaningless from inside
    # the HAOS QEMU guest. Slirp user networking always reaches the
    # host at 10.0.2.2, so rewrite the URL here for tests that fetch
    # blueprints through HA's import_blueprint flow.
    blueprint_for_haos = {
        **blueprint_http_server,
        "base_url": f"http://10.0.2.2:{blueprint_http_server['port']}",
    }
    return token, blueprint_for_haos


def _bringup_haos_out_of_process_server(
    base_url: str, token: str, inaddon: bool, haos_embedded: bool
) -> tuple[str | None, str | None]:
    """Bring up the inaddon dev-addon or haos_embedded server; return their URLs."""
    addon_mcp_url: str | None = None
    # haos_embedded: URL the mcp_client fixture connects to (the baked
    # in-process MCP server's ingress webhook inside the HAOS VM).
    embedded_webhook_url: str | None = None
    # Inaddon mode: refresh_dev_addon_source_in_qcow2 ran above
    # with a bumped version, so Supervisor now sees an update
    # available. Trigger it via WS supervisor_api (Docker layer
    # cache → only the COPY src/ + uv-sync-project layers
    # re-execute), then wait for the addon's MCP endpoint.
    if inaddon:
        logger.info("Inaddon mode: triggering Supervisor addon update for PR source")
        trigger_dev_addon_update(base_url, token, timeout=600.0)
        addon_mcp_url = wait_for_addon_mcp_ready(timeout=180.0)
        logger.info("Inaddon addon MCP endpoint ready at %s", addon_mcp_url)
        assert addon_mcp_url is not None, (
            "Inaddon setup completed without producing an "
            "addon_mcp_url — wait_for_addon_mcp_ready contract "
            "violation. Downstream mcp_client fixture would fail "
            "with an obscure TypeError on transport construction."
        )
        # The listener answering HTTP does not mean its Home Assistant link is
        # up, so gate on that too — otherwise the session's first HA-backed
        # tool call can land while the Supervisor proxy is still 502ing.
        if not wait_for_addon_ha_link_ready(addon_mcp_url):
            raise AssertionError(
                "The dev addon's MCP listener answered HTTP but no Home "
                "Assistant-backed tool call succeeded before the link "
                "deadline. The addon reaches Core through the Supervisor "
                "WebSocket proxy — see the Supervisor and HA Core logs in "
                "the HAOS diagnostics artifact."
            )
    elif haos_embedded:
        # Enable the baked-disabled in-process server entry ONCE for the
        # whole session (the per-test smoke module is skipped on this
        # lane via not_on_haos_embedded), then wait for its in-process
        # server to install itself, start, and register the webhook —
        # the same webhook the mcp_client fixture then drives for every
        # test. enable_config_entry raises on a WS-level failure (e.g.
        # a missing entry id) so the cause is clear rather than a
        # downstream webhook timeout.
        logger.info(
            "haos_embedded mode: enabling %s and waiting for the "
            "in-process server webhook",
            HA_MCP_SERVER_ENTRY_ID,
        )
        enable_config_entry(base_url, token, HA_MCP_SERVER_ENTRY_ID)
        embedded_webhook_url = f"{base_url}/api/webhook/{HA_MCP_SERVER_WEBHOOK_ID}"
        if not _wait_for_embedded_webhook_ready(
            embedded_webhook_url, timeout=_HAOS_EMBEDDED_BRINGUP_TIMEOUT
        ):
            raise AssertionError(
                "The in-process MCP server did not answer its HAOS "
                f"ingress webhook within {_HAOS_EMBEDDED_BRINGUP_TIMEOUT}s "
                f"of enabling {HA_MCP_SERVER_ENTRY_ID}. Bring-up (runtime "
                "pip install of the fastmcp tree inside HAOS / server "
                "thread / webhook registration) failed — see the HA Core "
                "runtime log in the HAOS diagnostics artifact for the "
                f"{HA_MCP_SERVER_DOMAIN} config-entry state."
            )
    return addon_mcp_url, embedded_webhook_url


def _dump_haos_session_logs(base_url: str, token: str, inaddon: bool) -> None:
    """Pull HA Core + Supervisor (+ addon) logs before QEMU shutdown."""
    # Pull HA Core's runtime log + Supervisor's own log via the
    # Supervisor /core/logs and /supervisor/logs endpoints before
    # QEMU shuts down. HA on HAOS logs to stdout (no file-based
    # home-assistant.log) so this is the only way to see what HA
    # itself said during the session. ?lines=20000 because the
    # default returns just a tail and we lose the boot phase
    # where recorder/integration init errors happen.
    #
    # IMPORTANT: each urlopen has its own 60s timeout so a hung
    # Supervisor caps total teardown delay at 2 endpoints × 60s
    # = 120s before boot_haos_qemu's own SIGTERM/SIGKILL kicks
    # in. Without per-call timeout an indefinitely-hanging
    # supervisor would stall session teardown forever.
    log_dest = Path("/tmp/haos-diagnostics")
    log_dest.mkdir(parents=True, exist_ok=True)
    log_endpoints = [
        (
            "ha-core-runtime.log",
            f"{base_url}/api/hassio/core/logs?lines=20000",
        ),
        (
            "supervisor-runtime.log",
            f"{base_url}/api/hassio/supervisor/logs?lines=20000",
        ),
    ]
    # Inaddon mode: also grab the dev addon container's logs —
    # often the real "Check Supervisor logs for details" detail
    # lives in the addon's own container output rather than
    # Supervisor's. /api/hassio/addons/{slug}/logs IS in
    # HA Core's REST PATHS_ADMIN allowlist (verified at
    # hassio/http.py).
    if inaddon:
        log_endpoints.append(
            (
                "ha-mcp-dev-addon.log",
                f"{base_url}/api/hassio/addons/{HA_MCP_DEV_ADDON_SLUG}/logs?lines=20000",
            ),
        )
    # Always grab the webhook-proxy addon's stdout — it's
    # installed by the bake (boot=manual) and started by the
    # haos_only test module's session fixture. When tests in
    # that module fail, the addon's own logs are the only
    # place start.py's failure mode is visible (Supervisor's
    # log only shows container lifecycle events, not addon
    # stdout).
    log_endpoints.append(
        (
            "webhook-proxy-addon.log",
            f"{base_url}/api/hassio/addons/"
            f"{HA_MCP_WEBHOOK_PROXY_ADDON_SLUG}/logs?lines=20000",
        ),
    )
    # Narrow except: any non-network error (NameError, KeyError
    # from a future refactor) should propagate instead of being
    # misreported as "Failed to dump". Per-endpoint network
    # failures are still per-mortem and shouldn't kill teardown.
    for name, url in log_endpoints:
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                (log_dest / name).write_bytes(resp.read())
            logger.info("Dumped %s via supervisor", name)
        except (
            OSError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
        ) as exc:
            logger.warning("Failed to dump %s: %s", name, exc)
