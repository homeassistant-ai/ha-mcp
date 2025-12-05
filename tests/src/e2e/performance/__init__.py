"""Performance tests for Home Assistant MCP Server.

This module contains performance regression tests that measure and validate
the response times of key MCP tools against established baselines.

Tests are marked with @pytest.mark.performance and can be run separately:
    pytest tests/src/e2e/performance/ -v -m performance
"""
