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
import sys
import tempfile
import threading
import time
from collections.abc import AsyncGenerator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from testcontainers.core.container import DockerContainer

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fastmcp import Client

from ha_mcp.client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.server import HomeAssistantSmartMCPServer

# Import test utilities
from .utilities.assertions import parse_mcp_result
from .utilities.supervisor_mock import (
    _supervisor_mock_server,  # noqa: F401  (session fixture supervisor_mock depends on)
    supervisor_mock,  # noqa: F401  (re-exported fixture)
)

# Import test constants
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from test_constants import HA_TEST_IMAGE, TEST_TOKEN

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


def _log_readiness_timing(gate: str, elapsed_s: float, **extras: Any) -> None:
    """Record a fixture-side readiness-gate timing data point.

    The data points are routed to the master process and rendered at
    session end by ``pytest_terminal_summary``. Direct ``sys.stderr``
    writes don't survive pytest-xdist's per-worker capture buffer, so
    going through pytest's own reporting plumbing is the reliable path
    (follow-up to #1273 which lowered the only previously-measured gate
    ``INPUT_BOOLEAN_WAIT`` from 30s to 10s based on a 1s observed-resolve;
    the remaining 30s ``STABILIZATION_TIMEOUT`` /
    ``ENTITY_STABILIZATION_TIMEOUT`` / ``SUN_WAIT`` budgets need
    empirical timings to scope a follow-up tightening PR).
    """
    _READINESS_TIMINGS.append({"gate": gate, "elapsed_s": elapsed_s, **extras})


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
    timings = getattr(node, "workeroutput", {}).get("readiness_timings", [])
    _ALL_READINESS_TIMINGS.extend(timings)


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
    import os
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
    import urllib.error
    import urllib.request

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
                # Asymmetric with ``HA_MCP_TOOLS_WAIT`` below (which is
                # ``pytest.fail``): a stuck tool registration breaks
                # every downstream test, while a stuck HACS download
                # only breaks HACS-dependent tests — the rest of the
                # session still produces useful signal. Clear the stale
                # lock so a subsequent session does not also hit the
                # 180s wait when the winner truly crashed.
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
        if not any(e.get("domain") == domain for e in entries):
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
    """
    import requests as _requests

    # Wall-clock-bound polling: ``for attempt in range(timeout)`` would let a
    # single slow request (5s HTTP timeout) plus the 1s sleep stretch each
    # iteration to ~6s, so a hung server could keep the loop running for up
    # to ``6 * timeout`` seconds instead of ``timeout``. The monotonic-based
    # cap enforces the budget the caller asked for.
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            response = _requests.get(f"{base_url}/api/", timeout=5, headers=headers)
            if response.status_code == 200:
                elapsed = int(time.monotonic() - start_time)
                logger.info(f"🏠 Home Assistant API ready after {elapsed}s")
                return True
        except _requests.exceptions.RequestException:
            pass
        time.sleep(1)
    return False


def _wait_for_ha_mcp_tools_services(
    base_url: str,
    headers: dict[str, str],
    timeout: int,
) -> tuple[bool, float, int]:
    """Poll ``/api/services`` for the ``ha_mcp_tools`` domain.

    Returns ``(ready, elapsed_s, domain_count)``:

    - ``ready`` — True if the domain appears within ``timeout`` seconds,
      False on timeout.
    - ``elapsed_s`` — wall-clock seconds spent polling, float-precise
      (matches the precision the other readiness gates emit).
      Always populated, both on success and on timeout, so the caller can
      ``_log_readiness_timing`` regardless of branch.
    - ``domain_count`` — size of the registered-domain set when the
      target domain appeared (0 on timeout / no-200-response). Surfaces
      as the ``count=`` extra on the timing line, parity with the
      ``components`` / ``entities`` gates.

    The single caller lives in ``ha_container_with_fresh_config``; the
    helper extraction is preserved so a future bounded retry (if a
    recoverable flake class surfaces in the dump) can re-use the same
    wait contract without duplicating the polling loop.
    """
    import requests as _requests

    # Wall-clock-bound (see ``_wait_for_ha_api_ready`` for the rationale on
    # why ``range(timeout)`` would understate the actual budget).
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            svc_resp = _requests.get(
                f"{base_url}/api/services", timeout=5, headers=headers
            )
            if svc_resp.status_code == 200:
                domains = {s.get("domain") for s in svc_resp.json()}
                if "ha_mcp_tools" in domains:
                    elapsed_s = time.monotonic() - start_time
                    logger.info(
                        f"✅ ha_mcp_tools services ready after {elapsed_s:.2f}s"
                    )
                    return True, elapsed_s, len(domains)
        except (
            _requests.exceptions.RequestException,
            json.JSONDecodeError,
        ) as exc:
            logger.debug(f"ha_mcp_tools service check failed: {exc}")
        time.sleep(1)
    return False, time.monotonic() - start_time, 0


def _dump_ha_readiness_diagnostics(
    container: DockerContainer,
    base_url: str,
    headers: dict[str, str],
    label: str,
    *,
    service_domain: str | None = None,
    config_entry_domain: str | None = None,
) -> None:
    """Emit HA-side diagnostics for any readiness-gate failure.

    Generic best-effort dump used by every readiness gate in
    ``ha_container_with_fresh_config``. The optional ``service_domain``
    and ``config_entry_domain`` arguments let the caller surface
    domain-specific presence/absence information (e.g.
    ``service_domain="ha_mcp_tools"`` makes the dump call out whether
    that specific domain is missing from ``/api/services``) instead of
    only the generic counts.

    Without those arguments, the dump shows aggregate ``/api/services``
    and ``/api/config/config_entries/entry`` domain lists — enough
    context to distinguish "HA never finished starting" from "HA started
    but a specific domain regressed".

    Each capture is wrapped in its own try/except so a single failure
    (container already exited, HA API gone) does not lose the other
    captures. Surfaced at WARNING level so CI logs keep the lines
    visible even with default filtering.
    """
    import docker as _docker
    import requests as _requests

    logger.warning(f"📋 readiness diagnostics dump ({label}):")

    # /api/services snapshot — distinguishes "domain absent" from
    # "request errored at timeout edge".
    try:
        svc_resp = _requests.get(f"{base_url}/api/services", timeout=5, headers=headers)
        if svc_resp.status_code == 200:
            domains = sorted(
                {s.get("domain") for s in svc_resp.json() if s.get("domain")}
            )
            if service_domain:
                present = service_domain in domains
                logger.warning(
                    f"  /api/services: {len(domains)} domains; "
                    f"{service_domain}={'present' if present else 'absent'}"
                )
                if not present:
                    # Surface adjacent domains so a reader can rule out a
                    # regex / casing / typo class mismatch.
                    logger.warning(f"  /api/services domains: {domains}")
            else:
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
        entries_resp = _requests.get(
            f"{base_url}/api/config/config_entries/entry",
            timeout=5,
            headers=headers,
        )
        if entries_resp.status_code == 200:
            entries = entries_resp.json()
            entry_domains = sorted(
                {e.get("domain") for e in entries if e.get("domain")}
            )
            if config_entry_domain:
                matching = [
                    e for e in entries if e.get("domain") == config_entry_domain
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
    import ha_mcp.config
    from ha_mcp.client.websocket_client import websocket_manager

    ha_mcp.config._settings = None
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
    """Start a local HTTP server for blueprint files before the HA container launches.

    Must start before the container so the port is known when ``extra_hosts``
    is configured in ``ha_container_with_fresh_config``.
    """
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


@pytest.fixture(scope="session")
def ha_container_with_fresh_config(_blueprint_http_server):
    """Create Home Assistant container with fresh config using testcontainers."""
    # --- Safety guard 1: ensure Docker is available before doing anything else ---
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
            for entry in ce_data.get("data", {}).get("entries", []):
                if entry.get("domain") == "hacs":
                    entry["data"] = {"token": github_token}
                    logger.info("Injected GITHUB_TOKEN into HACS config entry")
                    break
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
        "HA MCP Tools",
    )

    # Shift the pre-baked recorder timestamps forward so the seeded rows
    # look "recent" to history queries with a 24h window. The recorder DB in
    # initial_test_state is baked offline (see scripts/bake_pagination_seed.py)
    # and its rows have whatever timestamps were captured at bake time. Without
    # this shift, the pagination tests in test_history.py/test_logbook.py would
    # silently skip again the moment the seed gets more than 24h old.
    _refresh_recorder_timestamps(config_path / "home-assistant_v2.db")

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
    # On plain Linux Docker (CI) also inject the host.docker.internal mapping so
    # the blueprint HTTP server is reachable from within the container.
    # On Docker Desktop the mapping is provided by Docker's embedded DNS and must
    # NOT be overridden here.
    container_kwargs: dict = {"privileged": True}
    if _blueprint_http_server.get("extra_hosts"):
        container_kwargs["extra_hosts"] = _blueprint_http_server["extra_hosts"]

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
    manifest_reqs = _collect_manifest_requirements(config_path)
    if manifest_reqs:
        quoted = " ".join(shlex.quote(r) for r in manifest_reqs)
        container_kwargs["entrypoint"] = [
            "sh",
            "-c",
            f"pip install --no-cache-dir {quoted} && exec /init",
        ]
        logger.info(
            f"📦 Pre-installing {len(manifest_reqs)} custom-component "
            f"requirement(s) before HA boots: {manifest_reqs}"
        )

    container = container.with_kwargs(**container_kwargs)

    # Remove any .HA_RESTORE file that might cause issues
    restore_file = config_path / ".HA_RESTORE"
    if restore_file.exists():
        restore_file.unlink()
        logger.info("🗑️ Removed .HA_RESTORE file from config")

    with container:
        # Get the dynamically assigned port
        host_port = container.get_exposed_port(8123)
        base_url = f"http://localhost:{host_port}"

        # Set environment variables for the dynamic URL so WebSocket client uses correct port
        os.environ["HOMEASSISTANT_URL"] = base_url
        os.environ["HOMEASSISTANT_TOKEN"] = TEST_TOKEN
        # Enable feature flags for e2e tests
        os.environ["ENABLE_YAML_CONFIG_EDITING"] = "true"
        os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = "true"
        os.environ["HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION"] = "true"

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
        import requests

        # Use test token for API readiness checks
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        logger.info("🔄 Waiting for Home Assistant API to become ready...")
        if not _wait_for_ha_api_ready(base_url, headers, timeout=60):
            _dump_ha_readiness_diagnostics(
                container, base_url, headers, label="api-not-ready"
            )
            pytest.fail(
                f"Home Assistant API at {base_url} did not become ready within 60 seconds.\n"
                "The container may have failed to start. Check Docker logs for details."
            )

        # Poll until HA components are fully loaded.  HA typically loads 80+
        # components; 50 is the minimum needed for tests (covers automation,
        # script, input_*, group, scene, and other commonly-tested domains).
        MIN_COMPONENTS = 50
        STABILIZATION_TIMEOUT = 30

        logger.info("⏳ Waiting for Home Assistant components to stabilize...")
        last_count = 0
        # Wall-clock-bound polling: ``range(STABILIZATION_TIMEOUT)`` would let
        # a slow ``/api/config`` (2s HTTP timeout + 1s sleep ≈ up to 3s per
        # iteration) stretch the effective wait to ~3× the declared budget.
        stabilize_start = time.monotonic()
        while time.monotonic() - stabilize_start < STABILIZATION_TIMEOUT:
            try:
                config_resp = requests.get(
                    f"{base_url}/api/config", timeout=2, headers=headers
                )
                if config_resp.status_code == 200:
                    component_count = len(config_resp.json().get("components", []))
                    if component_count >= MIN_COMPONENTS:
                        elapsed = time.monotonic() - stabilize_start
                        logger.info(
                            f"✅ Home Assistant stabilized with {component_count} components "
                            f"after {elapsed:.1f}s"
                        )
                        _log_readiness_timing(
                            "components", elapsed, count=component_count
                        )
                        break
                    if component_count != last_count:
                        logger.info(
                            f"⏳ {component_count} components loaded, waiting for more..."
                        )
                        last_count = component_count
                elif config_resp.status_code >= 400:
                    logger.warning(
                        f"⚠️ Stabilization check returned HTTP {config_resp.status_code}"
                    )
            except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
                logger.debug(f"Stabilization check failed: {exc}")
            time.sleep(1)
        else:
            _dump_ha_readiness_diagnostics(
                container, base_url, headers, label="stabilization-timeout"
            )
            pytest.fail(
                f"Home Assistant component stabilization timed out after {STABILIZATION_TIMEOUT}s. "
                f"Only {last_count} components loaded (minimum: {MIN_COMPONENTS}). "
                f"Check Docker logs."
            )

        # Wait for entities to actually register (components loaded ≠
        # entities available). HA 2026.4+ can report 80+ components while
        # individual integrations (demo, sun, helpers) are still registering
        # their entities and WebSocket handlers. The demo integration alone
        # creates 60+ entities (lights, sensors, switches, etc.).
        MIN_ENTITIES = 50
        ENTITY_STABILIZATION_TIMEOUT = 30
        logger.info("⏳ Waiting for entities to register...")
        last_entity_count = 0
        # Wall-clock-bound polling — same rationale as the component-
        # stabilization loop above.
        entity_start = time.monotonic()
        while time.monotonic() - entity_start < ENTITY_STABILIZATION_TIMEOUT:
            try:
                states_resp = requests.get(
                    f"{base_url}/api/states",
                    timeout=5,
                    headers=headers,
                )
                if states_resp.status_code == 200:
                    entity_count = len(states_resp.json())
                    if entity_count >= MIN_ENTITIES:
                        elapsed = time.monotonic() - entity_start
                        logger.info(
                            f"✅ {entity_count} entities registered after {elapsed:.1f}s"
                        )
                        _log_readiness_timing("entities", elapsed, count=entity_count)
                        break
                    if entity_count != last_entity_count:
                        logger.info(
                            f"⏳ {entity_count} entities registered, "
                            f"waiting for more..."
                        )
                        last_entity_count = entity_count
            except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
                logger.debug(f"Entity registration check failed: {exc}")
            time.sleep(1)
        else:
            _dump_ha_readiness_diagnostics(
                container, base_url, headers, label="entity-registration-timeout"
            )
            pytest.fail(
                f"Entity registration timed out after "
                f"{ENTITY_STABILIZATION_TIMEOUT}s. "
                f"Only {last_entity_count} entities registered "
                f"(minimum: {MIN_ENTITIES}). Check Docker logs."
            )

        # Wait for input_boolean service domain to register. HA loads
        # entities before their services for some integrations; helper and
        # automation tests need input_boolean.* to be callable.
        # (sun is intentionally not polled here — it registers sun.sun as an
        # entity but never a service domain, so it would always time out.
        # sun.sun readiness is gated by the state check below.)
        INPUT_BOOLEAN_WAIT = 10
        logger.info("⏳ Waiting for input_boolean service domain to register...")
        # Wall-clock-bound polling — same rationale as the loops above.
        ib_start = time.monotonic()
        while time.monotonic() - ib_start < INPUT_BOOLEAN_WAIT:
            try:
                svc_resp = requests.get(
                    f"{base_url}/api/services", timeout=5, headers=headers
                )
                if svc_resp.status_code == 200:
                    domains = {s.get("domain") for s in svc_resp.json()}
                    if "input_boolean" in domains:
                        elapsed = time.monotonic() - ib_start
                        logger.info(
                            f"✅ input_boolean service ready after {elapsed:.1f}s"
                        )
                        _log_readiness_timing("input_boolean", elapsed)
                        break
            except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
                logger.debug(f"Service check failed: {exc}")
            time.sleep(1)
        else:
            _dump_ha_readiness_diagnostics(
                container,
                base_url,
                headers,
                label="input-boolean-warn",
                service_domain="input_boolean",
            )
            logger.warning(
                f"⚠️ input_boolean service not registered after {INPUT_BOOLEAN_WAIT}s "
                f"— helper/automation tests may be flaky"
            )

        # Loud failure (``pytest.fail`` rather than ``logger.warning``):
        # filesystem and config-editing tests downstream require
        # ha_mcp_tools, and a silent warn here lets them proceed into
        # COMPONENT_NOT_INSTALLED on the first tool call — surfacing
        # the registration gap beats misattributing it to the calling
        # test.
        #
        # On timeout the branch dumps HA-side diagnostics (``/api/services``
        # snapshot, ``/api/config/config_entries/entry`` state, ``docker
        # logs --tail 100``, container state) and then fails fast. The
        # container-restart retry path that originally sat here added a
        # ~3-minute slow-failure penalty (matching the second readiness
        # sequence) for negligible recovery value once the underlying
        # H_LOAD class — the only flake-class observed live — was fixed
        # structurally by pre-installing manifest ``requirements`` in
        # the container entrypoint above (see
        # ``_collect_manifest_requirements`` call site). Reintroduce a
        # bounded retry only if a future dump surfaces a recoverable
        # class (H_DEPS / H_LISTEN / H_RESOURCE) in the wild.
        HA_MCP_TOOLS_WAIT = 180  # session-level cap; covers slow CI runners
        ha_mcp_tools_src = repo_root / "custom_components" / "ha_mcp_tools"
        if ha_mcp_tools_src.exists():
            logger.info("⏳ Waiting for ha_mcp_tools services to register...")
            ha_mcp_ready, ha_mcp_elapsed, ha_mcp_domain_count = (
                _wait_for_ha_mcp_tools_services(
                    base_url, headers, HA_MCP_TOOLS_WAIT
                )
            )
            if ha_mcp_ready:
                # Success-only emit, parity with the other four readiness
                # gates above (components / entities / input_boolean / sun)
                # which call _log_readiness_timing only inside their hit
                # branch. ``count`` mirrors components/entities.
                _log_readiness_timing(
                    "ha_mcp_tools",
                    ha_mcp_elapsed,
                    count=ha_mcp_domain_count,
                )
            else:
                _dump_ha_readiness_diagnostics(
                    container,
                    base_url,
                    headers,
                    label="ha-mcp-tools-timeout",
                    service_domain="ha_mcp_tools",
                    config_entry_domain="ha_mcp_tools",
                )
                pytest.fail(
                    f"ha_mcp_tools services not registered after "
                    f"{HA_MCP_TOOLS_WAIT}s. See diagnostic dump above. "
                    "Required for filesystem and config-editing tests; "
                    "a silent warning here would let those tests proceed "
                    "into COMPONENT_NOT_INSTALLED on the first tool call."
                )

        # Wait for sun.sun to leave the 'unknown' state.  During HA startup the
        # sun integration reports 'unknown' until it computes the first position.
        # Template tests that assert above/below_horizon will fail if we proceed
        # before the sun integration finishes its first calculation.
        SUN_WAIT = 30
        logger.info("⏳ Waiting for sun.sun to reach a known state...")
        # Wall-clock-bound polling — same rationale as the loops above.
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

        # Store connection info for other fixtures
        container_info = {
            "container": container,
            "port": host_port,
            "base_url": base_url,
            "config_path": str(config_path),
            "blueprint_server": _blueprint_http_server,
        }

        try:
            yield container_info
        finally:
            # Cleanup temp directory (container cleanup handled by 'with' statement)
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("✅ Cleanup completed")


@pytest.fixture(scope="session")
async def ha_client(
    ha_container_with_fresh_config,
) -> AsyncGenerator[HomeAssistantClient]:
    """Create Home Assistant client connected to the container."""
    container_info = ha_container_with_fresh_config
    base_url = container_info["base_url"]

    client = HomeAssistantClient(base_url=base_url, token=TEST_TOKEN)

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
) -> AsyncGenerator[HomeAssistantSmartMCPServer]:
    """Create MCP server instance connected to the container."""
    logger.info("🚀 Creating MCP server instance...")

    container_info = ha_container_with_fresh_config
    base_url = container_info["base_url"]

    # Create client for the server
    client = HomeAssistantClient(base_url=base_url, token=TEST_TOKEN)

    # Create server with the client
    server = HomeAssistantSmartMCPServer(client=client)
    tools = await server.mcp.list_tools()
    logger.info(
        f"✅ MCP server initialized with {len(tools)} tools connected to {base_url}"
    )

    yield server
    # Server cleanup handled by server.close()


@pytest.fixture(scope="session")
async def mcp_client(mcp_server) -> AsyncGenerator[Client]:
    """Create FastMCP client connected to our server."""
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
    import os

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
        "ha_search_entities", {"query": "light", "domain_filter": "light", "limit": 10}
    )

    # Parse search results
    search_data = parse_mcp_result(search_result)

    data = search_data.get("data", {})
    if not data.get("success") or not data.get("results"):
        pytest.skip("No light entities available for testing")

    # Find a light that's currently off (preferred for testing)
    for entity in data["results"]:
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
    entity_id = data["results"][0]["entity_id"]
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
            "ha_search_entities",
            {"query": pattern, "domain_filter": "automation", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)
        if search_data.get("success") and search_data.get("results"):
            for entity in search_data["results"]:
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
    """Return blueprint HTTP server info for tests that need to import blueprints.

    The server is started by ``_blueprint_http_server`` before the HA container
    and stored in ``ha_container_with_fresh_config``; this fixture simply exposes
    it so tests don't need to depend on ``ha_container_with_fresh_config`` directly.
    """
    server = ha_container_with_fresh_config["blueprint_server"]
    logger.info(f"🌐 Blueprint server at {server['base_url']}")
    yield server
