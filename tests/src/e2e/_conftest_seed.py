"""Seeding of the fresh Home Assistant config directory before the container boots."""

import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _is_missing_column_or_table_error(exc: sqlite3.OperationalError) -> bool:
    """Return True only for benign 'schema drift' errors (column/table missing).

    Other ``OperationalError`` causes (locked DB, disk I/O error, malformed
    image, readonly DB) must propagate — silently swallowing them is exactly
    the regression class this helper exists to prevent.
    """
    msg = str(exc).lower()
    return "no such table" in msg or "no such column" in msg


def _find_newest_recorder_timestamp(
    conn: sqlite3.Connection,
    timestamp_columns: dict[str, tuple[str, ...]],
) -> float:
    """Return the max numeric timestamp across ``timestamp_columns`` (0.0 if none)."""
    newest = 0.0
    for table, cols in timestamp_columns.items():
        for col in cols:
            try:
                row = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
            except sqlite3.OperationalError as exc:
                if _is_missing_column_or_table_error(exc):
                    continue
                raise
            if row and row[0] is not None and isinstance(row[0], (int, float)):
                newest = max(newest, float(row[0]))
    return newest


def _shift_recorder_timestamps(
    conn: sqlite3.Connection,
    timestamp_columns: dict[str, tuple[str, ...]],
    offset: float,
) -> int:
    """Add ``offset`` to every non-null timestamp cell; return rows updated."""
    rows_updated = 0
    for table, cols in timestamp_columns.items():
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
    return rows_updated


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

        newest = _find_newest_recorder_timestamp(conn, TIMESTAMP_COLUMNS)

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

        rows_updated = _shift_recorder_timestamps(conn, TIMESTAMP_COLUMNS, offset)
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


def _clear_stale_hacs_lock(lock_dir: Path) -> None:
    """Remove an orphaned HACS frontend lock older than a safe threshold."""
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


def _wait_for_hacs_lock_release(lock_dir: Path) -> None:
    """Poll until the winning worker releases the HACS frontend lock (≤180s)."""
    wait_start = time.monotonic()
    while lock_dir.exists():
        if time.monotonic() - wait_start >= 180:
            # The owner may still be publishing, even if both sentinels exist.
            raise RuntimeError(
                f"Timeout waiting for HACS frontend lock release at {lock_dir}"
            )
        time.sleep(2)


def _is_valid_hacs_frontend(path: Path) -> bool:
    """Require both the imported Python package and its browser entry point."""
    return all((path / name).is_file() for name in ("__init__.py", "entrypoint.js"))


