"""Launcher-lane regression test for the HACS startup nudge (the add-on gap).

The nudge that asks HACS to refresh the paired component's release data used to
be scheduled by ``__main__``'s ``_run_with_shutdown``, so any launcher that
calls ``mcp.run()`` directly — the add-on's ``start.py`` above all — never got
it. The fix moves the scheduling into the server's FastMCP ``lifespan``
(``hacs_refresh_lifespan`` in ``src/ha_mcp/hacs_auto_refresh.py``), which every
launcher runs because it is attached to the server itself, not to one entry
point.

The unit suite pins that wiring (the lifespan is attached, the task is created
and cancelled). What it cannot show is the runtime chain completing in a real
process, which is precisely what the add-on gap broke — and why CI stayed green
through it. So this test boots the real ``ha-mcp`` stdio binary (the same
``main()`` → ``run_async`` path real launchers hit) and watches for the
observable end of the chain: process start → lifespan → task → admin WebSocket
→ HACS repository list → marker file in the ha-mcp data dir.

Stdio stands in for every launcher here because the lifespan is transport-level:
one real launcher proving the chain runs end to end, plus the unit-level wiring
pin, covers the others. The e2e container ships HACS in ``custom_components``
but has neither candidate repository downloaded, so the pass completes on the
first attempt with no GitHub traffic and writes the marker immediately.

The lane matrix: stdio and ``ha-mcp-web`` are the POSITIVE lanes — the nudge
completes a pass and writes its marker (the HTTP lifespan fires at uvicorn
startup, so the web lane needs no client connection at all). ``ha-mcp-oauth``
is a NEGATIVE lane: OAuth mode holds no server-level HA credential, so
``maybe_refresh_hacs_after_update`` must return at the ``OAUTH_MODE_TOKEN``
gate instead of burning its whole retry schedule on auth failures every boot.
The embedded server is the other negative lane, pinned next to its own bring-up
in ``tests/src/e2e/workflows/embedded/test_embedded_server.py`` (the
``is_embedded`` gate). ``ha-mcp-oidc`` has no lane here: it validates HA
credentials at startup and exits without them (``_validate_standard_credentials``
in ``__main__``), so it has no sentinel state to prove, and its run path is the
same ``_run_with_shutdown(mcp.run_async(**_http_run_kwargs(...)))`` call the web
lane already drives. The HAOS add-on lane is covered directly in
``tests/src/e2e/haos_only/test_inaddon_startup_nudge.py``, which restarts the
real add-on and requires a per-boot nudge line from that restart; the unit suite
separately pins the shared lifespan wiring.
"""

import asyncio
import json
import logging
import os
from contextlib import suppress
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
from ha_mcp.hacs_auto_refresh import MARKER_FILENAME_PREFIX, RETRY_DELAYS
from ha_mcp.tools.hacs_registration import HACS_REFRESH_TIMEOUT

from ...conftest import TEST_TOKEN, _retire_stdio_sidecar, _stdio_env
from ...utilities.wait_helpers import wait_for_condition

logger = logging.getLogger(__name__)

# The nudge's first attempt is immediate, but a launcher can win the race
# against HA finishing HACS's setup — HACS registers its WS handlers late, and
# until it does the command comes back ``unknown_command``, which
# ``_refresh_with_retries`` cannot tell apart from "no HACS installed" and so
# keeps retrying. Budget for one such attempt failing the slowest way it can,
# all the way to the marker actually being written:
#   1. ``hacs/repositories/list`` hangs until ``DEFAULT_COMMAND_WAIT_TIMEOUT``
#   2. ``RETRY_DELAYS[0]`` of sleep before the pass tries again
#   3. the second list call may spend that same command timeout
#   4. ``send_hacs_repository_refresh`` then runs on its OWN, longer budget
#      (``HACS_REFRESH_TIMEOUT``) — the marker is written after it returns
# Waiting any less makes these lanes a coin flip on a slow runner: the marker
# lands just after the assert. Every term is derived from the schedule it
# waits on, never a literal, so raising any of them cannot silently outgrow
# the wait. It costs nothing on the happy path — wait_for_condition polls and
# returns the moment the marker appears.
NUDGE_MARKER_TIMEOUT = (
    DEFAULT_COMMAND_WAIT_TIMEOUT
    + RETRY_DELAYS[0]
    + DEFAULT_COMMAND_WAIT_TIMEOUT
    + HACS_REFRESH_TIMEOUT
    + 15.0
)


