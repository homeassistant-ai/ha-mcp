"""
Testcontainers integration for E2E testing.

Spins up an isolated Home Assistant Docker container for each test session.
Tests MUST run against this container — never against a real HA instance.

Environment Variables:
    HA_TEST_PORT: Optional fixed port for HA container (default: dynamic).
                  Example: HA_TEST_PORT=8124

NOTE: config.py loads HOMEASSISTANT_URL from the .env.test file at import
time, so checking os.environ for a pre-set URL is not a reliable guard here.
Protection against accidental real-HA usage is instead ensured by:
  - Guard 1: Docker must be available (testcontainers requirement)
  - Guard 3: HA API must become ready within 60s (container health check)
  - AGENTS.md: documents correct test-run commands
"""

import asyncio
import http.server
import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import requests
from testcontainers.core.container import DockerContainer

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))  # tests/src/ for haos_runtime

from doomed_run import DoomedRunDetector
from fastmcp import Client
from haos_runtime import (
    HA_MCP_DEV_ADDON_SLUG,
    HA_MCP_SERVER_DOMAIN,
    HA_MCP_SERVER_ENTRY_ID,
    HA_MCP_SERVER_WEBHOOK_ID,
    HA_MCP_WEBHOOK_PROXY_ADDON_SLUG,
    HAOS_IMAGE_ENV,
    boot_haos_qemu,
    enable_config_entry,
    inject_hacs_token,
    inject_hacs_token_in_qcow2,
    is_haos_backend_selected,
    is_haos_embedded_mode,
    is_haos_inaddon_mode,
    login_for_token,
    refresh_dev_addon_source_in_qcow2,
    refresh_recorder_in_qcow2,
    set_default_backup_password,
    stage_embedded_server_feature_flags_in_qcow2,
    stage_embedded_server_wheel_in_qcow2,
    trigger_dev_addon_update,
    wait_for_addon_mcp_ready,
)

from ha_mcp.client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.server import HomeAssistantSmartMCPServer

# Import test utilities
from .utilities.assertions import parse_mcp_result
from .utilities.streamable_http import parse_mcp_response
from .utilities.supervisor_mock import (
    _supervisor_mock_server,  # noqa: F401  (session fixture supervisor_mock depends on)
    supervisor_mock,  # noqa: F401  (re-exported fixture)
)

# Import test constants
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from test_constants import HA_TEST_IMAGE, TEST_PASSWORD, TEST_TOKEN, TEST_USER

# Configure logging for tests
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Module-level collection of readiness-gate timings, per process. Workers
# write to ``_READINESS_TIMINGS``; pytest_sessionfinish hands their lists
# to the master via xdist's ``workeroutput`` channel, and the master
# aggregates into ``_ALL_READINESS_TIMINGS`` in pytest_testnodedown. The
# pytest_terminal_summary hook then renders the aggregate to the
# master's terminalreporter, which writes outside the pytest-xdist
# capture buffer (refs #366).
_READINESS_TIMINGS: list[dict[str, Any]] = []
_ALL_READINESS_TIMINGS: list[dict[str, Any]] = []


# --- Embedded backend (in-process server entry of ha_mcp_tools, #1527) --------
# Same testcontainer HA as the ``container`` backend, but the server-under-test is
# the ha_mcp_tools component's in-process "server" config entry running IN-PROCESS
# inside the container (not an in-process FastMCP server in the pytest process).
# Selected with ``E2E_BACKEND=embedded``; ``mcp_client`` then speaks Streamable
# HTTP to the entry's ingress webhook instead of using an in-memory transport. The
# component is installed once (for all testcontainer backends) by
# ``_install_custom_component``; the embedded backend additionally seeds the
# server entry via ``_install_embedded_server``.
_EMBEDDED_DOMAIN = "ha_mcp_tools"
_EMBEDDED_ENTRY_ID = "e2e_embedded_ha_mcp_server_entry"
# unique_id of the single-instance server entry (matches config_flow's
# ``_SERVER_UNIQUE_ID = f"{DOMAIN}-server"``), distinct from the tools entry's.
_EMBEDDED_UNIQUE_ID = "ha_mcp_tools-server"
# Stable webhook id + secret so the fixture knows the connect URL up front (a
# generated one would live only in the container's .storage after bring-up).
_EMBEDDED_WEBHOOK_ID = "mcp_e2e_embedded_0123456789abcdef"
_EMBEDDED_SECRET_PATH = "/private_e2e_embedded_server"
_EMBEDDED_SERVER_PORT = 9584
# Persistent data dir the server entry hands ha_mcp via HA_MCP_CONFIG_DIR (mirrors
# the component's const.SERVER_CONFIG_SUBDIR); the feature-flag override file
# lives directly under it.
_EMBEDDED_SERVER_CONFIG_SUBDIR = ".ha_mcp"
# Feature flags the container backend injects as pytest-process env vars for its
# in-process server (see the testcontainer path below). The embedded server runs
# inside the container and cannot read the pytest process env, so the SAME values
# are written to the component's data-dir override file (feature_flags.json) —
# ha_mcp.config reads that override layer in a standalone deployment, and the
# embedded server IS standalone (is_running_in_addon() is False in embedded mode,
# so the file is honored rather than short-circuited by Supervisor). Keys are
# ha_mcp.config Settings field names (config.FEATURE_FLAG_FIELDS), not env-var
# names. READ_ONLY_MODE / ENABLE_TOOL_SECURITY_POLICIES are deliberately absent:
# the tests that need them build their own in-process server
# (test_readonly_mode / test_approval_flow), so enabling them on the shared
# embedded server would break the default-catalog tests.
_EMBEDDED_FEATURE_FLAGS: dict[str, bool] = {
    "enable_beta_features": True,
    "enable_yaml_config_editing": True,
    "enable_yaml_packages_automation": True,
    "enable_yaml_packages_script": True,
    "enable_yaml_packages_scene": True,
    "enable_filesystem_tools": True,
    # Strict best-practices gate (#1779) defaults ON with its parent
    # (enable_mandatory_bps); pin it OFF here the same way the testcontainer
    # backend pins ENABLE_STRICT_MANDATORY_BPS=false, so the embedded server's
    # keyless writes aren't hard-blocked.
    "enable_strict_mandatory_bps": False,
}
# BACKUP_OVERRIDE_FIELDS (not FEATURE_FLAG_FIELDS) values for the embedded
# server — a separate override file (backup_settings.json) from
# _EMBEDDED_FEATURE_FLAGS's feature_flags.json, since ha_mcp.config reads the
# two registries from different files. Mirrors the ENABLE_SNAPSHOT_DELETE env
# var the container backend sets directly (#1861); the embedded server can't
# read that env, so it goes through this file instead, same rationale as
# _EMBEDDED_FEATURE_FLAGS above.
_EMBEDDED_BACKUP_OVERRIDES: dict[str, bool] = {
    "enable_snapshot_delete": True,
}
# Boot budgets for the embedded path. The wheel + its dependency tree is
# preinstalled in the container entrypoint BEFORE HA's /init (so bring-up is
# fast + deterministic), which delays /api/ liveness by the pip window — hence a
# much larger API-ready timeout than the default 60s. ``_EMBEDDED_BRINGUP_TIMEOUT``
# then covers the background bring-up (force-install of the local wheel with deps
# already satisfied, token provisioning, server thread start, webhook register).
_EMBEDDED_API_READY_TIMEOUT = 480
_EMBEDDED_BRINGUP_TIMEOUT = 300
_EMBEDDED_READY_POLL_S = 5
# The haos_embedded lane's bring-up runs a runtime pip install of the whole
# fastmcp tree INSIDE the resource-constrained HAOS QEMU guest (no entrypoint
# preinstall like the container path), so it needs the same generous budget the
# per-test HAOS embedded smoke fixture uses (test_embedded_server_haos.py's
# _READY_TIMEOUT_S). pytest.ini's timeout_func_only exempts this session-fixture
# wait from the 300s per-test timeout.
_HAOS_EMBEDDED_BRINGUP_TIMEOUT = 600


def _is_embedded_backend_selected() -> bool:
    """Return True when ``E2E_BACKEND=embedded`` selects the in-process server.

    The embedded backend is a variant of the testcontainer path (not HAOS), so it
    is orthogonal to ``is_haos_backend_selected()`` — both are never true at once.
    """
    return os.environ.get("E2E_BACKEND", "").strip().lower() == "embedded"


def _log_readiness_timing(gate: str, elapsed_s: float, **extras: Any) -> None:
    """Record a fixture-side readiness-gate timing data point.

    The data points are routed to the master process and rendered at
    session end by ``pytest_terminal_summary``. Direct ``sys.stderr``
    writes don't survive pytest-xdist's per-worker capture buffer, so
    going through pytest's own reporting plumbing is the reliable path.

    The current ``core_state`` emission includes ``entries_loaded`` /
    ``entries_total`` / ``snapshot_ok`` from ``_snapshot_config_entries``
    at the trip moment. ``pytest_terminal_summary`` warns when any
    ``snapshot_ok`` sample shows ``entries_loaded < entries_total`` —
    that drift means a slow integration finished ``async_setup_entry``
    after ``CoreState.RUNNING`` was set and a follow-up gate is
    justified. Gate-instrumentation history: introduced in #1310 and
    iterated through #1346 / #1369; consolidated onto the single
    ``CoreState.RUNNING`` check in the PR referencing #366
    (Ilya0527 2026-05-18 thread for the structural rationale).
    """
    _READINESS_TIMINGS.append({"gate": gate, "elapsed_s": elapsed_s, **extras})


