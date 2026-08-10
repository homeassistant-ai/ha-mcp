"""Tolerate a ``rich`` older than fastmcp's logging setup expects (#2186).

``fastmcp/__init__.py`` calls ``configure_logging()`` at import time, which
builds a ``rich.logging.RichHandler`` with ``tracebacks_max_frames`` and
``tracebacks_suppress`` unconditionally — no version guard, no ``try``. Both
keywords require ``rich>=13.9.4`` (``fastmcp-slim``'s declared floor), so an
environment holding an older ``rich`` cannot ``import fastmcp`` at all::

    TypeError: RichHandler.__init__() got an unexpected keyword argument
    'tracebacks_max_frames'

Since ``ha_mcp/__init__.py`` imports ``.auth`` -> ``fastmcp``, that takes the
whole package down before any of our code runs. It is not hypothetical: the
official Home Assistant container ships ``rich 10.16.2`` (held below 11 by
``surepy``'s ``rich>=10.1.0,<11.0.0``) and ``rich`` is absent from HA's
``package_constraints.txt``, so whether installing ``ha-mcp`` pulls ``rich``
forward is left entirely to the resolver — it usually does, and when it does
not the integration cannot start.

The declared dependency floor is correct upstream and this module does not
second-guess it: it only intervenes when the *installed* ``RichHandler``
genuinely cannot accept a keyword, dropping the unsupported ones so logging
setup degrades instead of aborting the import. On ``rich>=13.9.4`` it is a
no-op. Handlers still attach and still render; such installs lose only
traceback frame-limiting and framework-frame suppression, which beats not
importing.

Remove this once the upstream fix is released and ha-mcp's floor excludes the
affected fastmcp versions.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Marker set on our wrapper so repeated calls do not stack wrappers.
_PATCH_MARKER = "_ha_mcp_filters_unsupported_kwargs"

# Present only on rich>=13.9.4; its absence is what makes fastmcp's call fatal.
_PROBE_KWARG = "tracebacks_max_frames"


def ensure_rich_handler_compat() -> bool:
    """Make ``RichHandler`` ignore keywords the installed ``rich`` lacks.

    Returns ``True`` when a shim is in place (freshly applied or already
    applied), ``False`` when none is needed or none could be applied — an
    unimportable ``rich``, an uninspectable ``__init__``, or a signature that
    already absorbs ``**kwargs``.
    """
    try:
        from rich.logging import RichHandler
    except Exception:  # pragma: no cover - rich absent or broken
        # fastmcp depends on rich, so this means the import would fail anyway
        # for reasons this shim cannot address.
        logger.debug("rich.logging unavailable; skipping RichHandler shim")
        return False

    original_init = RichHandler.__init__
    if getattr(original_init, _PATCH_MARKER, False):
        return True

    try:
        parameters = inspect.signature(original_init).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic rich build
        logger.debug("RichHandler.__init__ is not introspectable; skipping shim")
        return False

    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        # Already tolerant of anything fastmcp passes.
        return False

    if _PROBE_KWARG in parameters:
        return False

    supported = frozenset(parameters)

    @functools.wraps(original_init)
    def _init(self: Any, *args: Any, **kwargs: Any) -> Any:
        dropped = kwargs.keys() - supported
        if dropped:
            logger.debug(
                "Dropping RichHandler kwargs this rich does not accept: %s",
                ", ".join(sorted(dropped)),
            )
        return original_init(
            self, *args, **{k: v for k, v in kwargs.items() if k in supported}
        )

    setattr(_init, _PATCH_MARKER, True)
    RichHandler.__init__ = _init  # type: ignore[method-assign]

    logger.debug(
        "Installed RichHandler compatibility shim: rich is older than "
        "fastmcp expects, so tracebacks lose frame-limiting and "
        "framework-frame suppression. Upgrade to rich>=13.9.4 to restore them."
    )
    return True


# Applied as an import side effect: ``ha_mcp/__init__.py`` imports this module
# ahead of anything that reaches fastmcp, and that is the only window in which
# the shim can do any good.
_SHIM_APPLIED = ensure_rich_handler_compat()
