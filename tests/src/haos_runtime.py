"""Shared HAOS-QEMU runtime helpers (see #1281).

Imported by both ``tests/src/e2e/conftest.py`` (for backend-switched
session fixtures) and ``tests/src/haos_e2e/conftest.py`` (for the
HAOS-only canary suite). Keeping the QEMU lifecycle + HA login-flow
code in one place avoids drift between the two backends.

Mostly stdlib — ``websockets`` is required for the inaddon-tier
Supervisor API helper (#1349 item 7). The Supervisor's ``addons/{slug}/update``
endpoint isn't in HA Core's REST PATHS_ADMIN allowlist, so triggering
an addon update from outside the addon container requires the HA Core
WebSocket API's ``supervisor/api`` command. ``websockets`` is already
a project test dep.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

# Ports the host uses to reach the booted HAOS. The base URL we build from
# these (http://127.0.0.1:<HA_HOST_PORT>) must equal the URL registered as
# /auth/token's client_id at onboarding time — otherwise the token exchange
# rejects with "invalid_request".
HA_HOST_PORT = int(os.environ.get("HAOS_TEST_HA_PORT", "18123"))
SSH_HOST_PORT = int(os.environ.get("HAOS_TEST_SSH_PORT", "12222"))
# Inaddon MCP-tier port forward — addon's HTTP MCP endpoint runs at 9583
# inside HAOS (host_network: true → host port = addon port). Hostfwd to a
# unique outer port so external-tier 18123 and inaddon-tier 19583 can
# coexist if both run on the same runner.
HA_MCP_ADDON_HOST_PORT = int(os.environ.get("HAOS_TEST_ADDON_PORT", "19583"))
# Advanced SSH addon (bake-installed for inaddon CI tier debugging — see
# build_image.install_advanced_ssh). Listens inside HAOS on 22222. The
# user/password ("root"/"haosdebug") are CI-test-only.
SSH_DEBUG_HOST_PORT = int(os.environ.get("HAOS_TEST_SSH_DEBUG_PORT", "22222"))
OVMF_CODE_PATH = os.environ.get("HAOS_BUILD_OVMF", "/usr/share/OVMF/OVMF_CODE.fd")
HAOS_IMAGE_ENV = "HAOS_TEST_IMAGE_PATH"

# Deterministic secret_path the bake pre-sets on the ha-mcp dev addon's
# options (see tests/haos_image_build/build_image.py:HA_MCP_TEST_SECRET_PATH).
# Kept in sync between the two modules manually — both are small constants
# and a cross-package import here would pull qemu/websockets build deps
# into the test runtime path.
HA_MCP_TEST_SECRET_PATH = "/mcp_e2e_test_path"
# Slug Supervisor assigns to a local addon staged under
# /supervisor/addons/local/<dir>/. Derived from config.yaml's slug ``ha_mcp_dev``
# with the ``local_`` prefix that Supervisor applies to local-store
# addons.
HA_MCP_DEV_ADDON_SLUG = "local_ha_mcp_dev"
# Advanced SSH Web Terminal addon's installed slug. Bake-installed by
# build_image.install_advanced_ssh; available on host port
# SSH_DEBUG_HOST_PORT (22222) with user=root, password=haosdebug.
SSH_ADDON_SLUG = "local_homeassistant_advanced_ssh"
SSH_ADDON_USER = "root"
SSH_ADDON_PASSWORD = "haosdebug"


def ssh_exec(
    cmd: list[str], *, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` over SSH against the booted HAOS's Advanced SSH addon.

    Used by inaddon-only tests that need to run a process inside HAOS
    itself (typically ``docker exec`` into a sibling addon container to
    poison its filesystem mid-test). The Advanced SSH addon is bake-
    installed with a known root password; we use ``sshpass`` for
    non-interactive auth and disable host-key checking because the
    addon container's key changes whenever it restarts (commit 6's
    restart-retarget test relies on this leniency).

    The caller passes the COMMAND list (``["docker", "exec", ...]``);
    this helper assembles the ssh wrapper.
    """
    ssh_cmd = [
        "sshpass", "-p", SSH_ADDON_PASSWORD,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-p", str(SSH_DEBUG_HOST_PORT),
        f"{SSH_ADDON_USER}@127.0.0.1",
        *cmd,
    ]
    return subprocess.run(
        ssh_cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def docker_exec_in_addon(
    addon_slug: str, cmd: list[str], *, timeout: float = 30.0
) -> str:
    """Run ``cmd`` inside an addon container via SSH + docker exec.

    ``addon_slug`` is the Supervisor slug (e.g. ``local_ha_mcp_dev``);
    the helper resolves it to the Docker container name (
    ``addon_<slug>``). Returns stdout of the command. Raises on
    non-zero exit (caller can catch ``subprocess.CalledProcessError``
    for graceful diagnostics).

    Used by item 8's filesystem-poisoning E2E to make
    ``/data/saved_tools.json`` unwriteable inside the dev addon
    container mid-test, force the save-warning rollback, then chmod
    back in the test's finally block.
    """
    container = f"addon_{addon_slug}"
    result = ssh_exec(["docker", "exec", container, *cmd], timeout=timeout)
    return result.stdout


def is_haos_inaddon_mode() -> bool:
    """True iff this run targets the inaddon HAOS tier (#1349 item 7).

    The inaddon mode points ``mcp_client`` at the ha-mcp dev addon's HTTP
    MCP endpoint inside the booted HAOS instead of starting an in-process
    FastMCP server. Exercises ``is_running_in_addon()=True`` code paths
    that the external-runner tier can't reach.
    """
    return os.environ.get("HAOS_TEST_MODE", "external") == "inaddon"


def is_haos_backend_selected() -> bool:
    """True iff the workflow has staged a HAOS qcow2 for this run."""
    raw = os.environ.get(HAOS_IMAGE_ENV)
    return bool(raw and Path(raw).exists())


def refresh_recorder_in_qcow2(
    image_path: Path, *, target_age_seconds: float = 300.0
) -> None:
    """Shift recorder timestamps inside the baked qcow2 to look ``recent``.

    The image cache key is content-hashed, so a cache hit re-uses an image
    whose ``home-assistant_v2.db`` timestamps are frozen at bake time —
    once that exceeds the ~24h window history queries use, every history
    pagination test silently regresses. This helper extracts the DB from
    the qcow2, runs the same uniform timestamp shift the testcontainer
    path does (``conftest._refresh_recorder_timestamps``), and copies the
    file back in place. Done once per pytest session before QEMU boots.

    Uses guestfish (libguestfs) for both copy-out and copy-in; sqlite3
    stdlib for the shift itself. ~30s wall-clock overhead per session.
    """
    import sqlite3
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="haos-ts-refresh-"))
    db_local = workdir / "home-assistant_v2.db"
    try:
        # copy-out the recorder DB from the qcow2's hassos-data partition.
        subprocess.run(
            [
                "guestfish",
                "--ro",
                "-a", str(image_path),
                "run",
                ":",
                "mount", "/dev/sda8", "/",
                ":",
                "copy-out",
                "/supervisor/homeassistant/home-assistant_v2.db",
                str(workdir),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

        # Same logic as conftest._refresh_recorder_timestamps. Kept inline
        # rather than importing because conftest pulls in heavy dev deps
        # (docker, testcontainers) that the HAOS-only paths don't need.
        TIMESTAMP_COLUMNS = {
            "states": ("last_updated_ts", "last_changed_ts", "last_reported_ts"),
            "events": ("time_fired_ts",),
            "statistics": ("start_ts", "created_ts"),
            "statistics_short_term": ("start_ts", "created_ts"),
        }
        conn = sqlite3.connect(str(db_local))
        try:
            # The bake's seed DB is in WAL journal mode. Our copy-in only
            # ships the .db file (not -wal / -shm) into the qcow2, so any
            # pending WAL frames written during our UPDATE would land in
            # the workdir alongside the .db and never reach HAOS — HA Core
            # then sees a .db whose page tree is mid-transaction, raises
            # ``sqlite3.DatabaseError: database disk image is malformed``,
            # and renames the seed to ``.corrupt.<ts>`` before booting with
            # an empty fresh DB. (Captured in PR #1361 diagnostics: see
            # /supervisor/homeassistant/home-assistant_v2.db.corrupt.*.)
            #
            # Fix: checkpoint any preexisting WAL into the main file, then
            # switch this connection to DELETE mode so our UPDATEs write
            # straight to .db with no journal artefacts. End state on disk:
            # one self-contained .db file. HA Core will switch it back to
            # WAL on first boot — its preferred mode.
            #
            # Both PRAGMAs return rows we MUST inspect: a busy WAL
            # checkpoint or a refused mode switch silently leaves the
            # same corruption the comment above describes.
            ckpt_row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            # Result shape: (busy, log_pages, checkpointed_pages). busy=0
            # means all frames were merged into the main DB.
            if ckpt_row is not None and ckpt_row[0] not in (0, None):
                raise RuntimeError(
                    f"wal_checkpoint(TRUNCATE) reported busy={ckpt_row[0]} on "
                    f"{db_local}; another connection is holding WAL frames. "
                    f"Aborting refresh — copy-in would still produce a "
                    f"WAL/SHM-mismatched DB inside the qcow2."
                )
            mode_row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            if mode_row is None or mode_row[0] != "delete":
                raise RuntimeError(
                    f"Failed to switch {db_local} to DELETE journal mode "
                    f"(got {mode_row!r}); subsequent UPDATEs would write WAL "
                    f"frames that the qcow2 copy-in won't include."
                )
            newest = 0.0
            matched_columns = 0
            for table, cols in TIMESTAMP_COLUMNS.items():
                for col in cols:
                    try:
                        row = conn.execute(
                            f"SELECT MAX({col}) FROM {table}"
                        ).fetchone()
                    except sqlite3.OperationalError as exc:
                        msg = str(exc).lower()
                        if "no such table" in msg or "no such column" in msg:
                            continue
                        raise
                    matched_columns += 1
                    if row and row[0] is not None and isinstance(row[0], (int, float)):
                        newest = max(newest, float(row[0]))

            # Schema drift guard: if HAOS bumps the recorder schema and renames
            # every column in TIMESTAMP_COLUMNS, the loop above silently
            # `continue`s through all of them and newest stays 0. Without this
            # check, history pagination tests would mysteriously fail with "no
            # data" instead of pointing at the schema bump. Mirrors the
            # testcontainer _refresh_recorder_timestamps "raise vs no-op"
            # discipline.
            if matched_columns == 0:
                raise RuntimeError(
                    f"Recorder DB at {db_local} matched zero TIMESTAMP_COLUMNS "
                    f"entries — recorder schema may have drifted; update "
                    f"TIMESTAMP_COLUMNS to match the new column names."
                )
            if newest <= 0:
                raise RuntimeError(
                    f"Recorder DB at {db_local} has no numeric timestamps in "
                    f"any of the {matched_columns} matched columns — the bake "
                    f"may have produced an empty DB; re-run "
                    f"`uv run python scripts/bake_pagination_seed.py`."
                )

            target = time.time() - target_age_seconds
            offset = target - newest
            if offset <= 0:
                LOG.info(
                    "Recorder timestamps already recent (newest=%.0f, "
                    "target=%.0f); no shift needed", newest, target,
                )
                return

            for table, cols in TIMESTAMP_COLUMNS.items():
                for col in cols:
                    try:
                        conn.execute(
                            f"UPDATE {table} SET {col} = {col} + ? "
                            f"WHERE {col} IS NOT NULL",
                            (offset,),
                        )
                    except sqlite3.OperationalError as exc:
                        msg = str(exc).lower()
                        if "no such table" in msg or "no such column" in msg:
                            continue
                        raise
            conn.commit()
            LOG.info("Shifted recorder timestamps by %+.0fs", offset)
        finally:
            conn.close()

        # copy-in the shifted DB, and PURGE any stale ``-wal`` / ``-shm``
        # sidecars in the qcow2. Two-part fix: the PRAGMA block above
        # handles the workdir DB (no WAL frames leaving our process); this
        # block handles the qcow2 side.
        #
        # The bake (build_image.py) boots HA Core to bootstrap state — Core
        # writes the recorder DB in WAL mode and leaves
        # ``home-assistant_v2.db``, ``home-assistant_v2.db-wal``, and
        # ``home-assistant_v2.db-shm`` in the supervisor data dir. Our
        # copy-out only fetches the ``.db``, so when we copy the edited
        # ``.db`` back, the old ``-wal`` / ``-shm`` from bake are still
        # there — referencing page numbers from the pre-edit ``.db`` that
        # no longer exist. On boot HA Core opens ``.db`` + ``-wal``
        # together, sqlite finds the page-tree inconsistent, and raises
        # ``sqlite3.DatabaseError: database disk image is malformed``
        # (PR #1361 diagnostics: ha-core-runtime.log captured this exact
        # message, plus ``home-assistant_v2.db-wal.corrupt.<ts>`` proving
        # a WAL file was present). The seed is then renamed to
        # ``.corrupt.<ts>`` and HA boots with a fresh empty DB — losing
        # all the pre-baked history rows.
        #
        # --rw so guestfish opens the qcow2 for write; the file's
        # owner/perms inside the qcow2 are preserved by libguestfs when
        # overwriting an existing path.
        try:
            subprocess.run(
                [
                    "guestfish",
                    "--rw",
                    "-a", str(image_path),
                    "run",
                    ":",
                    "mount", "/dev/sda8", "/",
                    ":",
                    "copy-in",
                    str(db_local),
                    "/supervisor/homeassistant/",
                    ":",
                    "rm-f",
                    "/supervisor/homeassistant/home-assistant_v2.db-wal",
                    ":",
                    "rm-f",
                    "/supervisor/homeassistant/home-assistant_v2.db-shm",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # Partial-write recovery hint: the previous copy-out + sqlite
            # mutate succeeded but the write-back is gone, so the qcow2's
            # recorder DB is whatever the bake left there. Subsequent runs
            # against this cached image will repeat the same shift on the
            # original timestamps — safe but slow. If the recorder DB
            # appears corrupt downstream, delete the image and let the
            # next CI run rebuild.
            LOG.error(
                "guestfish copy-in failed for %s; cached qcow2 may need "
                "a manual rebuild (delete /tmp/haos-test-image.qcow2 + "
                "the matching cache key, then re-run)",
                image_path,
            )
            raise
        LOG.info("Refreshed recorder DB in %s", image_path)
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def refresh_dev_addon_source_in_qcow2(image_path: Path) -> None:
    """Overwrite the staged ha-mcp dev addon source with the PR's current source.

    The cached qcow2 ships with the addon installed + Docker image built
    from whatever HEAD source the bake captured. To exercise the
    PR-under-review code, this helper:

    1. Walks the working tree for the addon-build-context files
       (homeassistant-addon-dev/* + start.py from homeassistant-addon/ +
       pyproject.toml + uv.lock + src/ha_mcp/).
    2. Bumps the addon's config.yaml ``version:`` so Supervisor's
       local-store scanner reports an update-available on next boot.
       Bump format: ``<base>-pr-<GITHUB_SHA[:7] or "local">`` so every
       distinct PR commit produces a distinct version (Supervisor caches
       by exact version string).
    3. libguestfs replaces the /supervisor/addons/local/ha_mcp_dev/
       directory (rm-rf + tar-in of parent) on the offline qcow2.

    Subsequent boot + ``addons/{slug}/update`` via Supervisor WS picks up
    the new files and rebuilds the addon's Docker image. Because the
    cached qcow2 already has the base Docker layers cached, the rebuild
    only re-executes the bottom layers (COPY src/, uv sync project) —
    typically ~20-30s end-to-end.
    """
    import shutil as _shutil
    import tempfile as _tempfile

    repo_root = Path(__file__).resolve().parent.parent.parent
    dev_addon_src = repo_root / "homeassistant-addon-dev"
    if not dev_addon_src.exists():
        raise RuntimeError(
            f"homeassistant-addon-dev not found at {dev_addon_src} — "
            f"checkout is incomplete; inaddon tier cannot refresh source."
        )

    workdir = Path(_tempfile.mkdtemp(prefix="haos-inaddon-refresh-"))
    try:
        staging = workdir / "ha_mcp_dev"
        _shutil.copytree(dev_addon_src, staging)

        # Same file-shaping as build_image.stage_dev_addon_source so the
        # build context matches what the cached Docker layers expect.
        _shutil.copy(
            repo_root / "homeassistant-addon" / "start.py",
            staging / "start.py",
        )
        _shutil.copy(repo_root / "pyproject.toml", staging / "pyproject.toml")
        _shutil.copy(repo_root / "uv.lock", staging / "uv.lock")
        addon_src_dir = staging / "src"
        if addon_src_dir.exists():
            _shutil.rmtree(addon_src_dir)
        addon_src_dir.mkdir()
        _shutil.copytree(repo_root / "src" / "ha_mcp", addon_src_dir / "ha_mcp")

        # Dockerfile shape fixup (same as bake).
        dockerfile = staging / "Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace(
            "COPY homeassistant-addon/start.py /",
            "COPY start.py /",
        ))

        # Strip image: from config.yaml — Supervisor pulls from GHCR when
        # image: is set, but the per-PR version we bump to below doesn't
        # exist there. Force local Dockerfile build by removing the field.
        # Same fix the bake's stage_dev_addon_source applies.
        config_path_pre = staging / "config.yaml"
        config_path_pre.write_text(
            "".join(
                ln for ln in config_path_pre.read_text().splitlines(keepends=True)
                if not ln.startswith("image:")
            )
        )

        # Tag with GITHUB_SHA when in CI so each PR commit gets its own
        # version string — Supervisor caches install state by exact
        # version, so two consecutive CI runs with the same version
        # would no-op the update.
        sha = (os.environ.get("GITHUB_SHA", "") or "local")[:7] or "local"
        config_path = staging / "config.yaml"
        config_text = config_path.read_text()
        # config.yaml is human-edited; preserve line shape rather than
        # round-tripping through a YAML parser (which would lose comments).
        new_lines: list[str] = []
        bumped = False
        for line in config_text.splitlines(keepends=True):
            if line.startswith("version:") and not bumped:
                # ``version: "devNNN"`` → ``version: "devNNN-pr-<sha>"``
                prefix, _, rest = line.partition(":")
                base = rest.strip().strip('"').strip("'")
                new_lines.append(f'{prefix}: "{base}-pr-{sha}"\n')
                bumped = True
            else:
                new_lines.append(line)
        if not bumped:
            raise RuntimeError(
                "No version: line in homeassistant-addon-dev/config.yaml — "
                "cannot trigger Supervisor update without a version bump."
            )
        config_path.write_text("".join(new_lines))
        LOG.info("Bumped addon version to pr-%s for update-detection", sha)

        # Build the tar, then replace /supervisor/addons/local/ha_mcp_dev/ in the qcow2.
        # rm-rf + tar-in (rather than tar-in alone) so removed files in the
        # PR source actually disappear from the addon dir — leftover files
        # would be picked up by the next Docker build.
        seed_tar = workdir / "ha_mcp_dev.tar"
        subprocess.run(
            [
                "tar", "--numeric-owner", "--owner=0", "--group=0",
                "-C", str(workdir), "-cf", str(seed_tar), "ha_mcp_dev",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            [
                "guestfish",
                "--rw",
                "-a", str(image_path),
                "run",
                ":",
                "mount", "/dev/sda8", "/",
                ":",
                "rm-rf", "/supervisor/addons/local/ha_mcp_dev",
                ":",
                "tar-in", str(seed_tar), "/supervisor/addons/local",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        LOG.info("Refreshed ha-mcp dev addon source in %s", image_path)
    finally:
        _shutil.rmtree(workdir, ignore_errors=True)


def wait_for_addon_mcp_ready(*, timeout: float = 300.0) -> str:
    """Poll the inaddon MCP HTTP port until TCP-accept succeeds + HTTP-ready.

    Returns the full base URL on success.

    Two-stage probe because the addon's FastMCP streamable-HTTP
    transport RSTs plain GET requests (it expects MCP-shaped POSTs
    only):

    1. TCP-level connect — proves the addon container has bound the
       listener socket. But slirp's NAT may complete the SYN/ACK
       handshake at the slirp boundary even when the guest-side
       listener isn't fully up yet, so this alone isn't sufficient.
    2. After TCP succeeds, send an HTTP OPTIONS to the secret_path and
       accept any 2xx/3xx/405 response — Uvicorn answers OPTIONS even
       when MCP's handler RSTs GET. 405-not-allowed is a perfectly
       good "listener is HTTP-ready" signal.
    """
    base_url = f"http://127.0.0.1:{HA_MCP_ADDON_HOST_PORT}{HA_MCP_TEST_SECRET_PATH}"
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    last_status: int | None = None
    tcp_ready_at: float | None = None
    while time.monotonic() < deadline:
        # Stage 1: TCP probe.
        if tcp_ready_at is None:
            try:
                with socket.create_connection(
                    ("127.0.0.1", HA_MCP_ADDON_HOST_PORT), timeout=5.0
                ):
                    tcp_ready_at = time.monotonic()
                    LOG.info(
                        "Inaddon MCP TCP port %d open at %.1fs into wait",
                        HA_MCP_ADDON_HOST_PORT,
                        tcp_ready_at - (deadline - timeout),
                    )
            except OSError as e:
                last_err = e
                time.sleep(3.0)
                continue

        # Stage 2: HTTP-level probe. OPTIONS is the safest verb for an
        # unknown server — RFC-required to be implemented and Uvicorn's
        # default handler responds 405-method-not-allowed even when the
        # mounted app rejects all methods.
        #
        # Accept codes that mean "Uvicorn answered with knowledge of this
        # route": 200/204/3xx/401/403/405. Treat 5xx as transient (server
        # is binding/crashing/restarting — keep retrying within the outer
        # timeout). Treat 404 as fatal: it means the secret_path under
        # which the MCP app is mounted doesn't match
        # ``HA_MCP_TEST_SECRET_PATH`` here — a config-drift bug worth
        # surfacing now instead of as a confusing MCP failure later.
        req = urllib.request.Request(base_url, method="OPTIONS")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                last_status = resp.status
                LOG.info(
                    "Inaddon MCP HTTP ready at %s (OPTIONS → %d)",
                    base_url, resp.status,
                )
                return base_url
        except urllib.error.HTTPError as e:
            last_status = e.code
            if e.code == 404:
                raise RuntimeError(
                    f"Inaddon MCP probe got 404 at {base_url} — the addon "
                    f"booted but the secret_path mount doesn't match "
                    f"HA_MCP_TEST_SECRET_PATH={HA_MCP_TEST_SECRET_PATH!r}. "
                    f"Check that bake-time options install set the same "
                    f"value (build_image.install_ha_mcp_dev_addon)."
                ) from e
            if 500 <= e.code < 600:
                # Server up but in a bad state — keep retrying within the
                # outer deadline.
                last_err = e
                time.sleep(3.0)
                continue
            # 2xx/3xx/401/403/405 — route exists, listener is HTTP-ready.
            LOG.info(
                "Inaddon MCP HTTP ready at %s (OPTIONS → %d, expected)",
                base_url, e.code,
            )
            return base_url
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(3.0)

    raise TimeoutError(
        f"Inaddon MCP endpoint {base_url} did not become reachable within "
        f"{timeout}s (last_status={last_status}, last_exc={last_err!r})"
    )


def _http(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data: bytes | None
    headers: dict[str, str] = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        # Surface the body so login_for_token failures don't show as a bare
        # "HTTPError: 400 Bad Request" — the response body almost always
        # names the specific validation that failed.
        try:
            err_body = e.read().decode()
        except (OSError, UnicodeDecodeError):
            err_body = ""
        LOG.error("%s %s → HTTP %s: %s", method, url, e.code, err_body)
        raise
    return json.loads(raw) if raw else {}


def _wait_port(port: int, host: str = "127.0.0.1", timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(2.0)
    raise TimeoutError(f"{host}:{port} did not open within {timeout}s")


def _wait_http_ok(url: str, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
        time.sleep(3.0)
    raise TimeoutError(f"{url} did not become ready within {timeout}s (last: {last_err})")


DEFAULT_BACKUP_PASSWORD = "e2e-test-backup-password"


def set_default_backup_password(
    base_url: str, token: str, *, password: str = DEFAULT_BACKUP_PASSWORD
) -> None:
    """Set HA Core's default backup-create password via WS at session start.

    ``ha_backup_create`` and friends fail with ``SERVICE_CALL_FAILED``
    ("No default backup password configured in Home Assistant") unless
    ``create_backup.password`` is set in the backup integration's stored
    config. On HAOS we can't reliably bake this into ``.storage/backup``
    (the file is written by HA Core's backup integration on first save —
    pre-baking the file occasionally races with the integration's own
    storage migration on a fresh-boot HAOS). Doing it at session start
    via the WS ``backup/config/update`` command is deterministic.

    Idempotent: HA Core overwrites the stored ``password`` field on
    each call, so re-running this against a HAOS that already has a
    password set is harmless.
    """
    import websockets.sync.client

    ws_url = (
        base_url.replace("http://", "ws://").replace("https://", "wss://")
        + "/api/websocket"
    )
    with websockets.sync.client.connect(ws_url, max_size=None) as ws:
        first = json.loads(ws.recv())
        if first.get("type") != "auth_required":
            raise RuntimeError(
                f"WS handshake: expected auth_required, got {first!r}"
            )
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth rejected: {auth_resp}")

        msg_id = 1
        ws.send(json.dumps({
            "id": msg_id,
            "type": "backup/config/update",
            "create_backup": {"password": password},
        }))
        # Read frames until our id arrives. Short timeout: this is a
        # storage write, not a Docker rebuild — sub-second under normal
        # conditions.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(deadline - time.monotonic(), 1.0))
            except TimeoutError:
                continue
            if not isinstance(raw, str):
                raw = raw.decode()
            resp = json.loads(raw)
            if resp.get("id") != msg_id:
                continue
            if not resp.get("success", False):
                raise RuntimeError(
                    f"backup/config/update failed: {resp.get('error')}"
                )
            LOG.info("Default backup password configured via WS")
            return
        raise TimeoutError(
            "backup/config/update did not complete within 30s"
        )


def login_for_token(base_url: str, username: str, password: str) -> str:
    """Drive HA's login flow against a pre-onboarded image, return access token.

    Same shape as the HA frontend's auth: /auth/login_flow (start) →
    /auth/login_flow/<flow_id> (submit creds) → /auth/token (form-encoded
    exchange). The returned access token is short-lived (~30 min) but long
    enough for a single test session.
    """
    flow = _http(
        "POST",
        f"{base_url}/auth/login_flow",
        body={
            "client_id": base_url,
            "handler": ["homeassistant", None],
            "redirect_uri": base_url,
        },
    )
    flow_id = flow["flow_id"]
    submit = _http(
        "POST",
        f"{base_url}/auth/login_flow/{flow_id}",
        body={"client_id": base_url, "username": username, "password": password},
    )
    if submit.get("type") != "create_entry":
        raise RuntimeError(f"login_flow rejected credentials: {submit}")
    auth_code = submit["result"]
    token_resp = _http(
        "POST",
        f"{base_url}/auth/token",
        # /auth/token uses await request.post() — must be form-encoded
        # (same gotcha as the build script).
        form={
            "client_id": base_url,
            "grant_type": "authorization_code",
            "code": auth_code,
        },
    )
    return token_resp["access_token"]


@contextmanager
def boot_haos_qemu(image_path: Path, serial_log: Path | None = None) -> Iterator[str]:
    """Boot a HAOS qcow2 under QEMU/KVM; yield the HA base URL.

    Caller is responsible for guarding with ``is_haos_backend_selected()``
    or similar before invoking. On context exit, terminates QEMU (SIGTERM
    then SIGKILL after 60s if still alive).
    """
    if not Path("/dev/kvm").exists():
        raise RuntimeError("/dev/kvm not available — HAOS tests require KVM acceleration")

    serial = serial_log or Path("/tmp/haos-e2e-serial.log")
    cmd = [
        "qemu-system-x86_64",
        "-machine", "q35,accel=kvm",
        "-cpu", "host",
        "-smp", "2",
        "-m", "4096",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE_PATH}",
        "-drive", f"if=virtio,file={image_path},format=qcow2",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{HA_HOST_PORT}-:8123,"
        f"hostfwd=tcp:127.0.0.1:{SSH_HOST_PORT}-:22,"
        f"hostfwd=tcp:127.0.0.1:{HA_MCP_ADDON_HOST_PORT}-:9583,"
        f"hostfwd=tcp:127.0.0.1:{SSH_DEBUG_HOST_PORT}-:22222",
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", f"file:{serial}",
    ]
    LOG.info("Booting HAOS (serial log: %s)", serial)
    proc = subprocess.Popen(cmd)
    base_url = f"http://127.0.0.1:{HA_HOST_PORT}"
    try:
        _wait_port(HA_HOST_PORT, timeout=180)
        _wait_http_ok(f"{base_url}/manifest.json", timeout=600)
        LOG.info("HAOS frontend ready at %s", base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            LOG.warning(
                "QEMU did not exit within 60s of SIGTERM; escalating to SIGKILL"
            )
            proc.kill()
            proc.wait()


def trigger_dev_addon_update(base_url: str, token: str, *, timeout: float = 600.0) -> None:
    """Trigger Supervisor's ``addons/{slug}/update`` then start the dev addon.

    The cached qcow2 ships with the addon installed at the bake-time
    source version; ``refresh_dev_addon_source_in_qcow2`` has just
    overwritten ``/supervisor/addons/local/ha_mcp_dev/`` with the PR's source and
    bumped the addon's ``config.yaml`` version. Asking Supervisor to
    update detects the new version, rebuilds the addon's Docker image
    (Docker layer cache → only COPY src/ + uv-sync-project layers
    re-execute) — but does NOT auto-restart the container. We explicitly
    call ``/start`` after update completes; without this, the addon ends
    up in Supervisor's ``boot_fail`` state with the new image built but
    never running (verified at CI run 26046741970 supervisor log).

    Uses the HA Core WebSocket API's ``supervisor/api`` command because
    ``addons/{slug}/update`` is NOT in HA Core's REST PATHS_ADMIN
    allowlist (HAOS source verified: ``hassio/http.py:PATHS_ADMIN`` only
    covers logs/changelog/documentation/backups). Same mechanism as
    ``build_image.py``'s ``HAWebSocket.supervisor_api``.
    """
    import websockets.sync.client

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    LOG.info("Connecting to HA WS for Supervisor update: %s", ws_url)

    def _await_supervisor_result(msg_id: int, op_endpoint: str, op_deadline: float) -> None:
        """Read WS frames until the response for ``msg_id`` arrives or deadline hits.

        Supervisor stays silent during slow Docker rebuilds (worst case
        ~14-18 min on cache miss). The per-recv timeout exists only to
        let the outer ``op_deadline`` govern; on each per-recv timeout
        we re-poll. Bare propagation would skip our descriptive error.
        """
        while time.monotonic() < op_deadline:
            remaining = max(op_deadline - time.monotonic(), 1.0)
            try:
                raw = ws.recv(timeout=remaining)
            except TimeoutError:
                continue
            if not isinstance(raw, str):
                raw = raw.decode()
            resp = json.loads(raw)
            if resp.get("id") != msg_id:
                continue
            if not resp.get("success", False):
                raise RuntimeError(
                    f"supervisor/api {op_endpoint} failed: {resp.get('error')}"
                )
            return
        raise TimeoutError(
            f"supervisor/api {op_endpoint} did not complete within deadline "
            f"({op_deadline - (op_deadline - timeout):.0f}s after start)"
        )

    with websockets.sync.client.connect(ws_url, max_size=None) as ws:
        first_frame = json.loads(ws.recv())
        if first_frame.get("type") != "auth_required":
            raise RuntimeError(
                f"WS handshake: expected auth_required as first frame, got "
                f"{first_frame!r}"
            )
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth rejected: {auth_resp}")

        # Trigger the update. ``backup=false`` skips Supervisor's pre-update
        # snapshot (saves ~30s, irrelevant for CI).
        msg_id = 1
        update_endpoint = f"/addons/{HA_MCP_DEV_ADDON_SLUG}/update"
        ws.send(json.dumps({
            "id": msg_id,
            "type": "supervisor/api",
            "endpoint": update_endpoint,
            "method": "post",
            "timeout": timeout,
            "data": {"backup": False},
        }))
        _await_supervisor_result(msg_id, update_endpoint, time.monotonic() + timeout)
        LOG.info("Dev addon update completed via Supervisor WS; starting addon")

        # Explicit start after update — Supervisor leaves the addon in
        # boot_fail state otherwise, with the new image built but no
        # running container. See docstring for the CI log evidence.
        msg_id = 2
        start_endpoint = f"/addons/{HA_MCP_DEV_ADDON_SLUG}/start"
        ws.send(json.dumps({
            "id": msg_id,
            "type": "supervisor/api",
            "endpoint": start_endpoint,
            "method": "post",
            "timeout": 120,
        }))
        _await_supervisor_result(msg_id, start_endpoint, time.monotonic() + 120)
        LOG.info("Dev addon started via Supervisor WS")
