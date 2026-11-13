"""Every test here runs once per flavor, against real Home Assistant Core.

The component under test is the flavor's own ``mcp_proxy``/``mcp_proxy_dev``
directory, made importable as a custom integration so Core loads and sets it
up the way the app (add-on) installs it into ``/config/custom_components``.
Its ``/config`` and ``/data`` files are redirected into the test's temporary
directory.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from aiohttp import web

from ._proxy import STATE_ROOTS, Upstream, UpstreamRequest, state_path
from ._variants import VARIANTS, Variant


@pytest.fixture(autouse=True, params=VARIANTS, ids=lambda v: v.key)
def variant(request: pytest.FixtureRequest) -> Variant:
    flavor: Variant = request.param
    return flavor


@pytest.fixture(scope="session")
def _custom_components_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A ``custom_components`` directory holding both flavors' components.

    Their domains differ, so both sit side by side; ``custom_components`` is a
    namespace package, so this directory joins the repository's own.
    """
    root = tmp_path_factory.mktemp("proxy_components")
    package = root / "custom_components"
    package.mkdir()
    for flavor in VARIANTS:
        (package / flavor.component).symlink_to(
            flavor.component_dir, target_is_directory=True
        )
    return root


def _forget(package_name: str) -> None:
    for name in [
        m for m in sys.modules if m == package_name or m.startswith(f"{package_name}.")
    ]:
        del sys.modules[name]


@pytest.fixture
def fs_root(tmp_path: Path) -> Path:
    """Where the component's and start.py's ``/config`` and ``/data`` live."""
    root = tmp_path / "fs"
    for state_root in STATE_ROOTS:
        (root / state_root.lstrip("/")).mkdir(parents=True)
    return root


@pytest.fixture
def component(
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    _custom_components_root: Path,
) -> Iterator[ModuleType]:
    """The flavor's component package, with every submodule loaded.

    Every module-level ``/config`` or ``/data`` path is redirected under
    ``fs_root``, and the app's heartbeat is fresh, as while the app runs.
    """
    monkeypatch.syspath_prepend(str(_custom_components_root))
    package_name = f"custom_components.{variant.component}"
    _forget(package_name)
    package = importlib.import_module(package_name)
    modules = [package] + [
        importlib.import_module(f"{package_name}.{path.stem}")
        for path in sorted(variant.component_dir.glob("*.py"))
        if path.stem != "__init__"
    ]
    for module in modules:
        for attribute, value in list(vars(module).items()):
            if isinstance(value, Path) and str(value).startswith(STATE_ROOTS):
                monkeypatch.setattr(module, attribute, state_path(fs_root, value))
    heartbeat = vars(sys.modules[f"{package_name}.oauth"]).get("HEARTBEAT_FILE")
    if isinstance(heartbeat, Path):
        heartbeat.touch()
    yield package
    _forget(package_name)


@pytest.fixture
async def upstream(socket_enabled: None) -> AsyncIterator[Upstream]:
    state = Upstream(base_url="")

    async def dispatch(request: web.Request) -> web.StreamResponse:
        state.requests.append(
            UpstreamRequest(
                method=request.method,
                path=request.path,
                headers=dict(request.headers),
                body=await request.read(),
            )
        )
        if state.handler is not None:
            return await state.handler(request)
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    state.base_url = f"http://127.0.0.1:{port}"
    yield state
    await runner.cleanup()