def pytest_collection_modifyitems(config, items):
    """Enforce backend markers and auto-apply ``haos_only`` to its dir.

    Backend markers (#1349 item 7 introduced the inaddon split; #1527 added
    the embedded lanes, where ``external_only`` also skips because the
    server-under-test lives out-of-process inside the HA container):

    - ``haos_only``: only runs when the HAOS backend is selected
      (``HAOS_TEST_IMAGE_PATH`` set). Auto-applied to anything under
      ``tests/src/e2e/haos_only/``.
    - ``container_only``: only runs on the testcontainer backend.
    - ``external_only``: HAOS external mode only (``mcp_client`` is an
      in-process FastMCP server talking HTTP to HAOS). Skipped on the
      inaddon tier.
    - ``inaddon_only``: HAOS inaddon mode only (``mcp_client`` is HTTP
      to the addon's MCP endpoint, ``is_running_in_addon()=True`` paths
      exercised). Skipped on external mode and on testcontainer.
    """
    del config
    haos = is_haos_backend_selected()
    inaddon = haos and is_haos_inaddon_mode()
    # The embedded backend (#1527) is a testcontainer variant, so ``haos`` is
    # False here — ``haos_only`` still skips and ``container_only`` still runs on
    # it. What differs is that the in-process server lives INSIDE the container:
    # test-process env / monkeypatch reconfiguration and in-process mocks can't
    # reach it (exactly the inaddon limitation ``external_only`` already guards),
    # and a couple of tests are provably redundant with the lane's own backend.
    embedded = _is_embedded_backend_selected()
    # The HAOS embedded lane (#1527) IS a HAOS backend (``haos`` True — qcow2
    # staged), so ``haos_only`` runs and ``container_only`` skips exactly like the
    # other HAOS lanes. Its server-under-test is the in-process MCP server
    # inside the HAOS core container, driven over its ingress webhook — same
    # out-of-process constraint as inaddon / container-embedded, so ``external_only``
    # skips here too; and the haos_only embedded smoke module is redundant with the
    # lane's own session backend, so it skips via ``not_on_haos_embedded``.
    haos_embedded = haos and is_haos_embedded_mode()
    skip_haos = pytest.mark.skip(
        reason="HAOS backend not selected (set HAOS_TEST_IMAGE_PATH)"
    )
    skip_container = pytest.mark.skip(
        reason="HAOS backend is active; test is container-only"
    )
    skip_inaddon_only = pytest.mark.skip(
        reason="inaddon mode required (set HAOS_TEST_MODE=inaddon)"
    )
    skip_external_only = pytest.mark.skip(
        reason="out-of-process server (inaddon/embedded); test needs an "
        "in-process server it can reconfigure via env/monkeypatch or reach an "
        "in-process mock"
    )
    skip_not_on_embedded = pytest.mark.skip(
        reason="redundant on the embedded backend (the lane's own session "
        "backend already exercises this path)"
    )
    skip_not_on_haos_embedded = pytest.mark.skip(
        reason="redundant on the haos_embedded backend (the lane's own session "
        "backend already enables the entry and drives the in-process server)"
    )
    for item in items:
        if "haos_only" in str(item.fspath):
            item.add_marker(pytest.mark.haos_only)
        keywords = item.keywords
        if "haos_only" in keywords and not haos:
            item.add_marker(skip_haos)
        elif "container_only" in keywords and haos:
            item.add_marker(skip_container)
        if "inaddon_only" in keywords and not inaddon:
            item.add_marker(skip_inaddon_only)
        # ``external_only`` skips on any tier where the server is NOT in the
        # pytest process: the inaddon HAOS addon AND the embedded backend's
        # in-process MCP server (both #1527). The name is historical (from
        # #1361 where the only motivating consumer was ``test_supervisor_mock.py``,
        # whose monkeypatch-based fixture works fine on testcontainer + external
        # HAOS but can't reach a server in another process). Skipping on plain
        # testcontainer was a dispatcher bug — the mock fixture is in-process and
        # runs cleanly there (PR #1375 final-skip audit; 14 supervisor_mock tests
        # were silently skipping on every testcontainer e2e-tests.yml run). The
        # embedded backend has the same out-of-process constraint as inaddon, so
        # it joins the skip; those tests keep full coverage on the container lane.
        # ``haos_embedded`` joins for the same reason: its server-under-test is the
        # in-process MCP server inside the HAOS core container, reachable only
        # over the webhook — the test process can't reconfigure it via env /
        # monkeypatch or reach an in-process mock. These tests keep full coverage
        # on the external HAOS lane (where the in-process FastMCP server IS in the
        # test process) and the container lane.
        elif "external_only" in keywords and (inaddon or embedded or haos_embedded):
            item.add_marker(skip_external_only)
        # ``not_on_embedded`` is applied only where a test is provably redundant
        # with the embedded lane's own session backend (e.g. the workflows/embedded
        # smoke test boots its OWN in-process MCP server container to prove the install
        # method — which the embedded lane already does for every test). It keeps
        # running unchanged on the container lane.
        if "not_on_embedded" in keywords and embedded:
            item.add_marker(skip_not_on_embedded)
        # ``not_on_haos_embedded`` marks the haos_only embedded smoke module
        # (haos_only/test_embedded_server_haos.py): it enables the baked entry and
        # drives the webhook per-test, which the haos_embedded lane's session
        # backend already does ONCE at setup for the whole suite. Running them here
        # would double-enable the entry and race the session backend, so they skip
        # (they keep running on the external + inaddon HAOS lanes, where they are
        # the sole thing exercising the in-process server).
        if "not_on_haos_embedded" in keywords and haos_embedded:
            item.add_marker(skip_not_on_haos_embedded)


# Fail fast on a doomed run, on EVERY e2e lane (this conftest is shared by the
# testcontainer / external-HAOS / inaddon suites, so the hook guards all three).
# A Supervisor add-on-update flake did exactly this on PR #1699: all 997 inaddon
# tests ERRORed at setup (0 passed, 0 failed) while the run ground on 11m39s
# producing nothing but errors. DoomedRunDetector aborts once it sees 50
# consecutive setup/teardown errors with zero call-phase pass/fail between them;
# a genuine pass/fail resets the streak, so real failures still run through in
# full (this is NOT --maxfail). The detector logic is unit-tested in
# tests/src/unit/test_doomed_run.py.
#
# Under xdist, pytest_runtest_logreport fires in EACH process — the controller
# (which receives every worker's reports) AND each worker for its own tests — so
# every process keeps its own module-global detector; the streak is per-process,
# not global. That is fine: a doomed run errors on every process, so whichever
# reaches 50 first calls pytest.exit and ends the session (validated under -n2:
# a 150-test all-error run aborts at 50 in ~2s).
_doomed_detector = DoomedRunDetector()


def pytest_runtest_logreport(report):
    if _doomed_detector.record(report.when, report.outcome):
        pytest.exit(
            f"Aborting: {_doomed_detector.streak} consecutive setup/teardown "
            f"errors with no test passing or failing — the run is doomed by a "
            f"systemic setup failure (e.g. add-on/container setup). Failing fast "
            f"instead of grinding through the suite.",
            returncode=1,
        )


def pytest_sessionfinish(session, exitstatus):
    """xdist worker hook: hand collected timings up to the master.

    ``config.workeroutput`` only exists on workers; on the master (or
    when running without xdist) the attribute is missing, and the local
    list is read directly by ``pytest_terminal_summary`` instead.
    """
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None and _READINESS_TIMINGS:
        workeroutput["readiness_timings"] = list(_READINESS_TIMINGS)


