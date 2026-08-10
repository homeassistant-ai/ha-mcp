"""Unit tests for the ``rich``-version compatibility shim (#2186).

``fastmcp.utilities.logging.configure_logging`` constructs a
``rich.logging.RichHandler`` with ``tracebacks_max_frames`` and
``tracebacks_suppress`` unconditionally, and ``fastmcp/__init__.py`` calls it
at *import* time. Those two keyword arguments only exist on ``rich>=13.9.4``
(``fastmcp-slim``'s declared floor), so on an environment that resolves an
older ``rich`` the very first ``import fastmcp`` dies with::

    TypeError: RichHandler.__init__() got an unexpected keyword argument
    'tracebacks_max_frames'

That is fatal for ha-mcp: ``ha_mcp/__init__.py`` imports ``.auth``, which
imports ``fastmcp``, so *nothing* in the package can be imported. It is
reachable in practice — the official Home Assistant container ships
``rich 10.16.2`` (held below 11 by ``surepy``'s ``rich>=10.1.0,<11.0.0``),
and ``rich`` is absent from HA's ``package_constraints.txt``, so whether the
runtime install of ``ha-mcp`` drags ``rich`` up is left to the resolver.

``ha_mcp._rich_compat`` filters keyword arguments the installed
``RichHandler`` cannot accept, and only when it cannot accept them. The
tracebacks lose frame-limiting and framework-frame suppression on such
installs; they render.
"""

from __future__ import annotations

import inspect
import logging
import subprocess
import sys
import textwrap

import pytest

from ha_mcp._rich_compat import ensure_rich_handler_compat

# The keyword arguments fastmcp passes that predate rich 13.9.4.
_MODERN_ONLY_KWARGS = ("tracebacks_max_frames", "tracebacks_suppress")


class _OldRichHandler(logging.Handler):
    """Stand-in for ``rich<13.9.4``'s ``RichHandler``.

    Accepts the keyword arguments that old rich understood and nothing else,
    so passing a modern-only kwarg raises ``TypeError`` exactly as the real
    old class does.
    """

    def __init__(
        self,
        level: int = logging.NOTSET,
        console: object = None,
        *,
        show_path: bool = True,
        show_level: bool = True,
        rich_tracebacks: bool = False,
    ) -> None:
        super().__init__(level=level)
        self.console = console
        self.show_path = show_path
        self.show_level = show_level
        self.rich_tracebacks = rich_tracebacks


class _ModernRichHandler(_OldRichHandler):
    """Stand-in for ``rich>=13.9.4`` — understands the newer kwargs."""

    def __init__(
        self,
        level: int = logging.NOTSET,
        console: object = None,
        *,
        show_path: bool = True,
        show_level: bool = True,
        rich_tracebacks: bool = False,
        tracebacks_max_frames: int = 100,
        tracebacks_suppress: object = (),
    ) -> None:
        super().__init__(
            level=level,
            console=console,
            show_path=show_path,
            show_level=show_level,
            rich_tracebacks=rich_tracebacks,
        )
        self.tracebacks_max_frames = tracebacks_max_frames
        self.tracebacks_suppress = tracebacks_suppress


@pytest.fixture
def patched_rich(monkeypatch):
    """Swap ``rich.logging.RichHandler`` for a stub, restored on teardown."""

    def _install(handler_cls: type) -> type:
        import rich.logging

        # Subclass per test so the shim's mutation never leaks between tests.
        stub = type(handler_cls.__name__, (handler_cls,), {})
        monkeypatch.setattr(rich.logging, "RichHandler", stub)
        return stub

    return _install


def test_old_rich_drops_unsupported_kwargs(patched_rich):
    """The shim makes fastmcp's exact call survive on pre-13.9.4 rich."""
    stub = patched_rich(_OldRichHandler)

    # Without the shim this is the reported crash.
    with pytest.raises(TypeError, match="tracebacks_max_frames"):
        stub(rich_tracebacks=True, tracebacks_max_frames=3, tracebacks_suppress=[])

    assert ensure_rich_handler_compat() is True

    handler = stub(
        show_path=False,
        show_level=False,
        rich_tracebacks=True,
        tracebacks_max_frames=3,
        tracebacks_suppress=[object()],
    )

    # Unsupported kwargs dropped, supported ones still applied.
    assert handler.rich_tracebacks is True
    assert handler.show_path is False
    assert handler.show_level is False
    for kwarg in _MODERN_ONLY_KWARGS:
        assert not hasattr(handler, kwarg)


def test_modern_rich_is_left_alone(patched_rich):
    """On rich>=13.9.4 the shim must not touch ``RichHandler``."""
    stub = patched_rich(_ModernRichHandler)
    original_init = stub.__init__

    assert ensure_rich_handler_compat() is False
    assert stub.__init__ is original_init

    handler = stub(rich_tracebacks=True, tracebacks_max_frames=3)
    assert handler.tracebacks_max_frames == 3


def test_shim_is_idempotent(patched_rich):
    """Repeated calls must not stack wrappers around ``__init__``."""
    stub = patched_rich(_OldRichHandler)

    assert ensure_rich_handler_compat() is True
    after_first = stub.__init__

    assert ensure_rich_handler_compat() is True
    assert stub.__init__ is after_first


def test_missing_rich_is_not_fatal(monkeypatch):
    """A ``rich`` that cannot be imported must not break ``import ha_mcp``."""
    monkeypatch.setitem(sys.modules, "rich.logging", None)
    assert ensure_rich_handler_compat() is False


def test_importing_ha_mcp_survives_old_rich():
    """End-to-end: ``import ha_mcp`` works when rich predates the kwargs.

    This is the regression the issue actually reports, and it also pins the
    ordering requirement — the shim has to run before anything imports
    ``fastmcp``, which happens via ``ha_mcp/__init__.py`` -> ``.auth``.
    """
    program = textwrap.dedent(
        """
        import logging
        import rich.logging

        class OldRichHandler(logging.Handler):
            def __init__(self, level=logging.NOTSET, console=None, *,
                         show_path=True, show_level=True, rich_tracebacks=False):
                super().__init__(level=level)

        # Simulate rich<13.9.4 before anything imports fastmcp.
        rich.logging.RichHandler = OldRichHandler

        import ha_mcp
        print("IMPORT_OK", ha_mcp.__version__)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "IMPORT_OK" in result.stdout, (
        f"import ha_mcp failed under an old rich\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_shim_signature_is_preserved(patched_rich):
    """The wrapper keeps ``__init__`` introspectable for other callers."""
    stub = patched_rich(_OldRichHandler)
    ensure_rich_handler_compat()

    params = inspect.signature(stub.__init__).parameters
    assert "rich_tracebacks" in params
