"""Shared setup for the webhook-proxy app tests.

The dev flavor imports ``homeassistant.util.aiohttp.MockRequest`` at module
load (#2696). ``_embedded_stubs`` registers that module, with the one
``MockRequest`` stand-in the repository keeps, as a side effect of being
imported, and ``_install_runtime_stubs`` never overwrites those keys — so
importing it here is what lets the dev flavor import under ``tests/addon``
alone.
"""

from tests.src.unit import _embedded_stubs  # noqa: F401