# HACS registers its WebSocket handlers late in HA's boot, and this fixture
# restarts HA with a fresh config before each test. Until those handlers exist
# ``hacs/repositories/list`` answers ``unknown_command`` FAST, so the nudge's
# attempts at 0 s, 30 s and 90 s can all fall inside that window on a slow
# runner — and the next one, at 210 s, lands after NUDGE_MARKER_TIMEOUT below.
# The budget below deliberately covers only the happy path plus ONE slow
# failure; it is not a HACS-boot timeout. So wait for HACS to be reachable
# BEFORE launching, from the test process, and the launcher's first attempt
# succeeds. Bound it by the nudge's own schedule: if HACS is not up by then the
# lane could never have passed anyway, and the message says which it was.
HACS_WS_READY_TIMEOUT = sum(RETRY_DELAYS) + DEFAULT_COMMAND_WAIT_TIMEOUT


async def _wait_for_hacs_ws_ready(container_info: dict) -> None:
    """Block until the container's HACS answers ``hacs/repositories/list``."""
    client = HomeAssistantWebSocketClient(
        container_info["base_url"], container_info.get("token", TEST_TOKEN)
    )
    # connect() opens the socket and starts its reader before it returns, and
    # a cancellation in between bypasses its own cleanup — so the finally must
    # already be armed when connect() runs; disconnect() tolerates a client
    # that never got that far.
    try:
        if not await client.connect():
            pytest.fail("could not open the HA WebSocket for the HACS probe")
        deadline = asyncio.get_running_loop().time() + HACS_WS_READY_TIMEOUT
        while True:
            try:
                await client.send_command("hacs/repositories/list")
                return
            except HomeAssistantCommandError as err:
                if not (
                    err.code == "unknown_command"
                    or "unknown command" in str(err).lower()
                ):
                    raise
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail(
                    "HACS never registered its WebSocket handlers within "
                    f"{HACS_WS_READY_TIMEOUT:.0f}s of the fresh-config restart, "
                    "so no launcher could complete the startup nudge here."
                )
            await asyncio.sleep(2.0)
    finally:
        await client.disconnect()


@pytest.mark.hacs
async def test_stdio_launcher_runs_the_startup_nudge(
    ha_container_with_fresh_config, tmp_path
):
    """A real ha-mcp subprocess must complete the nudge and write its marker."""
    logger.info("Testing the HACS startup nudge through the stdio launcher...")

    container_info = ha_container_with_fresh_config
    await _wait_for_hacs_ws_ready(container_info)

    # Use the same explicit environment as the stdio fixtures, plus this test's
    # config dir. HA_MCP_DISABLE_UPDATE_CHECK is intentionally absent because it
    # would return the nudge early.
    env = _stdio_env(container_info, tmp_path)
    transport = StdioTransport(command="ha-mcp", args=[], env=env, keep_alive=False)
    client = Client(transport)

    def marker_files():
        return list(tmp_path.glob(f"{MARKER_FILENAME_PREFIX}_*.json"))

    try:
        async with client:
            # Poll INSIDE the client context: leaving it terminates the subprocess,
            # and the lifespan cancels the nudge task on the way out.
            found = await wait_for_condition(
                marker_files,
                timeout=NUDGE_MARKER_TIMEOUT,
                condition_name="HACS refresh marker written by the stdio launcher",
            )
            markers = marker_files()
    finally:
        await _retire_stdio_sidecar(tmp_path)

    assert found, (
        "No HACS refresh marker appeared in the subprocess data dir within "
        f"{NUDGE_MARKER_TIMEOUT:.0f}s — the startup nudge never completed a "
        "pass. Either the server lifespan is not scheduling "
        "maybe_refresh_hacs_after_update, or every attempt inside that window "
        "failed (a log full of 'unknown_command' means HA had not registered "
        "HACS's WS handlers yet and the pass was still retrying). Data dir "
        f"contents: {sorted(p.name for p in tmp_path.iterdir())}"
    )
    assert len(markers) == 1, (
        "The nudge writes one marker per HA target and this run had exactly "
        f"one, got: {[p.name for p in markers]}"
    )

    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    logger.info(f"Marker written by the stdio launcher: {marker}")

    # A completed pass against this container must have SEEN HACS. "absent" is
    # written only after the retry schedule runs out, which would mean the
    # WebSocket reached HA but HACS never answered.
    assert marker["hacs"] == "present", (
        f"Marker reports hacs={marker['hacs']!r}, but the e2e container ships "
        "HACS in custom_components — the pass did not reach a loaded HACS."
    )
    # ``latest`` is intentionally not asserted: it is None or a version string
    # depending on whether PyPI is reachable from the runner, and both are fine.
    server_version = marker["server_version"]
    assert isinstance(server_version, str) and server_version, (
        "Marker must record the running server version so the next startup can "
        f"detect an update, got {server_version!r}"
    )

    logger.info("Stdio launcher startup nudge test passed")


