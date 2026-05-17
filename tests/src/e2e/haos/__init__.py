"""HAOS-tier E2E tests (see #1281).

Tests in this package run against a pre-baked HAOS qcow2 booted under
QEMU/KVM, not the testcontainer Docker image used by the rest of the
``e2e/`` suite. They opt in via ``@pytest.mark.haos`` and are scheduled by
``.github/workflows/haos-e2e-tests.yml``.
"""
