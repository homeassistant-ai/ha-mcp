"""Hold a live HA test instance up for dev-ha-env.yml, following a branch.

The workflow copies this file into the tracked checkout's tests/src/e2e/ and
runs it with pytest, so the E2E session fixture does the whole bring-up for the
selected backend: the Docker container (standalone or embedded server) or HAOS
(external, embedded or app). It writes the local targets to tunnel to
DEVENV_TARGETS, then every POLL_S fetches TRACK_REF and applies a new commit
the way a user's update would:

- component: replace custom_components/ha_mcp_tools, as HACS does, and restart
  Home Assistant.
- server, standalone: restart the server process on the runner.
- server, embedded: point the server entry at a wheel built from the commit,
  as ha_dev_manage_server(update_source) does; the entry reloads and reinstalls.
- server, app: refresh the app source with a bumped version and run
  Supervisor's app update.

Status lines (STATUS {...}) go to the job log.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
from haos_runtime import (
    DEV_ADDON_REPO_FILES,
    HA_MCP_SERVER_DOMAIN,
    SSH_ADDON_PASSWORD,
    SSH_ADDON_USER,
    SSHPASS_BIN,
    _build_embedded_server_wheel,
    _home_assistant_ws_command,
    _ssh_debug_host_port,
    trigger_dev_addon_update,
    wait_for_addon_mcp_ready,
)

from . import _conftest_embedded, _conftest_haos, _conftest_testcontainer
from ._conftest_embedded import _wait_for_embedded_webhook_ready
from ._conftest_readiness import _wait_for_ha_api_ready

SRC = Path(__file__).resolve().parents[3]
TRACK_REF = os.environ["TRACK_REF"]
POLL_S = 20
COMPONENT = Path("custom_components") / HA_MCP_SERVER_DOMAIN
SERVER_PATHS = ("src/ha_mcp/", "pyproject.toml", "uv.lock")
# Everything build_dev_addon_source_tar copies into the app's build context.
APP_PATHS = (
    *SERVER_PATHS,
    *DEV_ADDON_REPO_FILES,
    "homeassistant-addon-dev/",
    "homeassistant-addon/start.py",
)
# A failed update is retried on later polls this many times before it waits
# for the next commit.
MAX_TRIES = 3

# Agents meet strict best-practices mode by default; the E2E suite pins it off.
STRICT_BPS = os.environ.get("DEVENV_STRICT_BPS", "true") == "true"
_conftest_embedded._EMBEDDED_FEATURE_FLAGS["enable_strict_mandatory_bps"] = STRICT_BPS


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(SRC), *args], text=True).strip()


def log(**status: object) -> None:
    print("STATUS", json.dumps(status), flush=True)


def _boot_gate(url: str, timeout: int, **kwargs: Any) -> bool:
    """The session fixture's embedded-server gate, made non-fatal.

    A server that never comes up must not take Home Assistant down with it:
    the poll loop reports it and applies the push that fixes it.
    """
    if not _wait_for_embedded_webhook_ready(url, timeout, **kwargs):
        log(ready=False, error=f"embedded server did not answer within {timeout}s")
    return True


_conftest_testcontainer._wait_for_embedded_webhook_ready = _boot_gate
_conftest_haos._wait_for_embedded_webhook_ready = _boot_gate


def haos_shell(command: str, data: bytes | None = None, timeout: float = 300) -> str:
    """Run ``command`` in the HAOS host via the Advanced SSH app, feeding ``data``."""
    proc = subprocess.run(
        [
            SSHPASS_BIN,
            "-e",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(_ssh_debug_host_port()),
            f"{SSH_ADDON_USER}@127.0.0.1",
            command,
        ],
        input=data,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "SSHPASS": SSH_ADDON_PASSWORD},
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(
            f"{command!r} failed: {proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout.decode()


def supervisor_data(subpath: str, data: bytes) -> None:
    """Untar ``data`` into Supervisor's data dir (``/mnt/data/supervisor``)."""
    target = shlex.quote(f"/data/{subpath}")
    haos_shell(
        f"docker exec -i hassio_supervisor sh -c 'rm -rf {target} && mkdir -p "
        f"$(dirname {target}) && tar -x -C $(dirname {target})'",
        data,
    )


