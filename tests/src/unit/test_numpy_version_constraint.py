"""
Test to enforce NumPy version constraint for CPU compatibility.

This test ensures numpy stays pinned to 2.3.x to maintain compatibility with
older CPUs and VMs that don't support x86-64-v2 (SSE4.1/SSE4.2) instructions.

NumPy 2.4.0+ requires x86-64-v2 baseline which includes:
- SSE4.1, SSE4.2, SSSE3, POPCNT
- Not available on pre-2009 CPUs or certain VM configurations

NumPy 2.3.x uses SSE3 baseline which is more broadly compatible.

Related: Issue #406
"""

import sys
from importlib.metadata import version

import pytest


def test_numpy_version_constraint_for_cpu_compatibility():
    """
    Verify numpy version is <2.4.0 to prevent SIGILL on old CPUs.

    NumPy 2.4.0 raised the baseline to x86-64-v2 which requires SSE4 instructions.
    This test will fail if numpy is accidentally upgraded, reminding us that
    the version pin is intentional for CPU compatibility.
    """
    # Skip test on ARM platforms where numpy pin doesn't apply
    if any(
        arch in sys.platform or arch in str(sys.implementation)
        for arch in ["arm64", "aarch64", "ARM64"]
    ):
        pytest.skip("NumPy version pin only applies to x86_64 platforms")

    try:
        import numpy

        numpy_version = version("numpy")
    except ImportError:
        # numpy is optional dependency on some platforms
        pytest.skip("numpy not installed on this platform")

    # Parse version
    major, minor, *_ = numpy_version.split(".")
    major_minor = f"{major}.{minor}"

    # Assert we're on 2.3.x
    assert (
        major_minor == "2.3"
    ), f"""
NumPy version {numpy_version} detected, but must be 2.3.x for CPU compatibility.

NumPy 2.4.0+ requires x86-64-v2 baseline (SSE4.1/SSE4.2) which causes SIGILL
crashes on older CPUs and VMs. NumPy 2.3.x uses SSE3 baseline for broader
compatibility.

If you're upgrading numpy intentionally:
1. Verify the new version works on CPUs without SSE4 support
2. Test on old CPU or VM (e.g., KVM with old CPU model)
3. Update this test and pyproject.toml constraints together

Related: Issue #406 - https://github.com/homeassistant-ai/ha-mcp/issues/406
"""

    # Also verify we're at least on 2.3.0 (not downgraded)
    assert int(major) >= 2, f"NumPy {numpy_version} is too old, need >=2.3.0"
    if int(major) == 2:
        assert int(minor) >= 3, f"NumPy {numpy_version} is too old, need >=2.3.0"
