"""Spawn a real ha-mcp HTTP launcher (ha-mcp-web, ha-mcp-oauth) with its output drained."""

import asyncio
import os
from contextlib import suppress
from pathlib import Path

_LIFESPAN_STARTED_LOG_FRAGMENTS = (
    "Application startup complete",
    "Uvicorn running on",
)


def http_launcher_env(ha_url: str, config_dir: Path) -> dict[str, str]:
    """Env shared by the HTTP launcher lanes, minus each lane's credentials.

    Same shape as the stdio test's env (including the deliberate absence of
    HA_MCP_DISABLE_UPDATE_CHECK, which would return the nudge early) plus the
    HTTP knobs: loopback bind on an ephemeral port and a throwaway secret path.
    The nudge tests never connect to these servers, since the lifespan fires at
    uvicorn startup on its own; a test that does connect must poll the server
    first, because the lifespan signal precedes the socket bind.
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


class HttpLauncher:
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


async def spawn_http_launcher(command: str, env: dict[str, str]) -> HttpLauncher:
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
    return HttpLauncher(proc)