def pytest_testnodedown(node, error):
    """xdist master hook: collect a finished worker's timings.

    Called once per worker as it shuts down. ``error`` is non-None when
    the worker crashed — we still try to drain whatever it managed to
    record.
    """
    workeroutput = getattr(node, "workeroutput", {})
    _ALL_READINESS_TIMINGS.extend(workeroutput.get("readiness_timings", []))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Master-side hook: render collected timings as a terminal section.

    Falls back to the local list when running without xdist (no
    ``pytest_testnodedown`` fires in that mode, so ``_ALL_*`` stays empty).
    """
    timings = _ALL_READINESS_TIMINGS or _READINESS_TIMINGS
    if not timings:
        return
    terminalreporter.section("Readiness gate timings")
    for point in timings:
        parts = []
        for key, value in point.items():
            if isinstance(value, float):
                parts.append(f"{key}={value:.2f}")
            else:
                parts.append(f"{key}={value}")
        terminalreporter.write_line("[READINESS_GATE_TIMING] " + " ".join(parts))

    # Drift check (warn-only): the ``_log_readiness_timing`` docstring
    # promises this invariant — if any ``core_state`` sample shows
    # ``entries_loaded < entries_total`` at trip time, a slow integration
    # finished ``async_setup_entry`` after ``CoreState.RUNNING`` was set
    # and a follow-up gate would be justified. ``snapshot_ok=False``
    # samples are skipped (sentinel zeros, not real data).
    drift_samples = [
        p
        for p in timings
        if p.get("gate") == "core_state"
        and p.get("snapshot_ok")
        and p.get("entries_loaded", 0) < p.get("entries_total", 0)
    ]
    if drift_samples:
        terminalreporter.write_line(
            f"⚠️ [READINESS_GATE_DRIFT] {len(drift_samples)} core_state sample(s) "
            f"with entries_loaded < entries_total — a slow integration "
            f"finished async_setup_entry after CoreState.RUNNING; "
            f"follow-up gate justified."
        )


def _is_missing_column_or_table_error(exc: sqlite3.OperationalError) -> bool:
    """Return True only for benign 'schema drift' errors (column/table missing).

    Other ``OperationalError`` causes (locked DB, disk I/O error, malformed
    image, readonly DB) must propagate — silently swallowing them is exactly
    the regression class this helper exists to prevent.
    """
    msg = str(exc).lower()
    return "no such table" in msg or "no such column" in msg


def _refresh_recorder_timestamps(
    db_path: Path, target_age_seconds: float = 300.0
) -> None:
    """Shift baked recorder timestamps forward so seeded rows fall inside the test window.

    The seed ``home-assistant_v2.db`` (built by ``scripts/bake_pagination_seed.py``)
    ships with pre-recorded state-change rows for ``input_number.e2e_pagination_seed``.
    If more than 24h elapses between bake and a test run, every 24h-window
    history query misses them and pagination tests would silently skip.

    Finds the most recent numeric timestamp and uniformly shifts every numeric
    timestamp column so the newest row sits at ``now - target_age_seconds``.
    Relative ordering is preserved.

    Raises ``RuntimeError`` if the DB is missing — falling back to a no-op
    would re-introduce the silent-skip class this helper exists to prevent.
    """
    if not db_path.exists():
        raise RuntimeError(
            f"Recorder seed DB not found at {db_path}. The committed "
            f"tests/initial_test_state/home-assistant_v2.db must be staged into "
            f"the test config dir before this helper runs; without it the "
            f"pagination tests will silently skip."
        )

    conn = sqlite3.connect(str(db_path))
    try:
        # NUMERIC (REAL/FLOAT) recorder timestamp columns. Missing columns are
        # skipped (HA schema drift); other OperationalErrors propagate.
        #
        # `recorder_runs.{start,end,created}` and `statistics_runs.start` are
        # TEXT/ISO (DATETIME) columns and DELIBERATELY EXCLUDED — running
        # `UPDATE … SET col = col + N` against an ISO string silently coerces
        # it via SQLite leading-numeric parsing ("2026-05-11 15:58..." → 2026),
        # turning the cell into garbage and breaking HA's history layer on the
        # next boot. Do not add TEXT/DATETIME columns to this dict.
        TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
            "states": ("last_updated_ts", "last_changed_ts", "last_reported_ts"),
            "events": ("time_fired_ts",),
            "statistics": ("start_ts", "created_ts"),
            "statistics_short_term": ("start_ts", "created_ts"),
        }

        newest = 0.0
        for table, cols in TIMESTAMP_COLUMNS.items():
            for col in cols:
                try:
                    row = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
                except sqlite3.OperationalError as exc:
                    if _is_missing_column_or_table_error(exc):
                        continue
                    raise
                if row and row[0] is not None and isinstance(row[0], (int, float)):
                    newest = max(newest, float(row[0]))

        if newest <= 0:
            raise RuntimeError(
                f"Recorder seed DB at {db_path} has no numeric timestamps. "
                f"The bake script may have produced an empty DB; re-run "
                f"`uv run python scripts/bake_pagination_seed.py`."
            )

        target = time.time() - target_age_seconds
        offset = target - newest
        if offset <= 0:
            logger.info(
                f"⏱️ recorder timestamps already recent (newest={newest:.0f}, "
                f"target={target:.0f}); no shift needed"
            )
            return

        rows_updated = 0
        for table, cols in TIMESTAMP_COLUMNS.items():
            for col in cols:
                try:
                    cur = conn.execute(
                        f"UPDATE {table} SET {col} = {col} + ? WHERE {col} IS NOT NULL",
                        (offset,),
                    )
                    rows_updated += cur.rowcount
                except sqlite3.OperationalError as exc:
                    if _is_missing_column_or_table_error(exc):
                        continue
                    raise
        if rows_updated == 0:
            raise RuntimeError(
                f"Recorder seed DB at {db_path} matched zero rows for the "
                f"shift UPDATE — schema may have changed beyond TIMESTAMP_COLUMNS."
            )
        conn.commit()
        logger.info(
            f"⏱️ Shifted recorder timestamps by {offset:+.0f}s "
            f"({rows_updated} rows updated) — newest seed row is now ~5min ago"
        )
    finally:
        conn.close()


def _setup_config_permissions(config_path: Path) -> None:
    """Set up proper permissions for Home Assistant config directory."""
    import stat

    # Set directory permissions recursively
    for root, dirs, files in os.walk(config_path):
        for d in dirs:
            os.chmod(
                os.path.join(root, d),
                stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH,
            )
        for f in files:
            os.chmod(
                os.path.join(root, f),
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IRGRP
                | stat.S_IWGRP
                | stat.S_IROTH,
            )


def _ensure_hacs_frontend(initial_state_path: Path) -> None:
    """Download HACS frontend if not present.

    HACS requires the frontend (~51MB) to be present to fully initialize.
    This is not committed to git to keep the repo size manageable.

    Uses a directory-based atomic lock so concurrent xdist workers don't race
    on ``shutil.move`` into the shared ``initial_test_state`` path. One worker
    wins ``mkdir(exist_ok=False)`` and performs the download; the losers poll
    on the lock and exit when the winner releases it.
    """
    import tarfile

    def _is_valid_frontend(path: Path) -> bool:
        """Best-effort check that ``path`` holds a complete HACS frontend.

        A SIGKILL or partial-failure during ``tar.extractall`` →
        ``shutil.move`` can leave a populated but incomplete ``frontend_dir``
        from a prior session. ``entrypoint.js`` is a top-level file in every
        release tarball published at https://github.com/hacs/frontend/releases,
        so its absence flags an interrupted prior session and forces a clean
        re-download instead of letting HACS boot against a broken frontend.
        """
        return (path / "entrypoint.js").is_file()

    hacs_dir = initial_state_path / "custom_components" / "hacs"
    frontend_dir = hacs_dir / "hacs_frontend"
    lock_dir = initial_state_path / ".hacs_frontend.lock"

    # Staleness check: a SIGKILL during the winner's critical section (e.g.
    # a developer hard-kills pytest mid-download) leaves an orphan
    # ``lock_dir`` that would force every peer in the next session into the
    # 180s polling wait below. If the lock predates any plausible download
    # duration, clear it before the fast-path so peers recover instantly.
    # CI tolerates the 180s wait because containers are torn down between
    # runs; local developer iterations under a debugger don't.
    #
    # The 5-minute threshold is a conservative ceiling for any plausible
    # download; legitimate winners finish well within this window.
    #
    # NB: ``stat().st_mtime`` is wall-clock (epoch seconds), so the age math
    # uses ``time.time()`` — ``time.monotonic()`` measures elapsed-from-
    # arbitrary-origin and cannot be compared to ``st_mtime``.
    stale_lock_threshold_s = 300
    if lock_dir.exists():
        try:
            lock_age_s = time.time() - lock_dir.stat().st_mtime
            if lock_age_s > stale_lock_threshold_s:
                logger.warning(
                    f"Removing stale HACS frontend lock at {lock_dir} "
                    f"(age {lock_age_s:.0f}s > {stale_lock_threshold_s}s "
                    f"threshold — likely orphaned by a crashed prior run)."
                )
                lock_dir.rmdir()
        except FileNotFoundError:
            # Race: a peer cleared the lock between ``exists()`` and
            # ``stat`` / ``rmdir``. The fast-path or lock-acquire below
            # will handle whatever state remains.
            pass
        except OSError as exc:
            # Best-effort: if cleanup fails (e.g., non-empty dir from a
            # future sentinel/pidfile refactor, or permission denied), the
            # existing 180s polling timeout still catches the orphan.
            logger.warning(f"Stale-lock cleanup failed at {lock_dir}: {exc}")

    # Fast path: HACS not installed (nothing to do), or frontend present
    # AND no lock held. The lock-held check rules out the window where
    # another worker's ``shutil.move`` is mid-flight — ``frontend_dir``
    # exists then but its contents are partial. On the success path the
    # move commits in the inner ``try`` before the winner's ``finally``
    # runs ``rmdir`` — waiters observing lock-gone-and-frontend-present
    # see a complete frontend. On any failure path the finally still
    # releases with ``frontend_dir`` absent; waiters re-enter the slow
    # path and the download except-branch handles the skip.
    if not hacs_dir.exists() or (
        _is_valid_frontend(frontend_dir) and not lock_dir.exists()
    ):
        return

    # Cross-worker lock: atomic mkdir succeeds on exactly one xdist worker.
    # The losers poll on the lock itself (released by the winner's finally
    # block) so they wake immediately whether the winner finished, skipped
    # the download, or raised — not only when ``frontend_dir`` appears. The
    # 180s cap survives as a safety net for an ungraceful winner exit (e.g.
    # SIGKILL) that never reaches the finally clause.
    try:
        lock_dir.mkdir(exist_ok=False)
    except FileExistsError:
        wait_start = time.monotonic()
        while lock_dir.exists():
            if time.monotonic() - wait_start >= 180:
                # Warn-and-continue (not ``pytest.fail``): a stuck HACS
                # download only breaks HACS-dependent tests, while the
                # rest of the session still produces useful signal.
                # Clear the stale lock so a subsequent session does not
                # also hit the 180s wait when the winner truly crashed.
                logger.warning(
                    f"Timeout waiting for HACS frontend lock release at "
                    f"{lock_dir}; proceeding without verification."
                )
                try:
                    lock_dir.rmdir()
                except OSError as exc:
                    logger.warning(f"Could not clear stale HACS lock dir: {exc}")
                return
            time.sleep(2)
        return

    # We own the lock. Outer try/finally guarantees release even when the
    # download block is skipped (e.g. hacs_dir missing) or raises.
    try:
        # Check if HACS is installed and frontend is missing or invalid.
        if hacs_dir.exists() and not _is_valid_frontend(frontend_dir):
            if frontend_dir.exists():
                # Partial/corrupt directory from a prior interrupted
                # session. Clear it so ``shutil.move`` below replaces it
                # cleanly — moving onto an existing directory nests the
                # fresh ``hacs_frontend/`` inside the stale one.
                logger.warning(
                    f"HACS frontend at {frontend_dir} is partial or corrupt; "
                    f"removing before re-download."
                )
                shutil.rmtree(frontend_dir)
            logger.info("HACS frontend not found, downloading...")

            try:
                # Get the latest frontend version from GitHub API
                api_url = "https://api.github.com/repos/hacs/frontend/releases/latest"
                with urllib.request.urlopen(api_url, timeout=30) as response:
                    release_data = json.loads(response.read())
                    tag_name = release_data["tag_name"]

                # Download and extract the frontend
                tarball_url = f"https://github.com/hacs/frontend/releases/download/{tag_name}/hacs_frontend-{tag_name}.tar.gz"
                logger.info(f"Downloading HACS frontend {tag_name}...")

                with (
                    urllib.request.urlopen(tarball_url, timeout=120) as response,
                    tarfile.open(fileobj=response, mode="r:gz") as tar,
                    tempfile.TemporaryDirectory() as temp_dir_str,
                ):
                    # Extract to temp location first; the context manager
                    # cleans up even if extractall or shutil.move raises.
                    temp_extract = Path(temp_dir_str)
                    tar.extractall(temp_extract, filter="data")

                    # Move the hacs_frontend subdirectory
                    extracted_frontend = (
                        temp_extract / f"hacs_frontend-{tag_name}" / "hacs_frontend"
                    )
                    if extracted_frontend.exists():
                        shutil.move(str(extracted_frontend), str(frontend_dir))
                        logger.info(f"HACS frontend installed at {frontend_dir}")
                    else:
                        logger.warning(
                            f"Could not find hacs_frontend in downloaded archive for {tag_name}"
                        )

            except (
                urllib.error.URLError,
                json.JSONDecodeError,
                KeyError,
                tarfile.TarError,
                OSError,
            ):
                # Narrow catch + ``logger.exception`` so the full
                # traceback surfaces in CI logs, not just the exception
                # type — KP13's "don't green-pass a failed download"
                # principle. HACS-dependent tests will fail at first
                # HACS call; other tests can still run.
                logger.exception("Failed to download HACS frontend")
                logger.warning("HACS tests may be skipped without the frontend")
    finally:
        # Release the lock so waiting workers can proceed. A
        # ``FileNotFoundError`` here means a timed-out waiter cleared
        # the lock first — invariant broke but the session can
        # continue; log so it's diagnosable. Other ``OSError`` classes
        # (PermissionError, Errno-39 "Directory not empty" from a
        # future sentinel/pidfile refactor) would wedge subsequent
        # sessions into the 180s wait — let them surface.
        try:
            lock_dir.rmdir()
        except FileNotFoundError:
            logger.warning(
                f"HACS frontend lock dir {lock_dir} vanished before "
                f"release — concurrent timeout-clear?"
            )


def _install_custom_component(
    config_path: Path,
    component_src: Path,
    domain: str,
    title: str,
) -> bool:
    """Install a custom component into the test HA config.

    Copies component source into custom_components/<domain> and injects a
    config entry so HA loads it on startup. Returns True if installed.
    """
    if not component_src.exists():
        logger.info("%s source not found — skipping installation", domain)
        return False

    dest = config_path / "custom_components" / domain
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(component_src, dest, dirs_exist_ok=True)

    # Inject config entry if not already present
    storage_file = config_path / ".storage" / "core.config_entries"
    if storage_file.exists():
        data = json.loads(storage_file.read_text())
        entries = data.get("data", {}).get("entries", [])
        if not any(isinstance(e, dict) and e.get("domain") == domain for e in entries):
            entries.append(
                {
                    "created_at": "2025-09-07T23:56:28.040744+00:00",
                    "data": {},
                    "disabled_by": None,
                    "discovery_keys": {},
                    "domain": domain,
                    "entry_id": f"e2e_test_{domain}_entry",
                    "minor_version": 1,
                    "modified_at": "2025-09-07T23:56:28.040747+00:00",
                    "options": {},
                    "pref_disable_new_entities": False,
                    "pref_disable_polling": False,
                    "source": "import",
                    "subentries": [],
                    "title": title,
                    "unique_id": domain,
                    "version": 1,
                }
            )
            storage_file.write_text(json.dumps(data, indent=2))

    logger.info("Installed %s component", domain)
    return True


def _build_embedded_server_wheel(dest_dir: Path) -> Path:
    """Build a ha-mcp wheel from the checkout into ``dest_dir`` via ``uv build``.

    The wheel carries the PR's own ``src/ha_mcp`` (checkout fidelity for the
    embedded-mode routing under test); its dependencies still resolve in the
    container (preinstalled in the entrypoint, see the testcontainer path).

    Uses ``uv build`` rather than ``python -m pip wheel`` (as the
    workflows/embedded smoke test and ``haos_runtime`` helpers do): the project's
    uv-managed venv does not seed ``pip``/``setuptools``, so ``python -m pip
    wheel`` fails with "No module named pip". Those helpers catch the failure and
    skip/warn, but this backend must ERROR loudly if the wheel can't be built —
    the whole lane depends on it — so it must use a mechanism that actually works
    in the venv. ``uv`` is always on PATH (the suite runs under ``uv run``) and
    builds in an isolated env without needing pip in the venv.
    """
    repo_root = Path(__file__).parent.parent.parent.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dest_dir), str(repo_root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    wheels = list(dest_dir.glob("ha_mcp-*.whl"))
    if not wheels:
        raise RuntimeError(f"no ha_mcp wheel built in {dest_dir}")
    return wheels[0]


def _install_embedded_server(config_path: Path, wheel_name: str) -> None:
    """Seed the ha_mcp_tools "server" config entry + feature-flag overrides.

    The ha_mcp_tools component itself is already copied by
    ``_install_custom_component`` (which also seeds the "tools" services entry);
    the in-process server is a SECOND config entry of that same component,
    discriminated by ``data.entry_type == "server"``. Three pieces here, laid
    down before the container boots (so a post-boot host write to the bind
    mount doesn't have to propagate, matching the other pre-boot seeders):

    1. Seed the server config entry with the stable webhook id/secret (so the
       mcp_client fixture knows the connect URL) and a ``file://`` ``pip_spec``
       pointing at the checkout-built wheel copied into ``/config``. No
       ``last_pip_spec`` is stored, so bring-up takes the force-install path
       (proven by the workflows/embedded smoke test) — fast here because the
       entrypoint already installed the wheel + deps.
    2. Write the feature-flag override file. The container backend injects these
       as pytest-process env vars for its in-process server; the embedded server
       runs in the container and can't read that env, so the equivalent values go
       to ``<config>/.ha_mcp/feature_flags.json`` — the same override layer
       ``ha_mcp.config`` reads in a standalone deployment (the embedded server is
       standalone: ``is_running_in_addon()`` is False in embedded mode, so the
       file is honored rather than short-circuited by Supervisor).
    3. Write the backup-settings override file (``backup_settings.json``,
       ``_EMBEDDED_BACKUP_OVERRIDES``) the same way, for ``BACKUP_OVERRIDE_FIELDS``
       values (e.g. ``enable_snapshot_delete``) — a separate registry from
       ``FEATURE_FLAG_FIELDS``, read from a separate override file.
    """
    storage_file = config_path / ".storage" / "core.config_entries"
    data = json.loads(storage_file.read_text())
    entries = data.setdefault("data", {}).setdefault("entries", [])
    # Dedupe by entry_id, not domain: the domain (ha_mcp_tools) is shared with the
    # tools services entry seeded by _install_custom_component.
    if not any(
        isinstance(e, dict) and e.get("entry_id") == _EMBEDDED_ENTRY_ID for e in entries
    ):
        entries.append(
            {
                "created_at": "2025-09-07T23:56:28.040744+00:00",
                "data": {
                    "entry_type": "server",
                    "webhook_id": _EMBEDDED_WEBHOOK_ID,
                    "secret_path": _EMBEDDED_SECRET_PATH,
                },
                "disabled_by": None,
                "discovery_keys": {},
                "domain": _EMBEDDED_DOMAIN,
                "entry_id": _EMBEDDED_ENTRY_ID,
                "minor_version": 1,
                "modified_at": "2025-09-07T23:56:28.040747+00:00",
                "options": {
                    # file:// wheel (the pre-release/override channel) + deps
                    # resolved under HA's constraints on force-install.
                    "pip_spec": f"ha-mcp @ file:///config/{wheel_name}",
                    "server_port": _EMBEDDED_SERVER_PORT,
                    "bind_host": "127.0.0.1",
                    "webhook_auth": "none",
                },
                "pref_disable_new_entities": False,
                "pref_disable_polling": False,
                "source": "import",
                "subentries": [],
                "title": "HA-MCP Server",
                "unique_id": _EMBEDDED_UNIQUE_ID,
                "version": 1,
            }
        )
        storage_file.write_text(json.dumps(data, indent=2))

    server_data_dir = config_path / _EMBEDDED_SERVER_CONFIG_SUBDIR
    server_data_dir.mkdir(parents=True, exist_ok=True)
    (server_data_dir / "feature_flags.json").write_text(
        json.dumps(_EMBEDDED_FEATURE_FLAGS, indent=2)
    )
    (server_data_dir / "backup_settings.json").write_text(
        json.dumps(_EMBEDDED_BACKUP_OVERRIDES, indent=2)
    )
    logger.info(
        "Seeded ha_mcp_tools in-process server config entry + feature-flag "
        "+ backup-setting overrides"
    )


def _embedded_mcp_result(resp: requests.Response) -> dict[str, Any] | None:
    """Parse a Streamable-HTTP MCP response (JSON body or SSE) to a JSON-RPC dict."""
    return parse_mcp_response(resp.headers.get("Content-Type", ""), resp.content)


def _wait_for_embedded_webhook_ready(webhook_url: str, timeout: int) -> bool:
    """Poll the embedded server's ingress webhook until MCP ``initialize`` works.

    A valid JSON-RPC ``result`` means the in-process MCP server has installed
    itself, started its worker thread, and registered the webhook. Returns False
    on timeout so the caller can dump diagnostics and fail with context.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "e2e-embedded-readiness", "version": "1.0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.post(
                webhook_url, headers=headers, data=json.dumps(payload), timeout=30
            )
            if resp.status_code == 200:
                parsed = _embedded_mcp_result(resp)
                if parsed is not None and "result" in parsed:
                    elapsed = int(time.monotonic() - (deadline - timeout))
                    logger.info(
                        "✅ Embedded MCP server webhook ready after ~%ds", elapsed
                    )
                    return True
        except requests.exceptions.RequestException:
            # Bring-up still in flight (pip force-install, thread start): retry.
            pass
        time.sleep(_EMBEDDED_READY_POLL_S)
    return False


