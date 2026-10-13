"""Shared setup for the webhook-proxy app tests.

The dev flavor imports ``homeassistant.util.aiohttp.MockRequest`` at module
load (#2696). ``_embedded_stubs.install()`` registers that module, with the one
``MockRequest`` stand-in the repository keeps, and ``_install_runtime_stubs``
never overwrites those keys — so installing it here is what lets the dev flavor
import under ``tests/addon`` alone.
"""

from tests.src.unit._embedded_stubs import install

install()