# Uvicorn logs both of these only AFTER the ASGI lifespan's startup half has
# finished, so either line proves ``hacs_refresh_lifespan`` ran in the launcher's
# own process and created the nudge task. The negative lane below needs that
# proof: without it, "no marker appeared" could just mean the server never
# started.
_LIFESPAN_STARTED_LOG_FRAGMENTS = (
    "Application startup complete",
    "Uvicorn running on",
)

# Log fragments ``hacs_auto_refresh`` emits only once a pass has gone PAST the
# gates — the per-attempt failure, the give-up line, and the success line. None
# of them can come from a pass that returned at the OAuth sentinel gate.
_NUDGE_RAN_LOG_FRAGMENTS = (
    "HACS repository refresh attempt failed",
    "Could not reach HACS to refresh the component repository",
    "Asked HACS to refresh component repository info",
)


def _http_launcher_env(ha_url: str, config_dir: Path) -> dict[str, str]:
    """Env shared by the HTTP launcher lanes, minus each lane's credentials.

    Same shape as the stdio test's env (including the deliberate absence of
    HA_MCP_DISABLE_UPDATE_CHECK, which would return the nudge early) plus the
    HTTP knobs: loopback bind on an ephemeral port and a throwaway secret path,
    because nothing ever connects to these servers — the lifespan fires at
    uvicorn startup on its own.
    """
    return {
        "HOMEASSISTANT_URL": ha_url,
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "HA_MAX_RETRIES": "1",
        "ENABLE_STRICT_MANDATORY_BPS": "false",
        "HA_MCP_CONFIG_DIR": str(config_dir),
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": "0",
        "MCP_SECRET_PATH": "/e2e-nudge-probe",
    }


class _HttpLauncher:
    """A real ha-mcp HTTP launcher subprocess whose output is drained live.

    stdout and stderr are merged into one pipe that a background task keeps
    reading. That is not just for diagnostics: a launcher whose pipe buffer
    filled would block inside its own log write, which could stall it before or
    inside the lifespan and make both assertions below meaningless.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._lines: list[str] = []
        self._lifespan_started = asyncio.Event()
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        assert self._proc.stdout is not None
        buffer = ""
        while True:
            # Chunked reads rather than ``readline()``: a line longer than the
            # stream reader's limit makes ``readline`` raise instead of
            # returning, and a server banner is not a size we control.
            chunk = await self._proc.stdout.read(4096)
            if not chunk:
                if buffer:
                    self._record(buffer)
                return
            buffer += chunk.decode("utf-8", errors="replace")
            *complete, buffer = buffer.split("\n")
            for line in complete:
                self._record(line)

    def _record(self, line: str) -> None:
        self._lines.append(line)
        if any(f in line for f in _LIFESPAN_STARTED_LOG_FRAGMENTS):
            self._lifespan_started.set()

    async def wait_for_lifespan_started(self, timeout: float = 30) -> bool:
        """True once the launcher reports a completed ASGI lifespan startup.

        Raced against the process exiting so a launcher that died on a config
        error reports back immediately instead of costing the whole timeout.
        """
        started = asyncio.ensure_future(self._lifespan_started.wait())
        exited = asyncio.ensure_future(self._proc.wait())
        try:
            done, _pending = await asyncio.wait(
                {started, exited},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (started, exited):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        return started in done

    def lines_matching(self, fragments: tuple[str, ...]) -> list[str]:
        return [line for line in self._lines if any(f in line for f in fragments)]

    def output(self, limit: int = 40) -> str:
        """The tail of the launcher's merged output, for failure messages."""
        return "\n".join(self._lines[-limit:]) or "<no output captured>"

    async def aclose(self) -> None:
        """Stop the launcher and its drain — never leave an orphaned process."""
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._drain_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._drain_task