class _HacsMetadataRedirectRefused(urllib.request.HTTPRedirectHandler):
    """Keep metadata credentials on the fixed GitHub API endpoint."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url, code, "HACS release metadata redirect refused", headers, fp
        )


def _hacs_frontend_release_tag() -> str:
    request = urllib.request.Request(
        "https://api.github.com/repos/hacs/frontend/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
    )
    token = os.environ.get("HACS_FRONTEND_GITHUB_TOKEN") or os.environ.get(
        "GITHUB_TOKEN"
    )
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_HacsMetadataRedirectRefused())
    with opener.open(request, timeout=30) as response:
        tag_name: str = json.loads(response.read())["tag_name"]
    return tag_name


def _download_hacs_frontend(frontend_dir: Path) -> None:
    """Download the latest HACS frontend release into ``frontend_dir``."""
    import tarfile

    if frontend_dir.exists():
        # Discard an incomplete frontend before publishing a replacement.
        logger.warning(
            f"HACS frontend at {frontend_dir} is partial or corrupt; "
            f"removing before re-download."
        )
        shutil.rmtree(frontend_dir)
    logger.info("HACS frontend not found, downloading...")

    try:
        tag_name = _hacs_frontend_release_tag()

        # Download and extract the frontend
        tarball_url = f"https://github.com/hacs/frontend/releases/download/{tag_name}/hacs_frontend-{tag_name}.tar.gz"
        logger.info(f"Downloading HACS frontend {tag_name}...")

        with (
            urllib.request.urlopen(tarball_url, timeout=120) as response,
            tarfile.open(fileobj=response, mode="r:gz") as tar,
            tempfile.TemporaryDirectory(dir=frontend_dir.parent) as temp_dir_str,
        ):
            # Stage on the destination filesystem so publication cannot fall
            # back to a partial copy. Failed extraction/rename is cleaned up.
            temp_extract = Path(temp_dir_str)
            tar.extractall(temp_extract, filter="data")

            # Move the hacs_frontend subdirectory
            extracted_frontend = (
                temp_extract / f"hacs_frontend-{tag_name}" / "hacs_frontend"
            )
            if not _is_valid_hacs_frontend(extracted_frontend):
                raise RuntimeError(
                    f"Incomplete HACS frontend archive for {tag_name}: "
                    "missing __init__.py or entrypoint.js"
                )
            extracted_frontend.rename(frontend_dir)
            logger.info(f"HACS frontend installed at {frontend_dir}")

    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        KeyError,
        tarfile.TarError,
        OSError,
    ):
        logger.exception("Failed to download HACS frontend")
        raise


def _ensure_hacs_frontend(initial_state_path: Path) -> None:
    """Download HACS frontend if not present.

    HACS requires the frontend (~51MB) to be present to fully initialize.
    This is not committed to git to keep the repo size manageable.

    Uses a directory-based atomic lock so concurrent xdist workers don't race
    when publishing into the shared ``initial_test_state`` path. One worker
    wins ``mkdir(exist_ok=False)`` and performs the download; the losers poll
    on the lock and exit when the winner releases it.
    """

    hacs_dir = initial_state_path / "custom_components" / "hacs"
    frontend_dir = hacs_dir / "hacs_frontend"
    lock_dir = initial_state_path / ".hacs_frontend.lock"

    _clear_stale_hacs_lock(lock_dir)

    # The lock still belongs to the publisher even when sentinels exist.
    # Accept publication only after release; a failed owner leaves no frontend.
    if not hacs_dir.exists() or (
        _is_valid_hacs_frontend(frontend_dir) and not lock_dir.exists()
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
        _wait_for_hacs_lock_release(lock_dir)
        if not _is_valid_hacs_frontend(frontend_dir):
            raise RuntimeError(
                "HACS frontend unavailable after bootstrap lock release"
            ) from None
        return

    # We own the lock. Outer try/finally guarantees release even when the
    # download block is skipped (e.g. hacs_dir missing) or raises.
    try:
        # Check if HACS is installed and frontend is missing or invalid.
        if hacs_dir.exists() and not _is_valid_hacs_frontend(frontend_dir):
            _download_hacs_frontend(frontend_dir)
    finally:
        # Release the lock so waiting workers can proceed. A
        # ``FileNotFoundError`` can mean startup stale-lock cleanup removed
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
                f"release — concurrent stale-lock cleanup?"
            )


def _install_custom_component(
    config_path: Path,
    component_src: Path,
    domain: str,
    title: str,
    *,
    seed_entry: bool = True,
) -> bool:
    """Install a custom component into the test HA config.

    Copies component source into custom_components/<domain> and injects a
    config entry so HA loads it on startup. Returns True if installed.

    ``seed_entry=False`` copies the files but seeds NO config entry — the
    no-tools lanes (#2292) need ha_mcp_tools' code present (the in-process
    server entry loads from it) with its File & YAML Tools entry absent, so
    the privileged services never register.
    """
    if not component_src.exists():
        logger.info("%s source not found — skipping installation", domain)
        return False

    dest = config_path / "custom_components" / domain
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(component_src, dest, dirs_exist_ok=True)

    # Inject config entry if not already present
    storage_file = config_path / ".storage" / "core.config_entries"
    if seed_entry and storage_file.exists():
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

    logger.info(
        "Installed %s component%s", domain, "" if seed_entry else " (no config entry)"
    )
    return True


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


# scene_id / entity_id the YAML-package scene below lands at. HA slugifies the
# ``name`` into the entity_id while the declared ``id`` becomes the registry
# unique_id / storage key. They are deliberately DIFFERENT here so the e2e can
# discriminate the registry-hit classification branch (see the helper below).
E2E_YAML_PACKAGE_SCENE_ENTITY_ID = (
    "e2e_yaml_scene_1971"  # HA slugifies the name to this
)
E2E_YAML_PACKAGE_SCENE_UID = (
    "e2e_yaml_scene_1971_storage_uid"  # declared id / storage key
)
# Sibling scene declaring NO ``id``. HA core makes ``id`` optional and maps it
# straight to the entity's unique_id (homeassistant/components/homeassistant/
# scene.py: ``unique_id`` -> ``scene_config.id``), so this one gets no registry
# entry at all while still living in the state machine under its name slug -
# the registry-MISS arm of the same not-storage-scene case.
E2E_YAML_PACKAGE_SCENE_IDLESS_ENTITY_ID = "e2e_yaml_scene_1971_idless"


def _seed_yaml_package_scene(config_path: Path) -> None:
    """Stage a YAML-package scene declaring an ``id`` pre-boot (#1971).

    A scene defined in a YAML package with an ``id`` registers in the entity
    registry (unique_id = that ``id``) yet has NO entry in the managed
    ``scenes.yaml`` store, so ``config/scene/config/{id}`` 404s. That is the
    exact registry-hit not-storage-scene case #1971 classifies as
    CONFIG_NOT_FOUND. It is also the only arm of that case that can run
    in-container (the Hue/vendor arm needs a real integration). No tool can
    create it: scene writes go through the managed store, and a post-boot host
    write to the bind-mounted config dir doesn't propagate in CI, so it is
    staged here like the legacy backups above. The folder name matches the one
    initial_test_state/configuration.yaml binds via ``!include_dir_named``.

    The declared ``id`` is deliberately NOT the slug of ``name``: the caller
    references the scene by its name-derived entity slug, which resolves in the
    registry to this distinct ``id`` (registry_hit). So a regression that
    dropped the registry-hit classification branch would fall through to the
    state-machine check on the storage key ``id`` (which is NOT a real entity,
    since the entity is the name slug), yielding the generic 404 and failing the
    e2e. That makes the test pin the registry-hit path, not just the state-check
    fallback.

    A second scene in the same package declares NO ``id``. It therefore has no
    registry entry at all (registry MISS) yet still exists in the state machine,
    which is the other arm the classification has to cover: on the read path via
    the state check, and on the no-hash write path so a plain ``set`` cannot
    shadow-create over it either.

    The filename must NOT start with an underscore: ``!include_dir_named`` uses
    the file stem as the package name, and ``PACKAGES_CONFIG_SCHEMA`` validates
    each package name with ``cv.slug``, which rejects a leading underscore
    (slugify strips it, so ``value != slugify(value)`` raises). A ``_``-prefixed
    name makes HA discard the whole package and the scene silently never loads.
    """
    packages_dir = config_path / "custom_packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    (packages_dir / "e2e_yaml_scene.yaml").write_text(
        "scene:\n"
        f"  - id: {E2E_YAML_PACKAGE_SCENE_UID}\n"
        "    name: E2E YAML Scene 1971\n"
        "    entities:\n"
        "      light.bed_light:\n"
        '        state: "on"\n'
        "  - name: E2E YAML Scene 1971 Idless\n"
        "    entities:\n"
        "      light.bed_light:\n"
        '        state: "on"\n'
    )


# The component's own server-pin rule, loaded by path: importing the package
# would pull in Home Assistant.
_PIN_RULE_SPEC = importlib.util.spec_from_file_location(
    "server_pin",
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "ha_mcp_tools"
    / "server_pin.py",
)
assert _PIN_RULE_SPEC is not None and _PIN_RULE_SPEC.loader is not None
_server_pin = importlib.util.module_from_spec(_PIN_RULE_SPEC)
_PIN_RULE_SPEC.loader.exec_module(_server_pin)


def is_server_requirement(requirement: str) -> bool:
    """True for the ha-mcp server pin the component manifest carries (#2427).

    On the embedded lanes the checkout's own wheel supplies the server, so
    installing the pinned PyPI release first would put released code under
    test (pip then treats the same-version wheel as already satisfied).
    """
    return _server_pin.server_dist(requirement) is not None


def _collect_manifest_requirements(
    config_path: Path, *, skip_server: bool = False
) -> list[str]:
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
            if not isinstance(req, str) or req in reqs:
                continue
            if skip_server and is_server_requirement(req):
                continue
            reqs.append(req)
    return reqs
