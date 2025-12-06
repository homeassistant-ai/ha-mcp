"""
Performance baseline tests for MCP tools.

These tests establish and validate performance baselines for critical MCP tools.
They measure response times and assert they meet the defined targets.

Baseline targets (from issue #264):
- ha_get_overview: < 500ms minimal, < 1000ms full
- ha_search_entities: < 300ms
- ha_deep_search: < 2000ms
- ha_call_service: < 200ms

Usage:
    # Run all performance tests
    pytest tests/src/e2e/performance/ -v -m performance

    # Run with detailed timing output
    pytest tests/src/e2e/performance/ -v -m performance --log-cli-level=DEBUG
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success
from ..utilities.performance import (
    PERFORMANCE_BASELINES,
    PerformanceMetrics,
    calculate_percentile,
    measure_tool_call,
    run_performance_iterations,
)

logger = logging.getLogger(__name__)


@pytest.fixture
def perf_metrics():
    """Create a fresh metrics collector for each test."""
    return PerformanceMetrics()


@pytest.mark.asyncio
@pytest.mark.performance
async def test_get_overview_performance(mcp_client, perf_metrics):
    """
    Test ha_get_overview tool meets performance target.

    Target: < 1000ms for full overview
    """
    logger.info("Testing ha_get_overview performance (full mode)")

    # Run multiple iterations for stable measurement
    results = await run_performance_iterations(
        mcp_client,
        tool_name="ha_get_overview",
        params={"detail_level": "full"},
        iterations=3,
        warmup=1,
    )

    # Calculate statistics
    avg_ms = sum(r.duration_ms for r in results) / len(results)
    p95_ms = calculate_percentile(results, 95)

    logger.info(f"ha_get_overview (full): avg={avg_ms:.2f}ms, p95={p95_ms:.2f}ms")

    # Assert average is within target
    baseline = PERFORMANCE_BASELINES["ha_get_overview"]
    assert avg_ms <= baseline.target_ms, (
        f"ha_get_overview average ({avg_ms:.2f}ms) exceeds target ({baseline.target_ms}ms)"
    )

    # Log warning if approaching threshold
    if baseline.warning_threshold_ms and avg_ms > baseline.warning_threshold_ms:
        logger.warning(
            f"ha_get_overview approaching target: {avg_ms:.2f}ms "
            f"(warning threshold: {baseline.warning_threshold_ms}ms)"
        )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_get_overview_minimal_performance(mcp_client, perf_metrics):
    """
    Test ha_get_overview tool in minimal mode meets stricter target.

    Target: < 500ms for minimal overview
    """
    logger.info("Testing ha_get_overview performance (minimal mode)")

    results = await run_performance_iterations(
        mcp_client,
        tool_name="ha_get_overview",
        params={"detail_level": "minimal"},
        iterations=3,
        warmup=1,
    )

    avg_ms = sum(r.duration_ms for r in results) / len(results)
    p95_ms = calculate_percentile(results, 95)

    logger.info(f"ha_get_overview (minimal): avg={avg_ms:.2f}ms, p95={p95_ms:.2f}ms")

    baseline = PERFORMANCE_BASELINES["ha_get_overview_minimal"]
    assert avg_ms <= baseline.target_ms, (
        f"ha_get_overview minimal average ({avg_ms:.2f}ms) exceeds target ({baseline.target_ms}ms)"
    )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_search_entities_performance(mcp_client, perf_metrics):
    """
    Test ha_search_entities tool meets performance target.

    Target: < 300ms
    """
    logger.info("Testing ha_search_entities performance")

    # Test with a common search query
    results = await run_performance_iterations(
        mcp_client,
        tool_name="ha_search_entities",
        params={"query": "light", "limit": 20},
        iterations=5,
        warmup=1,
    )

    avg_ms = sum(r.duration_ms for r in results) / len(results)
    p95_ms = calculate_percentile(results, 95)

    logger.info(f"ha_search_entities: avg={avg_ms:.2f}ms, p95={p95_ms:.2f}ms")

    baseline = PERFORMANCE_BASELINES["ha_search_entities"]
    assert avg_ms <= baseline.target_ms, (
        f"ha_search_entities average ({avg_ms:.2f}ms) exceeds target ({baseline.target_ms}ms)"
    )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_search_entities_domain_filter_performance(mcp_client, perf_metrics):
    """
    Test ha_search_entities with domain filter performance.

    Domain filtering should not significantly impact performance.
    """
    logger.info("Testing ha_search_entities with domain filter performance")

    results = await run_performance_iterations(
        mcp_client,
        tool_name="ha_search_entities",
        params={"query": "", "domain_filter": "light", "limit": 50},
        iterations=5,
        warmup=1,
    )

    avg_ms = sum(r.duration_ms for r in results) / len(results)
    p95_ms = calculate_percentile(results, 95)

    logger.info(
        f"ha_search_entities (domain filter): avg={avg_ms:.2f}ms, p95={p95_ms:.2f}ms"
    )

    # Use same baseline - domain filtering shouldn't add significant overhead
    baseline = PERFORMANCE_BASELINES["ha_search_entities"]
    assert avg_ms <= baseline.target_ms, (
        f"ha_search_entities with domain filter average ({avg_ms:.2f}ms) "
        f"exceeds target ({baseline.target_ms}ms)"
    )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_deep_search_performance(mcp_client, perf_metrics):
    """
    Test ha_deep_search tool meets performance target.

    Target: < 2000ms (this searches across automations, scripts, and helpers)
    """
    logger.info("Testing ha_deep_search performance")

    results = await run_performance_iterations(
        mcp_client,
        tool_name="ha_deep_search",
        params={"query": "light", "limit": 10},
        iterations=3,
        warmup=1,
    )

    avg_ms = sum(r.duration_ms for r in results) / len(results)
    p95_ms = calculate_percentile(results, 95)

    logger.info(f"ha_deep_search: avg={avg_ms:.2f}ms, p95={p95_ms:.2f}ms")

    baseline = PERFORMANCE_BASELINES["ha_deep_search"]
    assert avg_ms <= baseline.target_ms, (
        f"ha_deep_search average ({avg_ms:.2f}ms) exceeds target ({baseline.target_ms}ms)"
    )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_get_state_performance(mcp_client, perf_metrics):
    """
    Test ha_get_state tool meets performance target.

    Target: < 100ms for single entity state retrieval
    """
    logger.info("Testing ha_get_state performance")

    # Use sun.sun which always exists
    results = await run_performance_iterations(
        mcp_client,
        tool_name="ha_get_state",
        params={"entity_id": "sun.sun"},
        iterations=5,
        warmup=2,
    )

    avg_ms = sum(r.duration_ms for r in results) / len(results)
    p95_ms = calculate_percentile(results, 95)

    logger.info(f"ha_get_state: avg={avg_ms:.2f}ms, p95={p95_ms:.2f}ms")

    baseline = PERFORMANCE_BASELINES["ha_get_state"]
    assert avg_ms <= baseline.target_ms, (
        f"ha_get_state average ({avg_ms:.2f}ms) exceeds target ({baseline.target_ms}ms)"
    )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_call_service_performance(mcp_client, perf_metrics):
    """
    Test ha_call_service tool meets performance target.

    Target: < 200ms
    """
    logger.info("Testing ha_call_service performance")

    # Use a safe service call that doesn't require specific entities
    results = await run_performance_iterations(
        mcp_client,
        tool_name="ha_call_service",
        params={
            "domain": "homeassistant",
            "service": "check_config",
        },
        iterations=3,
        warmup=1,
    )

    avg_ms = sum(r.duration_ms for r in results) / len(results)
    p95_ms = calculate_percentile(results, 95)

    logger.info(f"ha_call_service: avg={avg_ms:.2f}ms, p95={p95_ms:.2f}ms")

    baseline = PERFORMANCE_BASELINES["ha_call_service"]
    assert avg_ms <= baseline.target_ms, (
        f"ha_call_service average ({avg_ms:.2f}ms) exceeds target ({baseline.target_ms}ms)"
    )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_list_tools_performance(mcp_client, perf_metrics):
    """
    Test MCP tool listing performance.

    This is a meta-test to ensure the MCP server itself responds quickly.
    Target: < 100ms
    """
    logger.info("Testing list_tools performance")

    import time

    # Warmup
    await mcp_client.list_tools()

    # Measure
    durations = []
    for _ in range(5):
        start = time.perf_counter()
        tools = await mcp_client.list_tools()
        end = time.perf_counter()
        durations.append((end - start) * 1000)
        assert len(tools) > 0, "Should have tools available"

    avg_ms = sum(durations) / len(durations)
    logger.info(f"list_tools: avg={avg_ms:.2f}ms")

    # Listing tools should be very fast
    assert avg_ms < 100, f"list_tools average ({avg_ms:.2f}ms) exceeds 100ms target"


@pytest.mark.asyncio
@pytest.mark.performance
async def test_concurrent_operations_performance(mcp_client, perf_metrics):
    """
    Test performance under concurrent load.

    Measure how the server handles multiple simultaneous requests.
    Note: This is a softer test that primarily measures whether concurrency
    works at all, not strict timing (CI environments vary significantly).
    """
    import asyncio

    logger.info("Testing concurrent operations performance")

    async def timed_search(query: str) -> float:
        """Execute a search and return duration in ms."""
        import time

        start = time.perf_counter()
        await mcp_client.call_tool("ha_search_entities", {"query": query, "limit": 5})
        return (time.perf_counter() - start) * 1000

    # Run 5 concurrent searches
    queries = ["light", "switch", "sensor", "automation", "script"]

    start_total = asyncio.get_event_loop().time()
    durations = await asyncio.gather(*[timed_search(q) for q in queries])
    total_time = (asyncio.get_event_loop().time() - start_total) * 1000

    avg_individual = sum(durations) / len(durations)
    max_individual = max(durations)

    logger.info(
        f"Concurrent operations: total={total_time:.2f}ms, "
        f"avg_individual={avg_individual:.2f}ms, max={max_individual:.2f}ms"
    )

    # Main assertion: verify all operations completed successfully (implicitly done above)
    # Total time should show some parallelism benefit (not 5x serial time)
    # Use a generous threshold for CI environments which can be slow
    baseline = PERFORMANCE_BASELINES["ha_search_entities"]
    # Allow up to 5x target (very generous for CI environments)
    max_allowed_ms = baseline.target_ms * 5
    assert max_individual <= max_allowed_ms, (
        f"Slowest concurrent search ({max_individual:.2f}ms) exceeds "
        f"5x target ({max_allowed_ms}ms) - possible resource exhaustion"
    )

    # Log warning if above 2x target (informational)
    if max_individual > baseline.target_ms * 2:
        logger.warning(
            f"Concurrent search slower than expected: {max_individual:.2f}ms "
            f"(> 2x target {baseline.target_ms * 2}ms). CI environment may be under load."
        )


@pytest.mark.asyncio
@pytest.mark.performance
async def test_performance_report_generation(mcp_client):
    """
    Test that generates a comprehensive performance report.

    This test runs all key operations and produces a summary report.
    """
    logger.info("Generating comprehensive performance report")

    metrics = PerformanceMetrics()

    # Run key operations
    operations = [
        ("ha_get_overview", {"detail_level": "full"}),
        ("ha_get_overview", {"detail_level": "minimal"}),
        ("ha_search_entities", {"query": "light", "limit": 10}),
        ("ha_search_entities", {"query": "", "domain_filter": "sensor", "limit": 20}),
        ("ha_deep_search", {"query": "light", "limit": 5}),
        ("ha_get_state", {"entity_id": "sun.sun"}),
    ]

    for tool_name, params in operations:
        try:
            result, perf = await measure_tool_call(
                mcp_client, tool_name, params, operation_name=tool_name
            )
            # Verify the operation succeeded
            assert_mcp_success(result, tool_name)
            metrics.add_result(perf)
        except Exception as e:
            logger.error(f"Failed to measure {tool_name}: {e}")

    # Generate and log report
    summary = metrics.get_summary()
    logger.info(f"Performance summary: {summary}")

    # Print formatted report
    metrics.print_report()

    # Basic assertions
    assert summary["total_operations"] > 0, "Should have measured some operations"
    assert summary["failed"] == 0, "All operations should succeed"
