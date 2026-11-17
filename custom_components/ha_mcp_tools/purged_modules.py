"""Release what keeps purged ``ha_mcp`` modules in memory (#3131).

Dropping the modules from ``sys.modules`` frees nothing while a cache or a
logger elsewhere in the process still holds one of their functions or classes:
every purge then leaves a whole copy of the server's code behind until Home
Assistant restarts. These are the holders a started server leaves.
"""

from __future__ import annotations

import logging
import sys
import typing
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

# uncalled_for (the server's dependency injection) keeps every function it has
# inspected as a key of these module-level dicts and never drops one.
_FUNCTION_CACHES = {
    "uncalled_for.annotations": ("_metadata_cache", "_annotation_cache"),
    "uncalled_for.introspection": ("_signature_cache", "_parameter_cache"),
}


def _is_server_code(obj: object) -> bool:
    """Whether ``obj``, or the class it is an instance of, was defined in ``ha_mcp``."""
    for owner in (getattr(obj, "__func__", obj), type(obj)):
        name = getattr(owner, "__module__", None)
        if isinstance(name, str) and (name == "ha_mcp" or name.startswith("ha_mcp.")):
            return True
    return False


def _detach_log_hooks() -> None:
    """Remove the server's filters, and the handlers carrying one, from every logger."""
    loggers = [logging.getLogger(), *logging.root.manager.loggerDict.values()]
    for logger in loggers:
        if not isinstance(logger, logging.Logger):
            continue
        for handler in list(logger.handlers):
            if _is_server_code(handler) or any(map(_is_server_code, handler.filters)):
                logger.removeHandler(handler)
        for log_filter in list(logger.filters):
            if _is_server_code(log_filter):
                logger.removeFilter(log_filter)


def _forget_overloads() -> None:
    """``typing.overload`` registers every overload under its module's name."""
    registry = getattr(typing, "_overload_registry", None)
    if isinstance(registry, dict):
        for name in [name for name in registry if _is_server_code(_Named(name))]:
            del registry[name]


class _Named:
    """Lets a bare module name go through ``_is_server_code``."""

    def __init__(self, module: str) -> None:
        self.__module__ = module


def _forget_inspected_functions() -> None:
    for module_name, cache_names in _FUNCTION_CACHES.items():
        module = sys.modules.get(module_name)
        for cache_name in cache_names:
            cache = getattr(module, cache_name, None)
            if isinstance(cache, dict):
                for function in [f for f in cache if _is_server_code(f)]:
                    del cache[function]


def _forget_generic_models() -> None:
    """pydantic keeps each ``Model[Arg]`` it built under a key naming the model."""
    generics = sys.modules.get("pydantic._internal._generics")
    cache = getattr(generics, "_GENERIC_TYPES_CACHE", None)
    if cache is None:
        return
    for key in list(cache.keys()):
        parts = [
            p for part in key for p in (part if isinstance(part, tuple) else (part,))
        ]
        if any(map(_is_server_code, parts)):
            cache.pop(key, None)


def _clear_typing_caches() -> None:
    """A generic alias such as ``Context[Session]`` is cached with its arguments."""
    for clear in list(getattr(typing, "_cleanups", ())):
        clear()


def release_purged_modules() -> None:
    """Let go of the purged server code; a holder that cannot be released is logged."""
    steps: tuple[Callable[[], None], ...] = (
        _detach_log_hooks,
        _forget_overloads,
        _forget_inspected_functions,
        _forget_generic_models,
        _clear_typing_caches,
    )
    for step in steps:
        try:
            step()
        except Exception:
            _LOGGER.debug(
                "Could not run %s after the module purge", step.__name__, exc_info=True
            )