async def _spawn_http_launcher(command: str, env: dict[str, str]) -> _HttpLauncher:
    """Spawn ``command`` (an ha-mcp HTTP console script) with a drained pipe.

    Not StdioTransport: these entry points speak HTTP, not MCP-over-stdio, so
    the test drives the process directly.
    """
    proc = await asyncio.create_subprocess_exec(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    return _HttpLauncher(proc)


@pytest.mark.hacs
async def test_web_launcher_runs_the_startup_nudge(
    ha_container_with_fresh_config, tmp_path
):
    """A real ha-mcp-web subprocess must complete the nudge with no client."""
    logger.info("Testing the HACS startup nudge through the ha-mcp-web launcher...")

    container_info = ha_container_with_fresh_config
    await _wait_for_hacs_ws_ready(container_info)
    env = _http_launcher_env(container_info["base_url"], tmp_path)
    env["HOMEASSISTANT_TOKEN"] = container_info.get("token", TEST_TOKEN)

    def marker_files():
        return list(tmp_path.glob(f"{MARKER_FILENAME_PREFIX}_*.json"))

    launcher = await _spawn_http_launcher("ha-mcp-web", env)
    try:
        # The marker IS the readiness signal: it can only be written after
        # uvicorn ran the lifespan and the nudge completed a pass. Nothing
        # connects to the HTTP endpoint at any point in this test.
        found = await wait_for_condition(
            marker_files,
            timeout=NUDGE_MARKER_TIMEOUT,
            condition_name="HACS refresh marker written by the web launcher",
        )
        markers = marker_files()
        output = launcher.output()
    finally:
        await launcher.aclose()

    assert found, (
        "No HACS refresh marker appeared in the web launcher's data dir within "
        f"{NUDGE_MARKER_TIMEOUT:.0f}s — either the HTTP lifespan is not "
        "scheduling maybe_refresh_hacs_after_update, or every attempt inside "
        "that window failed ('unknown_command' in the output means HA had not "
        "registered HACS's WS handlers yet and the pass was still retrying). "
        f"Data dir contents: {sorted(p.name for p in tmp_path.iterdir())}. "
        f"Launcher output:\n{output}"
    )
    assert len(markers) == 1, (
        "The nudge writes one marker per HA target and this run had exactly "
        f"one, got: {[p.name for p in markers]}"
    )

    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    logger.info(f"Marker written by the web launcher: {marker}")

    assert marker["hacs"] == "present", (
        f"Marker reports hacs={marker['hacs']!r}, but the e2e container ships "
        "HACS in custom_components — the pass did not reach a loaded HACS."
    )
    server_version = marker["server_version"]
    assert isinstance(server_version, str) and server_version, (
        "Marker must record the running server version so the next startup can "
        f"detect an update, got {server_version!r}"
    )

    logger.info("Web launcher startup nudge test passed")


@pytest.mark.hacs
async def test_oauth_launcher_skips_the_startup_nudge(
    ha_container_with_fresh_config, tmp_path
):
    """A real ha-mcp-oauth subprocess must return at the OAuth sentinel gate.

    Per-user OAuth mode holds no server-level HA credential, so
    ``maybe_refresh_hacs_after_update`` returns before touching the WebSocket.
    A regression that dropped the gate would nudge with the sentinel token and
    burn the whole ~8-minute retry schedule on auth failures on every single
    boot — and it would still write no marker, so marker absence alone cannot
    detect it. Hence the two assertions: nothing that only a past-the-gate pass
    logs, and no marker.
    """
    logger.info("Testing the HACS startup nudge gate on the ha-mcp-oauth launcher...")

    container_info = ha_container_with_fresh_config
    env = _http_launcher_env(container_info["base_url"], tmp_path)
    # No HOMEASSISTANT_TOKEN: main_oauth substitutes OAUTH_MODE_TOKEN for it,
    # which is exactly the state under test. MCP_BASE_URL is only echoed into
    # the OAuth metadata URLs — HomeAssistantOAuthProvider never fetches it —
    # so a reserved .invalid host keeps that side of the run entirely offline.
    env["MCP_BASE_URL"] = "https://e2e-oauth-probe.invalid"
    # The nudge's own attempt/give-up lines are DEBUG; without this the
    # past-the-gate assertion below could not see them.
    env["LOG_LEVEL"] = "DEBUG"

    def marker_files():
        return list(tmp_path.glob(f"{MARKER_FILENAME_PREFIX}_*.json"))

    launcher = await _spawn_http_launcher("ha-mcp-oauth", env)
    try:
        started = await launcher.wait_for_lifespan_started(timeout=30)
        assert started, (
            "The ha-mcp-oauth launcher never reported a completed ASGI lifespan "
            "startup within 30s, so the assertions below would prove nothing "
            f"about the gate. Launcher output:\n{launcher.output()}"
        )
        # 10 s window as belt-and-braces only: a regressed gate with sentinel
        # credentials fails auth and never writes a marker at ANY timeout,
        # which is why the log assertion below — not marker absence — is the
        # deterministic proof for this lane.
        appeared = await wait_for_condition(
            marker_files,
            timeout=10,
            condition_name="(not expected) HACS refresh marker from the oauth launcher",
        )
        markers = marker_files()
        ran = launcher.lines_matching(_NUDGE_RAN_LOG_FRAGMENTS)
        output = launcher.output()
    finally:
        await launcher.aclose()

    assert not ran, (
        "The oauth launcher logged a HACS refresh pass, so the nudge ran past "
        "the OAUTH_MODE_TOKEN gate with sentinel credentials — every boot would "
        "spend its retry schedule failing auth. Lines:\n" + "\n".join(ran)
    )
    assert not appeared, (
        "The oauth launcher wrote a HACS refresh marker "
        f"({[p.name for p in markers]}), but OAuth mode has no server-level HA "
        "credential — the nudge must return at the OAUTH_MODE_TOKEN gate. "
        f"Launcher output:\n{output}"
    )

    logger.info("OAuth launcher startup nudge gate test passed")
