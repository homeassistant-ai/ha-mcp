"""HAOS test-image build pipeline.

Boots vanilla HAOS in QEMU, onboards, installs the ha-mcp addon repo plus
preconfigured addons and integrations, then saves the resulting qcow2 for
publication as the canary image used by the HAOS E2E test tier (see #1281).
"""
