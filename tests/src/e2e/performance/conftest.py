"""
Pytest configuration for performance tests.

This module provides fixtures and hooks specific to performance testing,
including session-level metrics collection and reporting.
"""

import logging

import pytest

from ..utilities.performance import (
    PerformanceMetrics,
    get_session_metrics,
    reset_session_metrics,
)

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session", autouse=True)
def performance_session_setup():
    """Set up and tear down performance metrics collection for the test session."""
    logger.info("=" * 60)
    logger.info("PERFORMANCE TEST SESSION STARTED")
    logger.info("=" * 60)

    # Reset metrics at session start
    reset_session_metrics()

    yield

    # Print final report at session end
    metrics = get_session_metrics()
    if metrics.results:
        logger.info("=" * 60)
        logger.info("PERFORMANCE TEST SESSION COMPLETED")
        logger.info("=" * 60)
        metrics.print_report()


@pytest.fixture
def perf_metrics():
    """Provide a fresh PerformanceMetrics instance for individual tests."""
    return PerformanceMetrics()


def pytest_configure(config):
    """Register custom markers for performance tests."""
    config.addinivalue_line(
        "markers",
        "performance: mark test as a performance test"
    )


def pytest_collection_modifyitems(config, items):
    """Add performance marker to all tests in this directory."""
    for item in items:
        if "performance" in str(item.fspath):
            item.add_marker(pytest.mark.performance)