def tar_dir(path: Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(path, arcname=arcname)
    return buf.getvalue()


# Copied from the app-source staging in haos_runtime.refresh_dev_addon_source_in_qcow2,
# which writes into an offline qcow2 instead of a running VM.
def build_dev_addon_source_tar(workdir: Path, sha: str) -> Path:
    """Build ``<workdir>/ha_mcp_dev.tar``: the dev addon's build context, version-bumped.

    The bumped ``version:`` is ``<base>-pr-<sha>`` so Supervisor sees a new
    version for every distinct commit and rebuilds the addon image.
    """
    import shutil as _shutil

    repo_root = SRC
    dev_addon_src = repo_root / "homeassistant-addon-dev"
    if not dev_addon_src.exists():
        raise RuntimeError(
            f"homeassistant-addon-dev not found at {dev_addon_src} — "
            f"checkout is incomplete; inaddon tier cannot refresh source."
        )

    staging = workdir / "ha_mcp_dev"
    _shutil.copytree(dev_addon_src, staging)

    # Same file-shaping as build_image.stage_dev_addon_source so the
    # build context matches what the cached Docker layers expect.
    _shutil.copy(
        repo_root / "homeassistant-addon" / "start.py",
        staging / "start.py",
    )
    for name in DEV_ADDON_REPO_FILES:
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy(repo_root / name, staging / name)
    addon_src_dir = staging / "src"
    if addon_src_dir.exists():
        _shutil.rmtree(addon_src_dir)
    addon_src_dir.mkdir()
    _shutil.copytree(repo_root / "src" / "ha_mcp", addon_src_dir / "ha_mcp")

    # Dockerfile shape fixup (same as bake).
    dockerfile = staging / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "COPY homeassistant-addon/start.py /",
            "COPY start.py /",
        )
    )

    # Strip image: from config.yaml — Supervisor pulls from GHCR when
    # image: is set, but the per-PR version we bump to below doesn't
    # exist there. Force local Dockerfile build by removing the field.
    # Same fix the bake's stage_dev_addon_source applies.
    config_path_pre = staging / "config.yaml"
    config_path_pre.write_text(
        "".join(
            ln
            for ln in config_path_pre.read_text().splitlines(keepends=True)
            if not ln.startswith("image:")
        )
    )

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

    seed_tar = workdir / "ha_mcp_dev.tar"
    subprocess.run(
        [
            "tar",
            "--numeric-owner",
            "--owner=0",
            "--group=0",
            "-C",
            str(workdir),
            "-cf",
            str(seed_tar),
            "ha_mcp_dev",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return seed_tar


class Instance:
    def __init__(self, env: dict) -> None:
        self.env = env
        self.backend = env["backend"]
        self.haos = self.backend.startswith("haos")
        self.base_url = env["base_url"]
        self.headers = {"Authorization": f"Bearer {env['token']}"}
        self.server: subprocess.Popen | None = None

    @property
    def standalone(self) -> bool:
        return self.backend in ("container", "haos")

    @property
    def embedded(self) -> bool:
        return self.backend in ("embedded", "haos_embedded")

    def targets(self) -> dict[str, str]:
        if self.standalone:
            mcp = f"http://127.0.0.1:{os.environ['MCP_PORT']}"
            path = os.environ["MCP_SECRET_PATH"]
        elif self.embedded:
            mcp, _, rest = self.env["embedded_webhook_url"].partition("/api/webhook/")
            path = f"/api/webhook/{rest}"
        else:
            url = self.env["addon_mcp_url"]
            mcp = url[: url.index("/", len("http://"))]
            path = url[len(mcp) :]
        return {"ha": self.base_url, "mcp": mcp, "mcp_path": path}

    def start_server(self) -> None:
        if not self.standalone:
            return
        if self.server:
            self.server.terminate()
            self.server.wait(30)
        env = os.environ | {
            "HOMEASSISTANT_URL": self.base_url,
            "HOMEASSISTANT_TOKEN": self.env["token"],
            "MCP_HOST": "127.0.0.1",
            "ENABLE_STRICT_MANDATORY_BPS": str(STRICT_BPS).lower(),
        }
        # The child keeps its own copy of the log handle.
        with open(SRC.parent / "server.log", "ab") as out:
            self.server = subprocess.Popen(
                ["uv", "run", "ha-mcp-web"], cwd=SRC, env=env, stdout=out, stderr=out
            )

    def update_component(self) -> None:
        if self.haos:
            supervisor_data(
                f"homeassistant/{COMPONENT.as_posix()}",
                tar_dir(SRC / COMPONENT, COMPONENT.name),
            )
            try:
                requests.post(
                    f"{self.base_url}/api/services/homeassistant/restart",
                    headers=self.headers,
                    timeout=30,
                )
            except requests.ConnectionError:
                pass  # Core can drop the connection as it goes down.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    requests.get(
                        f"{self.base_url}/api/", headers=self.headers, timeout=5
                    )
                except requests.RequestException:
                    break
                time.sleep(2)
        else:
            config = Path(self.env["config_path"])
            dest = config / COMPONENT
            # HA runs as root in the container, so files it wrote here
            # (__pycache__) are root-owned; the runner's user has passwordless sudo.
            subprocess.run(["sudo", "rm", "-rf", str(dest)], check=True)
            shutil.copytree(SRC / COMPONENT, dest)
            subprocess.run(["sudo", "chmod", "-R", "a+rwX", str(config)], check=True)
            self.env["container"].get_wrapped_container().restart(timeout=30)
        if not _wait_for_ha_api_ready(self.base_url, self.headers, timeout=600):
            raise RuntimeError("Home Assistant did not come back after the restart")

    def update_server(self, sha: str) -> None:
        if self.standalone:
            self.start_server()
            self.wait_server()
        elif self.embedded:
            with tempfile.TemporaryDirectory() as tmp:
                wheel = _build_embedded_server_wheel(Path(tmp))
                # A new path per attempt: an unchanged pip_spec skips the reinstall.
                tag = f"{sha[:12]}-{int(time.time())}"
                rel = f"devenv/{tag}/{wheel.name}"
                if self.haos:
                    supervisor_data(
                        f"homeassistant/devenv/{tag}", tar_dir(wheel.parent, tag)
                    )
                else:
                    dest = Path(self.env["config_path"]) / rel
                    subprocess.run(
                        ["sudo", "mkdir", "-p", "-m", "777", str(dest.parent)],
                        check=True,
                    )
                    shutil.copy(wheel, dest)
            _home_assistant_ws_command(
                self.base_url,
                self.env["token"],
                {
                    "type": f"{HA_MCP_SERVER_DOMAIN}/server_entry_update",
                    "pip_spec": f"ha-mcp @ file:///config/{rel}",
                },
            )
            time.sleep(5)
            self.wait_server()
        else:
            with tempfile.TemporaryDirectory() as tmp:
                tar = build_dev_addon_source_tar(Path(tmp), sha[:7])
                store = haos_shell(
                    "docker exec hassio_supervisor sh -c "
                    "'test -d /data/apps/local && echo apps || echo addons'"
                ).strip()
                supervisor_data(f"{store}/local/ha_mcp_dev", tar.read_bytes())
            trigger_dev_addon_update(self.base_url, self.env["token"], timeout=900)
            self.wait_server()

    def wait_server(self) -> None:
        if self.standalone:
            target = self.targets()
            if not _wait_for_embedded_webhook_ready(
                target["mcp"] + target["mcp_path"], timeout=120
            ):
                raise RuntimeError("standalone server did not answer; see server.log")
        if self.embedded and not _wait_for_embedded_webhook_ready(
            self.env["embedded_webhook_url"], timeout=300
        ):
            raise RuntimeError("embedded server did not come back")
        if self.backend == "haos_inaddon":
            wait_for_addon_mcp_ready(timeout=300)


def changed_kinds(inst: Instance, changed: list[str]) -> set[str]:
    """Which updates a commit's changed paths call for."""
    server_paths = APP_PATHS if inst.backend == "haos_inaddon" else SERVER_PATHS
    kinds = set()
    if any(p.startswith(server_paths) for p in changed):
        kinds.add("server")
    if any(p.startswith(f"{COMPONENT.as_posix()}/") for p in changed):
        kinds.add("component")
    return kinds


def apply_updates(inst: Instance, sha: str, pending: dict[str, int]) -> dict[str, str]:
    """Run the pending updates, server first; failures stay pending up to MAX_TRIES."""
    errors = {}
    for name in ("server", "component"):
        if name not in pending:
            continue
        if name == "component" and not (
            inst.haos or (Path(inst.env["config_path"]) / COMPONENT).exists()
        ):
            del pending[name]
            continue
        try:
            if name == "server":
                inst.update_server(sha)
            else:
                inst.update_component()
            del pending[name]
        except Exception as err:  # noqa: BLE001
            # Keep the instance up and apply the other update anyway.
            errors[name] = f"{type(err).__name__}: {err}"
            pending[name] += 1
            if pending[name] >= MAX_TRIES:
                del pending[name]
    return errors


def test_hold_dev_ha_env(ha_container_with_fresh_config: dict[str, Any]) -> None:
    inst = Instance(dict(ha_container_with_fresh_config))
    inst.start_server()
    Path(os.environ["DEVENV_TARGETS"]).write_text(json.dumps(inst.targets()))
    sha = git("rev-parse", "HEAD")
    # Update kind -> failed attempts, retried on each poll up to MAX_TRIES.
    pending: dict[str, int] = {}
    if inst.haos and os.environ.get("DEVENV_COMPONENT_STALE") == "1":
        # The restored image was baked from another commit's component (a
        # prefix cache hit): apply the branch's copy as a push would, so a
        # component-only change costs a Core restart, not an image rebuild.
        # A failure stays pending, so the poll loop retries it and the
        # instance is not reported ready with the wrong component.
        pending["component"] = 0
        log(sha=sha, backend=inst.backend, ready=False, updating=["component"])
        errors = apply_updates(inst, sha, pending)
        if errors:
            log(sha=sha, backend=inst.backend, ready=False, errors=errors)
    try:
        inst.wait_server()
        log(sha=sha, backend=inst.backend, ready=not pending)
    except Exception as err:  # noqa: BLE001
        # HA is up; a push that fixes the server is picked up below.
        log(sha=sha, backend=inst.backend, ready=False, error=str(err))
    end = time.monotonic() + 60 * int(os.environ.get("DEVENV_MINUTES", "340"))
    while time.monotonic() < end:
        time.sleep(POLL_S)
        try:
            git("fetch", "-q", "origin", TRACK_REF)
            head = git("rev-parse", "FETCH_HEAD")
            if head != sha:
                changed = git("diff", "--name-only", sha, head).splitlines()
                git("checkout", "-q", "-f", head)
                git("submodule", "update", "-q", "--init", "--recursive")
                sha = head
                pending = dict.fromkeys(set(pending) | changed_kinds(inst, changed), 0)
                if not pending:
                    log(sha=sha, ready=True)
        except subprocess.CalledProcessError as err:
            log(sha=sha, error=f"git: {err}")
            continue
        if not pending:
            continue
        log(sha=sha, ready=False, updating=sorted(pending))
        errors = apply_updates(inst, sha, pending)
        log(sha=sha, ready=not errors, **({"errors": errors} if errors else {}))
