"""The server's WebSocket client on a real ``hass``, and a command Core holds.

Shared by the transport tests. ``core_client`` already serves Core's
``websocket_api`` on a loopback port; these helpers open a dedicated
``HomeAssistantWebSocketClient`` on it, so a test can drop or close that one
connection without touching the pooled client the fixture tears down.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any

from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ha_mcp.client.rest_client import HomeAssistantClient

# ``execute_script`` answers once its script ends; this one waits for the gate.
GATE = "input_text.b11_gate"
HELD_SCRIPT: list[dict[str, Any]] = [
    {"wait_template": f"{{{{ is_state('{GATE}', 'open') }}}}"}
]


@asynccontextmanager
async def connected_ws(
    core_client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantWebSocketClient]:
    """A connected, authenticated client of its own on ``core_client``'s Core."""
    client = HomeAssistantWebSocketClient(
        core_client.base_url, core_client.token, verify_ssl=False
    )
    assert await client.connect() is True
    try:
        yield client
    finally:
        await client.disconnect()


def close_gate(hass: HomeAssistant) -> None:
    """Make ``HELD_SCRIPT`` wait until ``open_gate``."""
    hass.states.async_set(GATE, "closed")


async def open_gate(hass: HomeAssistant) -> None:
    """Let every held script finish, so no Core task outlives the test."""
    hass.states.async_set(GATE, "open")
    await hass.async_block_till_done(wait_background_tasks=True)


def abort_transport(client: HomeAssistantWebSocketClient) -> None:
    """Drop the TCP connection with no close frame, as a dead network does."""
    assert client.websocket is not None
    client.websocket.transport.abort()


@contextmanager
def answers_unread(client: HomeAssistantWebSocketClient) -> Iterator[None]:
    """Leave Core's answers unread meanwhile: the client gets no answer."""
    assert client.websocket is not None
    transport = client.websocket.transport
    transport.pause_reading()
    try:
        yield
    finally:
        if not transport.is_closing():
            transport.resume_reading()


async def until(predicate: Callable[[], bool], timeout: float = 5) -> None:
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(timeout):
        for _ in range(100_000):
            if predicate():
                return
            await asyncio.sleep(0.001)


class SpawnedTasks:
    """Tasks the client starts on its own, recorded through the task factory.

    The client keeps its background work private; the loop's public task
    factory is where a test can see it start, without reading the client.
    """

    def __init__(self) -> None:
        self.tasks: list[asyncio.Future[Any]] = []
        self.started = asyncio.Event()

    async def last(self) -> asyncio.Future[Any]:
        """The newest task the client started, once there is one."""
        async with asyncio.timeout(5):
            await self.started.wait()
        return self.tasks[-1]


@contextmanager
def spawned_by(
    client: HomeAssistantWebSocketClient,
    on_spawn: Callable[[], object] | None = None,
) -> Iterator[SpawnedTasks]:
    """Record every task whose coroutine is a method of ``client``.

    ``on_spawn`` runs once, as the first such task is created and before it
    takes its first step.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    spawned = SpawnedTasks()

    def factory(
        loop: asyncio.AbstractEventLoop,
        coro: Coroutine[Any, Any, Any],
        **kwargs: Any,
    ) -> asyncio.Future[Any]:
        # Read before the task exists: an eager task may finish at creation.
        frame = getattr(coro, "cr_frame", None)
        owned = frame is not None and frame.f_locals.get("self") is client
        task: asyncio.Future[Any]
        if previous is not None:
            task = previous(loop, coro, **kwargs)
        else:
            task = asyncio.Task(coro, loop=loop, **kwargs)
        if owned:
            if on_spawn is not None and not spawned.tasks:
                on_spawn()
            spawned.tasks.append(task)
            spawned.started.set()
        return task

    loop.set_task_factory(factory)
    try:
        yield spawned
    finally:
        loop.set_task_factory(previous)
