"""
Performance measurement utilities for E2E testing.

This module provides timing decorators, context managers, and assertion helpers
for measuring and validating performance of MCP tool operations.

Baseline targets (from issue #264):
- ha_get_overview: < 500ms minimal, < 1000ms full
- ha_search_entities: < 300ms
- ha_deep_search: < 2000ms
- ha_call_service: < 200ms
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

# Type variables for generic decorators
P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class PerformanceResult:
    """Result of a performance measurement."""

    operation: str
    duration_ms: float
    success: bool
    result: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Return duration in seconds."""
        return self.duration_ms / 1000

    def __str__(self) -> str:
        status = "OK" if self.success else "FAILED"
        return f"{self.operation}: {self.duration_ms:.2f}ms [{status}]"


@dataclass
class PerformanceBaseline:
    """Performance baseline configuration for a tool."""

    tool_name: str
    target_ms: float
    warning_threshold_ms: float | None = None
    description: str = ""

    def __post_init__(self):
        if self.warning_threshold_ms is None:
            # Default warning threshold is 80% of target
            self.warning_threshold_ms = self.target_ms * 0.8


# Baseline configurations from issue #264
PERFORMANCE_BASELINES: dict[str, PerformanceBaseline] = {
    "ha_get_overview": PerformanceBaseline(
        tool_name="ha_get_overview",
        target_ms=1000,  # Full overview target
        warning_threshold_ms=800,
        description="System overview (full mode)",
    ),
    "ha_get_overview_minimal": PerformanceBaseline(
        tool_name="ha_get_overview",
        target_ms=500,
        warning_threshold_ms=400,
        description="System overview (minimal mode)",
    ),
    "ha_search_entities": PerformanceBaseline(
        tool_name="ha_search_entities",
        target_ms=300,
        warning_threshold_ms=240,
        description="Entity search with fuzzy matching",
    ),
    "ha_deep_search": PerformanceBaseline(
        tool_name="ha_deep_search",
        target_ms=2000,
        warning_threshold_ms=1600,
        description="Deep search across automations/scripts/helpers",
    ),
    "ha_call_service": PerformanceBaseline(
        tool_name="ha_call_service",
        target_ms=200,
        warning_threshold_ms=160,
        description="Service call execution",
    ),
    "ha_get_state": PerformanceBaseline(
        tool_name="ha_get_state",
        target_ms=100,
        warning_threshold_ms=80,
        description="Single entity state retrieval",
    ),
}