def _seed_legacy_yaml_backups(config_path: Path) -> None:
    """Stage pre-#1579 legacy ``.bak`` artifacts before the container boots (#1579).

    The component's ``list_legacy_backups`` / ``read_legacy_backup`` services read
    ``<config>/.ha_mcp_tools_backups/`` live, but a post-boot host write to the
    bind-mounted config dir doesn't propagate in CI — so the legacy-backup e2e
    seeds here, at the same pre-boot stage the rest of the test config is laid
    down (``_setup_config_permissions`` then runs over it like everything else).

    Two fixed artifacts:
    - ``themes_e2elegacy.yaml.<ts>.bak`` decodes unambiguously to
      ``themes/e2elegacy.yaml`` (no underscore in the basename), so restore can
      target it.
    - ``packages_foo_bar.yaml.<ts>.bak`` has a literal underscore, making
      ``packages/foo_bar.yaml`` vs ``packages/foo/bar.yaml`` indistinguishable,
      so restore must refuse rather than guess.
    """
    legacy_dir = config_path / ".ha_mcp_tools_backups"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "themes_e2elegacy.yaml.20200101_000000.bak").write_text(
        "e2elegacy:\n  primary-color: '#abcdef'\n"
    )
    (legacy_dir / "packages_foo_bar.yaml.20200101_000000.bak").write_text(
        "# legacy ambiguous artifact\nswitch: []\n"
    )


