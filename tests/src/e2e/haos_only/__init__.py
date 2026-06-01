"""Tests that only make sense against the HAOS QEMU backend (see #1281).

These tests use the dispatched ``mcp_client`` fixture from the parent
``e2e/conftest.py`` and assume Supervisor + pre-installed addons are
available. The ``haos_only`` marker (auto-applied to everything in
this package) makes them skip on the testcontainer backend.

Container-side equivalents either don't exist (e.g. real addon lifecycle)
or are covered by the ``supervisor_mock`` shim added in #1192.
"""