class PerformanceMetrics:
    """Collects and reports performance metrics for a test session."""

    def __init__(self):
        self.results: list[PerformanceResult] = []

    def add_result(self, result: PerformanceResult) -> None:
        """Add a performance measurement result."""
        self.results.append(result)
        self._log_result(result)

    def _log_result(self, result: PerformanceResult) -> None:
        """Log a performance result with appropriate level."""
        baseline = PERFORMANCE_BASELINES.get(result.operation)

        if baseline:
            if result.duration_ms > baseline.target_ms:
                logger.warning(
                    f"SLOW: {result.operation} took {result.duration_ms:.2f}ms "
                    f"(target: {baseline.target_ms}ms)"
                )
            elif (
                baseline.warning_threshold_ms
                and result.duration_ms > baseline.warning_threshold_ms
            ):
                logger.info(
                    f"WARN: {result.operation} took {result.duration_ms:.2f}ms "
                    f"(approaching target: {baseline.target_ms}ms)"
                )
            else:
                logger.debug(
                    f"OK: {result.operation} took {result.duration_ms:.2f}ms "
                    f"(target: {baseline.target_ms}ms)"
                )
        else:
            logger.debug(f"{result.operation} took {result.duration_ms:.2f}ms")

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of all collected metrics."""
        if not self.results:
            return {"total_operations": 0}

        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]

        by_operation: dict[str, list[PerformanceResult]] = {}
        for result in self.results:
            if result.operation not in by_operation:
                by_operation[result.operation] = []
            by_operation[result.operation].append(result)

        operation_stats = {}
        for op_name, op_results in by_operation.items():
            durations = [r.duration_ms for r in op_results]
            operation_stats[op_name] = {
                "count": len(op_results),
                "min_ms": min(durations),
                "max_ms": max(durations),
                "avg_ms": sum(durations) / len(durations),
                "success_rate": sum(1 for r in op_results if r.success) / len(op_results),
            }

            # Add baseline comparison if available
            baseline = PERFORMANCE_BASELINES.get(op_name)
            if baseline:
                operation_stats[op_name]["target_ms"] = baseline.target_ms
                operation_stats[op_name]["within_target"] = all(
                    r.duration_ms <= baseline.target_ms for r in op_results
                )

        return {
            "total_operations": len(self.results),
            "successful": len(successful),
            "failed": len(failed),
            "by_operation": operation_stats,
        }

    def print_report(self) -> None:
        """Print a formatted performance report."""
        summary = self.get_summary()

        print("\n" + "=" * 60)
        print("PERFORMANCE REPORT")
        print("=" * 60)
        print(f"Total operations: {summary['total_operations']}")
        print(f"Successful: {summary['successful']}")
        print(f"Failed: {summary['failed']}")
        print("-" * 60)

        for op_name, stats in summary.get("by_operation", {}).items():
            baseline = PERFORMANCE_BASELINES.get(op_name)
            target_info = f" (target: {baseline.target_ms}ms)" if baseline else ""
            status = ""
            if baseline:
                status = " [OK]" if stats["within_target"] else " [SLOW]"

            print(f"\n{op_name}{target_info}{status}")
            print(f"  Count: {stats['count']}")
            print(f"  Min: {stats['min_ms']:.2f}ms")
            print(f"  Max: {stats['max_ms']:.2f}ms")
            print(f"  Avg: {stats['avg_ms']:.2f}ms")
            print(f"  Success rate: {stats['success_rate'] * 100:.1f}%")

        print("=" * 60 + "\n")


# Global metrics collector for the test session
_session_metrics = PerformanceMetrics()


def get_session_metrics() -> PerformanceMetrics:
    """Get the session-wide performance metrics collector."""
    return _session_metrics


def reset_session_metrics() -> None:
    """Reset the session-wide performance metrics."""
    global _session_metrics
    _session_metrics = PerformanceMetrics()


@asynccontextmanager
async def measure_performance(
    operation: str, metrics: PerformanceMetrics | None = None
):
    """
    Context manager to measure the performance of an async operation.

    Usage:
        async with measure_performance("ha_get_overview") as perf:
            result = await mcp_client.call_tool("ha_get_overview", {})
            perf.result = result

        print(f"Duration: {perf.duration_ms}ms")
    """
    if metrics is None:
        metrics = _session_metrics

    result = PerformanceResult(
        operation=operation, duration_ms=0, success=False, result=None
    )

    start_time = time.perf_counter()
    try:
        yield result
        result.success = True
    except Exception as e:
        result.error = str(e)
        result.success = False
        raise
    finally:
        end_time = time.perf_counter()
        result.duration_ms = (end_time - start_time) * 1000
        metrics.add_result(result)


def timed_operation(
    operation_name: str | None = None, metrics: PerformanceMetrics | None = None
):
    """
    Decorator to time an async function and record performance metrics.

    Usage:
        @timed_operation("ha_get_overview")
        async def test_get_overview(mcp_client):
            return await mcp_client.call_tool("ha_get_overview", {})
    """

    def decorator(
        func: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[tuple[R, PerformanceResult]]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> tuple[R, PerformanceResult]:
            op_name = operation_name or func.__name__
            target_metrics = metrics or _session_metrics

            start_time = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                end_time = time.perf_counter()
                duration_ms = (end_time - start_time) * 1000

                perf_result = PerformanceResult(
                    operation=op_name,
                    duration_ms=duration_ms,
                    success=True,
                    result=result,
                )
                target_metrics.add_result(perf_result)
                return result, perf_result

            except Exception as e:
                end_time = time.perf_counter()
                duration_ms = (end_time - start_time) * 1000

                perf_result = PerformanceResult(
                    operation=op_name,
                    duration_ms=duration_ms,
                    success=False,
                    error=str(e),
                )
                target_metrics.add_result(perf_result)
                raise

        return wrapper

    return decorator


# Performance assertion helpers


def assert_within_target(
    perf_result: PerformanceResult,
    target_ms: float | None = None,
    baseline_name: str | None = None,
) -> None:
    """
    Assert that a performance result is within the target threshold.

    Args:
        perf_result: The performance measurement result
        target_ms: Explicit target in milliseconds (overrides baseline)
        baseline_name: Name of baseline to use from PERFORMANCE_BASELINES
    """
    if target_ms is None:
        # Try to get from baselines
        name = baseline_name or perf_result.operation
        baseline = PERFORMANCE_BASELINES.get(name)
        if baseline:
            target_ms = baseline.target_ms
        else:
            raise ValueError(
                f"No target specified and no baseline found for '{name}'"
            )

    if perf_result.duration_ms > target_ms:
        raise AssertionError(
            f"Performance target exceeded for {perf_result.operation}: "
            f"{perf_result.duration_ms:.2f}ms > {target_ms}ms target"
        )


def assert_performance_regression(
    current: PerformanceResult,
    baseline_ms: float,
    tolerance_percent: float = 20.0,
) -> None:
    """
    Assert that performance has not regressed beyond tolerance.

    Args:
        current: Current performance measurement
        baseline_ms: Previous baseline measurement in ms
        tolerance_percent: Allowed regression percentage (default 20%)
    """
    allowed_ms = baseline_ms * (1 + tolerance_percent / 100)

    if current.duration_ms > allowed_ms:
        regression_percent = (
            (current.duration_ms - baseline_ms) / baseline_ms
        ) * 100
        raise AssertionError(
            f"Performance regression detected for {current.operation}: "
            f"{current.duration_ms:.2f}ms vs baseline {baseline_ms:.2f}ms "
            f"({regression_percent:.1f}% regression, allowed: {tolerance_percent}%)"
        )


async def measure_tool_call(
    mcp_client,
    tool_name: str,
    params: dict[str, Any],
    operation_name: str | None = None,
) -> tuple[Any, PerformanceResult]:
    """
    Measure the performance of an MCP tool call.

    Args:
        mcp_client: The FastMCP client
        tool_name: Name of the tool to call
        params: Parameters to pass to the tool
        operation_name: Optional custom name for the operation (defaults to tool_name)

    Returns:
        Tuple of (tool result, performance result)
    """
    op_name = operation_name or tool_name

    async with measure_performance(op_name) as perf:
        result = await mcp_client.call_tool(tool_name, params)
        perf.result = result

    return result, perf


async def run_performance_iterations(
    mcp_client,
    tool_name: str,
    params: dict[str, Any],
    iterations: int = 5,
    warmup: int = 1,
) -> list[PerformanceResult]:
    """
    Run multiple iterations of a tool call for performance measurement.

    Args:
        mcp_client: The FastMCP client
        tool_name: Name of the tool to call
        params: Parameters to pass to the tool
        iterations: Number of timed iterations
        warmup: Number of warmup iterations (not counted)

    Returns:
        List of performance results from timed iterations
    """
    results: list[PerformanceResult] = []

    # Warmup iterations
    for i in range(warmup):
        logger.debug(f"Warmup iteration {i + 1}/{warmup} for {tool_name}")
        await mcp_client.call_tool(tool_name, params)
        await asyncio.sleep(0.1)  # Brief pause between calls

    # Timed iterations
    for i in range(iterations):
        logger.debug(f"Timed iteration {i + 1}/{iterations} for {tool_name}")
        _, perf = await measure_tool_call(mcp_client, tool_name, params)
        results.append(perf)
        await asyncio.sleep(0.1)  # Brief pause between calls

    return results


def calculate_percentile(results: list[PerformanceResult], percentile: float) -> float:
    """Calculate the Nth percentile of durations from results."""
    if not results:
        return 0.0

    durations = sorted(r.duration_ms for r in results)
    index = int(len(durations) * percentile / 100)
    index = min(index, len(durations) - 1)
    return durations[index]
