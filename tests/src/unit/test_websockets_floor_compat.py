"""Pin the ``websockets`` API surface ha-mcp actually uses.

The dependency is declared as a range (``websockets>=15.0.1,<18`` — HA core's
own ``package_constraints`` floor) so the in-process embedded install can run
on whatever ``websockets`` the Home Assistant image already ships instead of
force-replacing it in place (issues #2135/#2146: an interrupted in-place
replacement leaves a torn half-old/half-new package that kills every WS
connect with ImportError).

A range is only honest if both ends are proven: the lockfile exercises the top
of the range everywhere, and the unit-tests workflow re-runs this module (plus
the websockets-facing client tests) with the floor version overlaid. These
tests therefore assert against whatever ``websockets`` is installed — they
pass at 15.0.1 and at the top of the range, and fail loudly if code starts
using an API the floor lacks (add the API here and raise the floor
deliberately, in ``pyproject.toml``, when that happens).
"""

import inspect

import websockets


class TestFloorApiSurface:
    """Every websockets API used in ``src/`` exists in the installed version."""

    def test_top_level_connect_and_connection_exist(self):
        # websocket_client.py / tools_addons.py: ``websockets.connect(...)``,
        # ``websockets.ClientConnection`` (lazy top-level exports).
        assert callable(websockets.connect)
        assert isinstance(websockets.ClientConnection, type)

    def test_asyncio_client_module_exports_client_connection(self):
        # tools_addons.py imports it from the implementation module directly.
        from websockets.asyncio.client import ClientConnection

        assert isinstance(ClientConnection, type)

    def test_connect_accepts_every_kwarg_we_pass(self):
        # The union of kwargs used at websocket_client.py::connect and
        # tools_addons.py::_collect_ws_messages call sites.
        used_kwargs = {
            "ping_interval",
            "ping_timeout",
            "additional_headers",
            "ssl",
            "max_size",
            "open_timeout",
            "close_timeout",
        }
        params = inspect.signature(websockets.connect).parameters
        param_names = set(params)
        accepts_var_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        missing = used_kwargs - param_names
        assert accepts_var_kwargs or not missing, (
            f"websockets.connect ({websockets.__version__}) does not accept "
            f"kwargs used by ha-mcp: {sorted(missing)}"
        )

    def test_exception_types_we_catch_exist(self):
        # rest_client.py: WebSocketException; websocket_client.py:
        # ConnectionClosed; tools_addons.py: ConnectionClosed,
        # InvalidHandshake, InvalidStatus.
        from websockets.exceptions import (
            ConnectionClosed,
            InvalidHandshake,
            InvalidStatus,
            WebSocketException,
        )

        assert issubclass(ConnectionClosed, WebSocketException)
        assert issubclass(InvalidStatus, InvalidHandshake)

    def test_import_chain_behind_connect_is_healthy(self):
        # The exact chain a torn install breaks (#2135/#2146): resolving the
        # lazy ``connect`` export imports the client implementation, which
        # imports the version-sensitive exception names.
        import websockets.asyncio.client
        import websockets.client
        import websockets.http11  # noqa: F401