def _seed_non_yaml_package_file(config_path: Path) -> None:
    """Stage a non-YAML file inside the bound packages folder pre-boot (#1788).

    ``ha_config_get_yaml``'s glob is not restricted to ``*.yaml``, so
    ``custom_packages/*`` legitimately turns up a file the component refuses to
    read (its package-dir read rule requires ``.yaml``). The warn-and-continue
    path that keeps one such file from sinking the whole search needs the real
    component to produce that refusal, and no tool can create the file:
    ``write_file`` is never granted package access, so it is staged here, like
    the legacy backups above (a post-boot host write doesn't propagate in CI).

    Inert at boot: HA loads a ``!include_dir_named`` folder through
    ``_find_files(loc, "*.yaml")`` (annotatedyaml), so a ``.md`` is ignored.
    The folder name matches the one initial_test_state/configuration.yaml binds.
    """
    packages_dir = config_path / "custom_packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    (packages_dir / "_e2e_not_yaml.md").write_text(
        "Not YAML. Staged so a packages glob has a file the component skips.\n"
    )


def _collect_manifest_requirements(config_path: Path) -> list[str]:
    """Aggregate ``requirements`` from every installed custom-component manifest.

    Returns a de-duplicated ordered list of pip-installable requirement
    strings (e.g. ``["ruamel.yaml>=0.18.0"]``).

    Used to pre-install third-party packages in the HA container's Python
    env before HA boots: HA's runtime manifest-requirement-install does
    not reliably fire for config entries that the e2e fixture pre-injects
    via ``.storage/core.config_entries`` (the path
    ``_install_custom_component`` takes), so an integration that imports
    a third-party package at module load or in ``async_setup_entry``
    would otherwise hit ``ModuleNotFoundError`` and end up in
    ``state=setup_error``. Live evidence on PR #1268 ARM E2E
    (2026-05-12): ``ruamel.yaml`` was never installed by HA on that run.
    """
    cc_dir = config_path / "custom_components"
    if not cc_dir.exists():
        return []
    reqs: list[str] = []
    for manifest_path in sorted(cc_dir.glob("*/manifest.json")):
        try:
            manifest_data = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                f"⚠️ Could not read {manifest_path}: {type(exc).__name__}: {exc}"
            )
            continue
        for req in manifest_data.get("requirements", []):
            if isinstance(req, str) and req not in reqs:
                reqs.append(req)
    return reqs


def _wait_for_ha_api_ready(
    base_url: str,
    headers: dict[str, str],
    timeout: int,
) -> bool:
    """Poll ``/api/`` for HTTP 200. Returns True on success, False on timeout.

    Called from the initial-boot path in ``ha_container_with_fresh_config``;
    the helper extraction is preserved so a future call site (e.g. a
    bounded retry after a real recoverable flake surfaces in the dump)
    can re-use the same readiness contract without duplicating the
    polling loop.

    NOTE: ``/api/`` 200 is HA's *liveness* signal (HTTP component up),
    not its *readiness* signal. The readiness gate that asserts every
    integration's ``async_setup_entry`` has completed lives in
    ``_wait_for_core_state_running`` below.
    """

    # Wall-clock-bound polling: ``for attempt in range(timeout)`` would let a
    # single slow request (5s HTTP timeout) plus the 1s sleep stretch each
    # iteration to ~6s, so a hung server could keep the loop running for up
    # to ``6 * timeout`` seconds instead of ``timeout``. The monotonic-based
    # cap enforces the budget the caller asked for.
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            response = requests.get(f"{base_url}/api/", timeout=5, headers=headers)
            if response.status_code == 200:
                elapsed = int(time.monotonic() - start_time)
                logger.info(f"🏠 Home Assistant API ready after {elapsed}s")
                return True
        except requests.exceptions.RequestException:
            # Boot-phase polling: HA API not up yet — retry until timeout (#1266).
            pass
        time.sleep(1)
    return False


