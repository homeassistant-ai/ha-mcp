"""Hold a live HA test instance up for dev-ha-env.yml at the dispatched commit.

The workflow copies this file into the tracked checkout's tests/src/e2e/ and
runs it with pytest, so the E2E session fixture does the whole bring-up for the
selected backend: the Docker container (standalone or embedded server) or HAOS
(external, embedded or app). It writes the local targets to tunnel to
DEVENV_TARGETS and keeps the instance up. The standalone and embedded servers
run in developer mode, so a branch is iterated on with the same tools as on any
live HA: ha_dev_manage_server (update_source) for the embedded server, HACS for
the component, ha_restart. The app is changed by a new run.

Status lines (STATUS {...}) go to the job log.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

import requests
from haos_runtime import (
    HA_MCP_SERVER_DOMAIN,
    SSH_ADDON_PASSWORD,
    SSH_ADDON_USER,
    SSHPASS_BIN,
    _ssh_debug_host_port,
    wait_for_addon_mcp_ready,
)

from . import _conftest_embedded, _conftest_haos, _conftest_testcontainer
from ._conftest_embedded import _wait_for_embedded_webhook_ready
from ._conftest_readiness import _wait_for_ha_api_ready

SRC = Path(__file__).resolve().parents[3]
COMPONENT = Path("custom_components") / HA_MCP_SERVER_DOMAIN
# A stale component applied at boot is retried this many times.
MAX_TRIES = 3

# Agents meet strict best-practices mode by default; the E2E suite pins it off.
STRICT_BPS = os.environ.get("DEVENV_STRICT_BPS", "true") == "true"
_conftest_embedded._EMBEDDED_FEATURE_FLAGS["enable_strict_mandatory_bps"] = STRICT_BPS
# Developer mode exposes ha_dev_manage_server, the tool that updates the
# embedded server in place, as on a developer's own instance.
_conftest_embedded._EMBEDDED_FEATURE_FLAGS["enable_dev_mode"] = True


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(SRC), *args], text=True).strip()


def log(**status: object) -> None:
    print("STATUS", json.dumps(status), flush=True)


def _boot_gate(url: str, timeout: int, **kwargs: Any) -> bool:
    """The session fixture's embedded-server gate, made non-fatal.

    A server that never comes up must not take Home Assistant down with it:
    the run reports it and the instance stays up for the fix.
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
        env = os.environ | {
            "HOMEASSISTANT_URL": self.base_url,
            "HOMEASSISTANT_TOKEN": self.env["token"],
            "MCP_HOST": "127.0.0.1",
            "ENABLE_STRICT_MANDATORY_BPS": str(STRICT_BPS).lower(),
            "HAMCP_ENABLE_DEV_MODE": "true",
        }
        # The child keeps its own copy of the log handle.
        with open(SRC.parent / "server.log", "ab") as out:
            self.server = subprocess.Popen(
                ["uv", "run", "ha-mcp-web"], cwd=SRC, env=env, stdout=out, stderr=out
            )

    def update_component(self) -> None:
        """Replace the HAOS component with the branch's, as HACS does, and
        restart Core."""
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
                requests.get(f"{self.base_url}/api/", headers=self.headers, timeout=5)
            except requests.RequestException:
                break
            time.sleep(2)
        if not _wait_for_ha_api_ready(self.base_url, self.headers, timeout=600):
            raise RuntimeError("Home Assistant did not come back after the restart")
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


def apply_stale_component(inst: Instance, sha: str) -> None:
    """Apply the branch's component to a HAOS image baked from another commit.

    A prefix cache hit restores an image whose component came from another
    commit, so a component-only change costs a Core restart, not an image
    rebuild. The instance is not reported ready with the wrong component.
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_TRIES + 1):
        log(sha=sha, backend=inst.backend, ready=False, updating=["component"])
        try:
            inst.update_component()
            return
        # Transient: HTTP, the SSH copy, timeouts, and the RuntimeError that
        # haos_shell and the readiness checks raise. Bugs propagate.
        except (
            requests.RequestException,
            subprocess.SubprocessError,
            TimeoutError,
            RuntimeError,
        ) as err:
            last_err = err
            log(
                sha=sha,
                ready=False,
                attempt=attempt,
                error=f"{type(err).__name__}: {err}",
            )
    raise RuntimeError("the branch's component could not be applied") from last_err


def test_hold_dev_ha_env(ha_container_with_fresh_config: dict[str, Any]) -> None:
    inst = Instance(dict(ha_container_with_fresh_config))
    inst.start_server()
    Path(os.environ["DEVENV_TARGETS"]).write_text(json.dumps(inst.targets()))
    sha = git("rev-parse", "HEAD")
    if inst.haos and os.environ.get("DEVENV_COMPONENT_STALE") == "1":
        apply_stale_component(inst, sha)
    try:
        inst.wait_server()
        log(sha=sha, backend=inst.backend, ready=True)
    except Exception as err:  # noqa: BLE001
        # HA is up; fix the server through the tools, as on any instance.
        log(sha=sha, backend=inst.backend, ready=False, error=str(err))
    time.sleep(60 * int(os.environ.get("DEVENV_MINUTES", "340")))