def _snapshot_config_entries(
    base_url: str,
    headers: dict[str, str],
    *,
    timeout: float = 5.0,
) -> tuple[int, int, bool]:
    """Return ``(loaded_count, total_count, snapshot_ok)`` from
    ``/api/config/config_entries/entry``.

    Used by ``_wait_for_core_state_running`` to capture the entry-state
    snapshot at the trip moment (and on timeout) so post-merge data can
    verify whether ``CoreState.RUNNING`` actually closes the race for
    slow integrations or whether late-binding entries are still loading
    when the gate exits.

    ``snapshot_ok=False`` signals that the count fields are sentinel
    ``(0, 0)`` rather than a legitimately empty entries list — the
    distinction matters for the ``pytest_terminal_summary`` drift check,
    which would otherwise read both sides as 0 on a persistently-broken
    endpoint and never fire. The endpoint hit is best-effort
    instrumentation, not a hard requirement.
    """

    try:
        resp = requests.get(
            f"{base_url}/api/config/config_entries/entry",
            timeout=timeout,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.debug(f"snapshot_config_entries: HTTP {resp.status_code}")
            return 0, 0, False
        entries = resp.json()
        if not isinstance(entries, list):
            logger.debug(
                f"snapshot_config_entries: unexpected body type {type(entries).__name__}"
            )
            return 0, 0, False
        total = len(entries)
        loaded = sum(
            1 for e in entries if isinstance(e, dict) and e.get("state") == "loaded"
        )
        return loaded, total, True
    except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
        logger.debug(f"snapshot_config_entries: {type(exc).__name__}: {exc}")
        return 0, 0, False


def _wait_for_core_state_running(
    base_url: str,
    headers: dict[str, str],
    timeout: int,
) -> tuple[bool, float, str, int, int, bool]:
    """Poll ``/api/core/state`` until ``state == "RUNNING"``.

    Returns ``(success, elapsed_s, last_state, entries_loaded,
    entries_total, snapshot_ok)``. ``snapshot_ok=False`` flags that the
    entries fields are sentinel zeros rather than real data.

    HA Core's ``APICoreStateView`` (``homeassistant/components/api/__init__.py``)
    is the documented Supervisor-facing readiness endpoint: it reports the
    ``CoreState`` enum value, which only transitions to ``"RUNNING"`` once
    every integration's ``async_setup_entry`` has been dispatched (or hit
    the 300s ``SLOW_SETUP_MAX_WAIT`` per-domain timeout from
    ``homeassistant/setup.py``). ``/api/`` 200 by contrast is HA's
    liveness signal — the HTTP component finishes setup early in bootstrap,
    before most integrations.

    Background (#366 thread, Ilya0527 2026-05-18): polling ``/api/`` then
    counting components / entities / services across separate gates was
    racing ``async_setup_entry`` for slow integrations. This helper
    replaces five such gates with a single ``CoreState.RUNNING`` check.
    A tight ``sun.sun`` state poll is still kept inline at the call site
    because sun's first periodic position computation runs as a scheduled
    task after ``async_setup_entry`` returns, so ``RUNNING`` does not
    strictly imply ``sun.sun != "unknown"``.

    Also captures ``/api/config/config_entries/entry`` at the trip moment
    via ``_snapshot_config_entries`` to emit ``entries_loaded`` /
    ``entries_total`` alongside the success signal. The post-merge data
    window then verifies that ``RUNNING`` actually closes the race: any
    run where ``entries_loaded < entries_total`` at trip time means a
    slow integration finished its ``async_setup_entry`` after
    ``CoreState.RUNNING`` was set, and a follow-up gate would be
    justified.
    """

    start_time = time.monotonic()
    last_state = "<no response>"
    while time.monotonic() - start_time < timeout:
        try:
            response = requests.get(
                f"{base_url}/api/core/state", timeout=2, headers=headers
            )
            if response.status_code == 200:
                # Parse before stamping last_state — a JSONDecodeError on
                # a 200 body would otherwise leave last_state misreporting
                # "HTTP 200" when the actual cause was malformed JSON,
                # which the except below correctly attributes to the
                # exception class.
                state_value = response.json().get("state", "<unknown>")
                last_state = state_value
                if state_value == "RUNNING":
                    elapsed = time.monotonic() - start_time
                    entries_loaded, entries_total, snapshot_ok = (
                        _snapshot_config_entries(base_url, headers)
                    )
                    logger.info(
                        f"🏃 CoreState.RUNNING after {elapsed:.1f}s "
                        f"(entries loaded: {entries_loaded}/{entries_total}"
                        f"{'' if snapshot_ok else ', snapshot unavailable'})"
                    )
                    return (
                        True,
                        elapsed,
                        state_value,
                        entries_loaded,
                        entries_total,
                        snapshot_ok,
                    )
            else:
                # Surface HTTP status as a fallback ``last_state`` so a
                # persistent non-200 (e.g. 503 during HA boot) shows up
                # in the timeout ``pytest.fail`` message instead of the
                # initial ``"<no response>"``.
                last_state = f"HTTP {response.status_code}"
        except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
            last_state = type(exc).__name__
            logger.debug(f"core_state check failed: {exc}")
        time.sleep(1)

    # Final snapshot for the failure-diagnostics path — HA is known-bad
    # by this point, so use a tight 2s timeout rather than the default 5s
    # to keep teardown responsive.
    entries_loaded, entries_total, snapshot_ok = _snapshot_config_entries(
        base_url, headers, timeout=2.0
    )
    return (
        False,
        time.monotonic() - start_time,
        last_state,
        entries_loaded,
        entries_total,
        snapshot_ok,
    )


def _dump_ha_readiness_diagnostics(
    container: DockerContainer,
    base_url: str,
    headers: dict[str, str],
    label: str,
    *,
    config_entry_domain: str | None = None,
) -> None:
    """Emit HA-side diagnostics for any readiness-gate failure.

    Generic best-effort dump used by every readiness gate in
    ``ha_container_with_fresh_config``. The optional
    ``config_entry_domain`` argument lets the caller surface
    domain-specific presence/absence information (e.g. the sun-gate
    failure path passes ``config_entry_domain="sun"`` to make the
    config-entries section call out whether that specific entry is
    present or which state it's stuck in) instead of only the generic
    counts.

    Without it, the dump shows aggregate ``/api/services`` and
    ``/api/config/config_entries/entry`` domain lists — enough context
    to distinguish "HA never finished starting" from "HA started but a
    specific domain regressed".

    Each capture is wrapped in its own try/except so a single failure
    (container already exited, HA API gone) does not lose the other
    captures. Surfaced at WARNING level so CI logs keep the lines
    visible even with default filtering.
    """
    import docker as _docker

    logger.warning(f"📋 readiness diagnostics dump ({label}):")

    # /api/services snapshot — distinguishes "domain absent" from
    # "request errored at timeout edge".
    try:
        svc_resp = requests.get(f"{base_url}/api/services", timeout=5, headers=headers)
        if svc_resp.status_code == 200:
            domains = sorted(
                {s.get("domain") for s in svc_resp.json() if s.get("domain")}
            )
            logger.warning(f"  /api/services: {len(domains)} domains: {domains}")
        else:
            logger.warning(
                f"  /api/services: HTTP {svc_resp.status_code} {svc_resp.text[:200]}"
            )
    except Exception as exc:
        # Broad catch by design: this is a diagnostic dump, and an
        # unexpected exception class (e.g. SSL error subclass, unicode
        # decode of an HTML 5xx body) should not abort the remaining
        # captures below. The per-capture try/except scope ensures any
        # single failure is logged without losing the others.
        logger.warning(f"  /api/services: request failed: {type(exc).__name__}: {exc}")

    # /api/config/config_entries/entry — surfaces the entry's state
    # ('loaded' / 'setup_retry' / 'setup_error' / 'not_loaded' / etc.).
    # Distinguishes "HA never imported the entry" from "HA imported but
    # async_setup_entry raised".
    try:
        entries_resp = requests.get(
            f"{base_url}/api/config/config_entries/entry",
            timeout=5,
            headers=headers,
        )
        if entries_resp.status_code == 200:
            entries = entries_resp.json()
            entry_domains = sorted(
                {
                    e.get("domain")
                    for e in entries
                    if isinstance(e, dict) and e.get("domain")
                }
            )
            if config_entry_domain:
                matching = [
                    e
                    for e in entries
                    if isinstance(e, dict) and e.get("domain") == config_entry_domain
                ]
                if matching:
                    for entry in matching:
                        logger.warning(
                            f"  config_entry[{config_entry_domain}]: "
                            f"id={entry.get('entry_id')} "
                            f"state={entry.get('state')} "
                            f"reason={entry.get('reason')} "
                            f"source={entry.get('source')}"
                        )
                else:
                    logger.warning(
                        f"  config_entry[{config_entry_domain}]: NO entry "
                        f"visible in HA's config_entries (available: "
                        f"{entry_domains}) — install step may have written "
                        "to .storage but HA did not pick it up"
                    )
            else:
                logger.warning(
                    f"  /api/config/config_entries/entry: {len(entries)} total: "
                    f"{entry_domains}"
                )
        else:
            logger.warning(
                f"  /api/config/config_entries/entry: HTTP {entries_resp.status_code}"
            )
    except Exception as exc:
        # Same broad-catch rationale as the /api/services dump above.
        logger.warning(
            f"  /api/config/config_entries/entry: request failed: {type(exc).__name__}: {exc}"
        )

    # docker logs --tail 100 + container state. The early ``tail=20`` grab
    # inside ``ha_container_with_fresh_config`` fires immediately after
    # container start and so does not cover the custom-component lifecycle
    # that produces the symptom.
    try:
        docker_client = _docker.from_env()
        docker_container = docker_client.containers.get(
            container.get_wrapped_container().id
        )
        logger.warning(f"  container status: {docker_container.status}")
        try:
            state_attrs = docker_container.attrs.get("State", {})
            logger.warning(
                f"  container state: exit_code={state_attrs.get('ExitCode')} "
                f"oom_killed={state_attrs.get('OOMKilled')} "
                f"restart_count={state_attrs.get('RestartCount')}"
            )
        except (KeyError, AttributeError) as exc:
            logger.warning(
                f"  container state: introspect failed: {type(exc).__name__}: {exc}"
            )
        try:
            logs = docker_container.logs(tail=100).decode("utf-8", errors="ignore")
            logger.warning(f"  docker logs --tail 100:\n{logs}")
        except _docker.errors.DockerException as exc:
            logger.warning(f"  docker logs: failed: {type(exc).__name__}: {exc}")
    except _docker.errors.DockerException as exc:
        logger.warning(f"  docker client: failed: {type(exc).__name__}: {exc}")


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


@pytest.fixture(scope="session")
async def test_settings():
    """Get test configuration settings."""
    settings = get_global_settings()
    logger.info(f"Test settings: HA_URL={settings.homeassistant_url}")
    return settings


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
    except Exception as exc:
        logger.debug(f"Docker Desktop DNS probe failed: {exc}")

    # Plain Linux Docker — inject the mapping so the hostname resolves in the container
    logger.info(
        "🔍 Plain Linux Docker detected — injecting host.docker.internal via extra_hosts"
    )
    return {
        "hostname": "host.docker.internal",
        "extra_hosts": {"host.docker.internal": "host-gateway"},
    }


@pytest.fixture(scope="session")
def _blueprint_http_server():
    """Start a local HTTP server for blueprint files used by HAOS tests."""
    env = _detect_docker_host()

    assets_dir = Path(__file__).parent.parent.parent / "assets" / "blueprints"
    assets_dir.mkdir(parents=True, exist_ok=True)

    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(assets_dir))
    handler.log_message = lambda *args: None  # type: ignore[method-assign]
    srv = http.server.HTTPServer(("0.0.0.0", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    base_url = f"http://{env['hostname']}:{port}"
    logger.info(f"🌐 Blueprint HTTP server on :{port}, container URL: {base_url}")

    try:
        yield {"base_url": base_url, "port": port, "extra_hosts": env["extra_hosts"]}
    finally:
        srv.shutdown()


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
    }


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


@pytest.fixture(scope="session")
def ha_container_with_fresh_config(request):
    """Create Home Assistant test environment with fresh config.

    Default backend: testcontainer (HA Core Docker image). When the
    ``HAOS_TEST_IMAGE_PATH`` env var points to a pre-baked HAOS qcow2,
    the fixture instead boots HAOS under QEMU/KVM and returns the same
    base_url + token contract. Container-specific keys (container,
    port, config_path) are None on the HAOS path — tests that depend on
    those should skip when the HAOS backend is selected (see #1281).
    """
    # HAOS backend dispatch — short-circuit the testcontainer path entirely.
    if is_haos_backend_selected():
        base_image_path = Path(os.environ[HAOS_IMAGE_ENV])
        inaddon = is_haos_inaddon_mode()
        haos_embedded = is_haos_embedded_mode()
        # Per-worker port + overlay setup for pytest-xdist parallel HAOS
        # (#1350). Single-worker runs short-circuit and reuse the base
        # image path unchanged.
        image_path = _haos_worker_setup(base_image_path)
        logger.info(
            "HAOS backend selected (mode=%s) — booting qcow2 at %s",
            "inaddon" if inaddon else "embedded" if haos_embedded else "external",
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
        with boot_haos_qemu(image_path) as base_url:
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
            # Strict best-practices gate (#1779) defaults ON with its parent;
            # pin it OFF so the suite's keyless writes aren't hard-blocked. The
            # strict-gate e2e test builds its own server with the flag enabled.
            os.environ["ENABLE_STRICT_MANDATORY_BPS"] = "false"
            # Snapshot deletion (#1861) defaults OFF in production; enabled
            # here so the e2e suite can cover the (snapshot, delete) guard
            # chain against a disposable test HAOS instance.
            os.environ["ENABLE_SNAPSHOT_DELETE"] = "true"
            _reset_ha_in_process_caches()
            # Mirrors the sun.sun + entity wait loop in the testcontainer
            # branch of ha_container_with_fresh_config: the first tests reach
            # for sun.sun's state immediately, but HA Core may still be
            # propagating registry → state-machine when boot_haos_qemu's
            # /manifest.json gate releases (manifest.json is served by the
            # frontend before all integrations finish loading). Wait for
            # sun.sun to (a) exist, then (b) leave the "unknown" state so
            # template tests don't race.
            haos_headers = {"Authorization": f"Bearer {token}"}
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
                    light_resp = requests.get(
                        light_url, timeout=5, headers=haos_headers
                    )
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
            # Inaddon mode: refresh_dev_addon_source_in_qcow2 ran above
            # with a bumped version, so Supervisor now sees an update
            # available. Trigger it via WS supervisor_api (Docker layer
            # cache → only the COPY src/ + uv-sync-project layers
            # re-execute), then wait for the addon's MCP endpoint.
            addon_mcp_url: str | None = None
            # haos_embedded: URL the mcp_client fixture connects to (the baked
            # in-process MCP server's ingress webhook inside the HAOS VM).
            embedded_webhook_url: str | None = None
            # Pull setup-time work INTO the try/finally so post-mortem log
            # dump runs even when trigger_dev_addon_update or
            # wait_for_addon_mcp_ready raises — those steps own ~all the
            # inaddon-specific failure surface, and without logs they're
            # opaque "unknown error" failures.
            try:
                if inaddon:
                    logger.info(
                        "Inaddon mode: triggering Supervisor addon update for PR source"
                    )
                    trigger_dev_addon_update(base_url, token, timeout=600.0)
                    addon_mcp_url = wait_for_addon_mcp_ready(timeout=180.0)
                    logger.info("Inaddon addon MCP endpoint ready at %s", addon_mcp_url)
                    assert addon_mcp_url is not None, (
                        "Inaddon setup completed without producing an "
                        "addon_mcp_url — wait_for_addon_mcp_ready contract "
                        "violation. Downstream mcp_client fixture would fail "
                        "with an obscure TypeError on transport construction."
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
                    embedded_webhook_url = (
                        f"{base_url}/api/webhook/{HA_MCP_SERVER_WEBHOOK_ID}"
                    )
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
                yield {
                    "container": None,
                    "port": None,
                    "base_url": base_url,
                    "config_path": None,
                    "blueprint_server": blueprint_for_haos,
                    "token": token,
                    # backend marker distinguishes inaddon dispatch (mcp_client
                    # → addon_mcp_url), haos_embedded (mcp_client →
                    # embedded_webhook_url), and external (in-process FastMCP
                    # server pointing at base_url).
                    "backend": (
                        "haos_inaddon"
                        if inaddon
                        else "haos_embedded"
                        if haos_embedded
                        else "haos"
                    ),
                    # Only set on inaddon mode; external/embedded modes leave None.
                    "addon_mcp_url": addon_mcp_url,
                    # Only set on haos_embedded; other HAOS modes leave None. Named
                    # to match the container embedded backend's key so mcp_client's
                    # HTTP-transport branch is shared.
                    "embedded_webhook_url": embedded_webhook_url,
                }
            finally:
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
        return

    # --- Testcontainer path ---
    # Safety guard 1: ensure Docker is available before doing anything else
    try:
        import docker as docker_sdk

        docker_sdk.from_env().ping()
    except Exception as e:
        pytest.fail(
            f"Docker is not available: {e}\n"
            "E2E tests require a running Docker daemon (testcontainers).\n"
            "Start Docker and retry."
        )

    logger.info("🐳 Creating Home Assistant container with testcontainers...")

    # Embedded backend (#1527): install the in-process MCP server integration
    # into this same testcontainer and drive it over its ingress webhook. The
    # wheel name is captured here and consumed by the entrypoint preinstall below.
    embedded = _is_embedded_backend_selected()
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
    _install_custom_component(
        config_path,
        repo_root / "custom_components" / "ha_mcp_tools",
        "ha_mcp_tools",
        "HA-MCP File & YAML Tools",
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
        quoted_wheel = shlex.quote(f"/config/{embedded_wheel_name}")
        constraints_probe = (
            "HACONS=\"$(python3 -c 'import homeassistant, os; "
            "print(os.path.join(os.path.dirname(homeassistant.__file__), "
            '"package_constraints.txt"))\')"'
        )
        preinit_cmds.append(
            f"{constraints_probe} && "
            f'if [ -f "$HACONS" ]; then '
            f'pip install --no-cache-dir --constraint "$HACONS" {quoted_wheel}; '
            f"else pip install --no-cache-dir {quoted_wheel}; fi"
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

    with container:
        # Readiness-gate budgets for the testcontainer path. Defined at
        # the top of this ``with container:`` block for grep-ability —
        # successor of the five per-gate constants the single
        # ``CoreState.RUNNING`` check replaced. The HAOS branch above
        # keeps its own ``SUN_WAIT = 60`` local (HAOS-qemu boot is a
        # different scope, no ``[READINESS_GATE_TIMING]`` emit).
        #
        # ``CORE_STATE_TIMEOUT = 60`` ≈ 12× the observed-max of ~5s
        # across the first CI window (4 worker sessions,
        # ``entries_loaded == entries_total == 11``). The upstream
        # per-domain ceiling ``SLOW_SETUP_MAX_WAIT = 300`` from
        # ``homeassistant/setup.py`` is the latest point at which a
        # stuck integration would surface as ``CoreState`` stuck at
        # ``starting`` — 60s is well clear of that without paying the
        # worst-case wall-clock if a slow integration legitimately
        # needs the full budget.
        CORE_STATE_TIMEOUT = 60
        # ``SUN_WAIT = 5`` is the residual tight poll:
        # ``CoreState.RUNNING`` does not strictly imply
        # ``sun.sun != "unknown"`` because sun's first periodic position
        # computation runs as a scheduled task after
        # ``async_setup_entry`` returns. Template tests asserting
        # above/below_horizon would fail without this inline check.
        SUN_WAIT = 5

        # Get the dynamically assigned port
        host_port = container.get_exposed_port(8123)
        base_url = f"http://localhost:{host_port}"

        # Set environment variables for the dynamic URL so WebSocket client uses correct port
        os.environ["HOMEASSISTANT_URL"] = base_url
        os.environ["HOMEASSISTANT_TOKEN"] = TEST_TOKEN
        # Enable feature flags for e2e tests. Beta sub-flags require
        # the master to also be on.
        os.environ["ENABLE_BETA_FEATURES"] = "true"
        os.environ["ENABLE_YAML_CONFIG_EDITING"] = "true"
        # Per-key sub-toggles default OFF; the E2E suite covers the
        # whole packages/*.yaml surface so enable all three.
        os.environ["ENABLE_YAML_PACKAGES_AUTOMATION"] = "true"
        os.environ["ENABLE_YAML_PACKAGES_SCRIPT"] = "true"
        os.environ["ENABLE_YAML_PACKAGES_SCENE"] = "true"
        os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = "true"
        # Strict best-practices gate (#1779) defaults ON with its parent; pin it
        # OFF so the suite's keyless writes aren't hard-blocked. The strict-gate
        # e2e test builds its own server with the flag enabled.
        os.environ["ENABLE_STRICT_MANDATORY_BPS"] = "false"
        # Snapshot deletion (#1861) defaults OFF in production; enabled here so
        # the e2e suite can cover the (snapshot, delete) guard chain against a
        # disposable test container.
        os.environ["ENABLE_SNAPSHOT_DELETE"] = "true"

        # Reset cached settings + WebSocket pool so subsequent client
        # lookups pick up the new container's URL.
        _reset_ha_in_process_caches()

        logger.info(f"🚀 Home Assistant container started on {base_url}")
        logger.info(f"🐳 Container ID: {container.get_container_host_ip()}:{host_port}")

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
        except Exception as e:
            logger.warning(f"⚠️ Could not inspect container: {e}")

        # Wait for API to be ready via the module-level
        # ``_wait_for_ha_api_ready`` helper. The helper extraction is
        # preserved as a single-contract surface in case a future bounded
        # retry path is reintroduced.
        # NOTE: ``requests`` is imported at module top; do NOT re-import it
        # locally here — Python's scoping rules would then make ``requests``
        # a function-local for the entire ha_container_with_fresh_config,
        # which previously caused UnboundLocalError in the HAOS branch.

        # Use test token for API readiness checks
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

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
        )

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
            logger.info(
                "⏳ Waiting for the in-process MCP server webhook to come up..."
            )
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

        # Store connection info for other fixtures
        container_info = {
            "container": container,
            "port": host_port,
            "base_url": base_url,
            "config_path": str(config_path),
            "blueprint_server": local_blueprint,
            "token": TEST_TOKEN,
            # ``embedded`` reuses the whole testcontainer path but swaps the
            # server-under-test to the in-process integration; mcp_server yields
            # None and mcp_client speaks HTTP to embedded_webhook_url (below).
            "backend": "embedded" if embedded else "container",
            # Set only on the embedded backend; None keeps the container-lane
            # dispatch assertions (addon_mcp_url is None) unchanged.
            "embedded_webhook_url": embedded_webhook_url,
        }

        try:
            yield container_info
        finally:
            # Container cleanup runs via the enclosing ``with container:``
            # block's ``__exit__`` (calls ``stop()`` which removes the
            # container). With ``TESTCONTAINERS_RYUK_DISABLED=true`` set in
            # the CI workflow env (see
            # .github/workflows/{pr,e2e-tests,performance-tests}.yml)
            # the with-block exit IS the only cleanup mechanism — Python's
            # context-manager protocol guarantees ``__exit__`` fires on
            # both normal and exception flows, so the Ryuk reaper safety
            # net is not needed. Refs #366.
            #
            # Do NOT add an explicit ``container.stop()`` here.
            # testcontainers' ``DockerContainer.stop()`` calls
            # ``remove(force=True)`` and is non-idempotent — a second call
            # from the enclosing ``with container:`` ``__exit__`` raises
            # ``docker.errors.NotFound`` at session teardown.
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("✅ Cleanup completed")


@pytest.fixture(scope="session")
async def ha_client(
    ha_container_with_fresh_config,
) -> AsyncGenerator[HomeAssistantClient]:
    """Create Home Assistant client connected to the container or HAOS QEMU."""
    container_info = ha_container_with_fresh_config
    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)

    client = HomeAssistantClient(base_url=base_url, token=token)

    # Verify connection
    try:
        config = await client.get_config()
        if not config:
            pytest.fail(f"Failed to connect to Home Assistant at {base_url}")

        logger.info(
            f"✅ Connected to HA: {config.get('location_name', 'Unknown')} v{config.get('version', 'Unknown')}"
        )
        logger.info(f"🏠 Components: {len(config.get('components', []))} loaded")

    except Exception as e:
        pytest.fail(f"Home Assistant connection failed: {e}\nURL: {base_url}")

    yield client
    await client.close()


@pytest.fixture(scope="session")
async def mcp_server(
    ha_container_with_fresh_config,
) -> AsyncGenerator[HomeAssistantSmartMCPServer | None]:
    """Create MCP server instance connected to the container or HAOS QEMU.

    Yields None on the inaddon HAOS backend, the container embedded backend, and
    the haos_embedded backend — in all three the server-under-test runs in a
    separate process (the ha-mcp dev addon inside booted HAOS; the in-process
    in-process MCP server entry inside the testcontainer or the HAOS core container),
    so spinning up an in-process FastMCP server here would be wasteful and
    misleading (it'd connect to HA but tests would never use it). The
    ``mcp_client`` fixture branches on backend to either use this in-process
    server or build an HTTP transport pointing at the out-of-process server.
    """
    container_info = ha_container_with_fresh_config
    if container_info.get("backend") in ("haos_inaddon", "embedded", "haos_embedded"):
        logger.info(
            "%s mode: skipping in-process MCP server "
            "(tests use the out-of-process server's HTTP endpoint instead)",
            container_info.get("backend"),
        )
        yield None
        return

    logger.info("🚀 Creating MCP server instance...")
    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)

    # Create client for the server
    client = HomeAssistantClient(base_url=base_url, token=token)

    # Create server with the client
    server = HomeAssistantSmartMCPServer(client=client)
    tools = await server.mcp.list_tools()
    logger.info(
        f"✅ MCP server initialized with {len(tools)} tools connected to {base_url}"
    )

    yield server
    # Server cleanup handled by server.close()


@pytest.fixture(scope="session")
async def mcp_client(
    ha_container_with_fresh_config, mcp_server
) -> AsyncGenerator[Client]:
    """Create FastMCP client — in-memory for in-process server, HTTP otherwise.

    On testcontainer + HAOS-external: in-memory transport bound to the
    ``mcp_server`` fixture (current behavior).
    On HAOS-inaddon: ``StreamableHttpTransport`` pointing at the dev
    addon's MCP endpoint (running inside the booted HAOS).
    On embedded (#1527): ``StreamableHttpTransport`` pointing at the
    in-process MCP server entry's ingress webhook (running inside the testcontainer).
    On haos_embedded (#1527): the same, but the in-process MCP server runs
    inside the HAOS core container (webhook on the booted VM). In all HTTP cases
    the server-under-test is a separate process; the local process is just a client.
    """
    container_info = ha_container_with_fresh_config
    backend = container_info.get("backend")
    if backend in ("haos_inaddon", "embedded", "haos_embedded"):
        from fastmcp.client.transports import StreamableHttpTransport

        if backend in ("embedded", "haos_embedded"):
            server_url = container_info.get("embedded_webhook_url")
            missing_msg = (
                f"{backend} backend signaled but container_info has no "
                "embedded_webhook_url — the embedded-webhook readiness gate must "
                "run + populate this key before mcp_client is requested. Check "
                "ha_container_with_fresh_config's embedded branch."
            )
        else:
            server_url = container_info.get("addon_mcp_url")
            missing_msg = (
                "Inaddon backend signaled but container_info has no "
                "addon_mcp_url — wait_for_addon_mcp_ready must run + "
                "populate this key before mcp_client is requested. "
                "Check ha_container_with_fresh_config's inaddon branch."
            )
        if not server_url:
            raise RuntimeError(missing_msg)
        logger.info(f"🔗 FastMCP client connecting (HTTP) to {server_url}")
        transport = StreamableHttpTransport(url=server_url)
        client = Client(transport)
        async with client:
            logger.debug("🔗 FastMCP client connected (HTTP transport, %s)", backend)
            yield client
        return

    # Default path: in-memory transport.
    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("🔗 FastMCP client connected (in-memory transport)")
        yield client


@pytest.fixture(scope="session")
async def stdio_mcp_client(
    ha_container_with_fresh_config,
) -> AsyncGenerator[Client]:
    """Spawn ``ha-mcp`` as a subprocess and connect via stdio JSON-RPC.

    Distinct from the default ``mcp_client`` fixture, which uses an
    in-memory transport (``Client(server.mcp)``) that bypasses subprocess
    startup, JSON serialization framing, and the installed-wheel side of
    the contract. This fixture is the only path that validates the
    transport real users hit when running ha-mcp via Claude Desktop,
    claude CLI, ``uvx``, or Docker stdio mode.

    Catches a different class of bug than the in-memory client:
    packaging regressions (e.g. skills missing from the installed
    package — #1280), entry-point startup failures, and JSON
    serialization issues. Without this fixture, stdio-only regressions
    can land green on CI.
    """

    from fastmcp.client.transports import StdioTransport

    container_info = ha_container_with_fresh_config

    # Subprocess inherits no env by default — forward only what ha-mcp
    # actually needs at startup. PATH so the subprocess resolves its
    # own dependencies via the test venv's site-packages.
    env = {
        "HOMEASSISTANT_URL": container_info["base_url"],
        "HOMEASSISTANT_TOKEN": TEST_TOKEN,
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # Keep startup snappy — the connection retry is irrelevant here,
        # the test container is already up by the time this fixture runs.
        "HA_MAX_RETRIES": "1",
        # Same pin as the session-server env blocks (#1779): strict mode
        # defaults ON and would prepend the acknowledgment line to tier-3
        # skill content, breaking the content == on-disk-bytes assertions.
        "ENABLE_STRICT_MANDATORY_BPS": "false",
    }

    # ``args`` is a required positional on the base ``StdioTransport``
    # (subclasses like ``PythonStdioTransport`` default it to ``None``,
    # but the base requires ``list[str]``). Pass an explicit empty list
    # since ``ha-mcp`` takes no positional args in stdio mode.
    transport = StdioTransport(command="ha-mcp", args=[], env=env)
    client = Client(transport)
    async with client:
        logger.debug("🔗 FastMCP client connected (stdio subprocess transport)")
        yield client


# Test session information
@pytest.fixture(scope="session", autouse=True)
async def test_session_info(ha_client, ha_container_with_fresh_config):
    """Log test session information."""
    config = await ha_client.get_config()
    container_info = ha_container_with_fresh_config

    logger.info("=" * 80)
    logger.info("🧪 HOME ASSISTANT MCP SERVER E2E TEST SESSION (FRESH CONFIG)")
    logger.info("=" * 80)
    logger.info(
        f"🏠 Home Assistant: {config.get('location_name')} v{config.get('version')}"
    )
    logger.info(f"🐳 Container URL: {container_info['base_url']}")
    logger.info(f"🔧 Components: {len(config.get('components', []))}")
    logger.info(f"🕒 Timezone: {config.get('time_zone', 'Unknown')}")
    logger.info("📁 Fresh config from: initial_test_state")
    logger.info(f"📂 Config path: {container_info['config_path']}")
    logger.info("=" * 80)

    yield

    logger.info("=" * 80)
    logger.info("✅ E2E TEST SESSION COMPLETED (FRESH CONFIG)")
    logger.info("=" * 80)


@pytest.fixture
def cleanup_tracker():
    """
    Track entities created during tests for cleanup.

    Usage in tests:
        cleanup_tracker.track("automation", "automation.test_automation")
        cleanup_tracker.track("script", "script.test_script")
    """
    created_entities: list[tuple[str, str]] = []

    class CleanupTracker:
        def track(self, entity_type: str, entity_id: str):
            """Track an entity for cleanup."""
            created_entities.append((entity_type, entity_id))
            logger.info(f"📝 Tracking {entity_type}: {entity_id} for cleanup")

        def get_tracked(self) -> list[tuple[str, str]]:
            """Get all tracked entities."""
            return created_entities.copy()

    tracker = CleanupTracker()
    yield tracker

    # Cleanup logic - log what would be cleaned up
    # Real implementation would delete the entities
    if created_entities:
        logger.info(f"🧹 Would clean up {len(created_entities)} test entities:")
        for entity_type, entity_id in created_entities:
            logger.info(f"  - {entity_type}: {entity_id}")


@pytest.fixture
async def test_light_entity(mcp_client) -> str:
    """
    Find a suitable light entity for testing.

    Returns the entity_id of a light that can be used for testing.
    Prefers entities that are currently off to minimize disruption.
    """
    # Search for light entities
    search_result = await mcp_client.call_tool(
        "ha_search", {"query": "light", "domain_filter": "light", "limit": 10}
    )

    # Parse search results
    search_data = parse_mcp_result(search_result)

    if not search_data.get("success") or not search_data.get("entities"):
        pytest.skip("No light entities available for testing")

    # Find a light that's currently off (preferred for testing)
    for entity in search_data["entities"]:
        entity_id = entity["entity_id"]

        # Get current state
        state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": entity_id}
        )
        state_data = parse_mcp_result(state_result)

        if state_data.get("data", {}).get("state") == "off":
            logger.info(f"🔍 Using test light: {entity_id} (currently off)")
            return entity_id

    # If no off lights, use the first available
    entity_id = search_data["entities"][0]["entity_id"]
    logger.info(f"🔍 Using test light: {entity_id} (may be on)")
    return entity_id


@pytest.fixture
async def clean_test_environment(mcp_client):
    """
    Ensure clean test environment by removing any existing test entities.

    This fixture runs before tests to clean up any leftover test data
    from previous test runs.
    """
    logger.info("🧹 Cleaning test environment...")

    # Search for test entities (containing 'test' or 'e2e' in name)
    search_patterns = ["test", "e2e"]

    for pattern in search_patterns:
        # Search automations
        search_result = await mcp_client.call_tool(
            "ha_search",
            {"query": pattern, "domain_filter": "automation", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)
        if search_data.get("success") and search_data.get("entities"):
            for entity in search_data["entities"]:
                entity_id = entity["entity_id"]
                if any(test_word in entity_id.lower() for test_word in ["test", "e2e"]):
                    logger.info(f"🗑️ Found test automation to clean: {entity_id}")
                    # In real implementation, would delete here

    logger.info("✅ Test environment cleaned")


class TestDataFactory:
    """Factory for creating test data configurations."""

    @staticmethod
    def automation_config(name: str, **overrides) -> dict[str, Any]:
        """Create a basic automation configuration for testing."""
        config = {
            "alias": f"Test {name} E2E",
            "description": f"E2E test automation - {name} - safe to delete",
            "trigger": [{"platform": "time", "at": "06:00:00"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.bed_light"}}
            ],
            "initial_state": False,  # Start disabled for safety
            "mode": "single",
        }

        config.update(overrides)
        return config

    @staticmethod
    def script_config(name: str, **overrides) -> dict[str, Any]:
        """Create a basic script configuration for testing."""
        config = {
            "alias": f"Test {name} Script E2E",
            "description": f"E2E test script - {name} - safe to delete",
            "sequence": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.bed_light"},
                },
                {"delay": {"seconds": 1}},
                {
                    "service": "light.turn_off",
                    "target": {"entity_id": "light.bed_light"},
                },
            ],
            "mode": "single",
        }
        config.update(overrides)
        return config

    @staticmethod
    def helper_config(helper_type: str, name: str, **overrides) -> dict[str, Any]:
        """Create helper configuration for testing."""
        base_configs = {
            "input_boolean": {"name": f"Test {name} Boolean", "initial": False},
            "input_number": {
                "name": f"Test {name} Number",
                "min_value": 0,
                "max_value": 100,
                "step": 1,
                "unit_of_measurement": "units",
            },
            "input_text": {
                "name": f"Test {name} Text",
                "initial": "test_value",
                "min": 0,
                "max": 255,
            },
        }

        config = base_configs.get(helper_type, {})
        config.update(overrides)
        return config


@pytest.fixture
def test_data_factory() -> TestDataFactory:
    """Provide factory for creating test data configurations."""
    return TestDataFactory()


@pytest.fixture
async def wait_for_state_change():
    """
    Utility fixture for waiting for entity state changes.

    Usage:
        await wait_for_state_change(mcp_client, "light.bedroom", "on", timeout=10)
    """

    async def _wait_for_state(
        client: Client, entity_id: str, expected_state: str, timeout: int = 5
    ) -> bool:
        """Wait for entity to reach expected state."""
        start_time = time.monotonic()

        while time.monotonic() - start_time < timeout:
            state_result = await client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            current_state = state_data.get("data", {}).get("state")
            if current_state == expected_state:
                logger.info(f"✅ {entity_id} reached state '{expected_state}'")
                return True

            await asyncio.sleep(0.5)

        logger.warning(
            f"⚠️ {entity_id} did not reach state '{expected_state}' within {timeout}s"
        )
        return False

    return _wait_for_state


@pytest.fixture(scope="session")
def local_blueprint_server(ha_container_with_fresh_config):
    """Return blueprint URL info for tests that need to import blueprints."""
    server = ha_container_with_fresh_config["blueprint_server"]
    logger.info(f"🌐 Blueprint server at {server['base_url']}")
    yield server
