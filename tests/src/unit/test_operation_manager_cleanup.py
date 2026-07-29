"""Unit tests for OperationManager.cleanup_expired_operations TTLs.

Regression pin for the TIMEOUT leak: ``get_operation()`` marks an
expired PENDING operation as TIMEOUT in place on the read path, so a
TIMEOUT-status operation can sit in ``self.operations`` outside the
cleanup pass that normally marks-and-removes in one step. The cleanup
predicate must reclaim those too — before the fix they matched no
branch (not COMPLETED, not FAILED, no longer PENDING) and accumulated
without bound in a long-lived server, while the overflow trim only
sweeps COMPLETED.
"""

import time

from ha_mcp.utils.operation_manager import (
    DeviceOperation,
    OperationManager,
    OperationStatus,
)


def _make_operation(
    op_id: str,
    status: OperationStatus,
    age_seconds: float,
    timeout_ms: int = 10000,
) -> DeviceOperation:
    return DeviceOperation(
        operation_id=op_id,
        entity_id="light.test",
        action="turn_on",
        service_domain="light",
        service_name="turn_on",
        service_data={},
        status=status,
        start_time=(time.time() - age_seconds) * 1000,
        timeout_ms=timeout_ms,
    )


def _manager_with(*operations: DeviceOperation) -> OperationManager:
    manager = OperationManager()
    for op in operations:
        manager.operations[op.operation_id] = op
    return manager


class TestCleanupExpiredOperations:
    def test_read_path_timeout_operation_is_reclaimed(self):
        # The regression scenario end to end: an expired PENDING op is
        # flipped to TIMEOUT by the read path (completion_time = now). It
        # survives its terminal minute so the timeout stays queryable,
        # then cleanup reclaims it.
        manager = _manager_with(
            _make_operation("op-1", OperationStatus.PENDING, 120, timeout_ms=1000)
        )
        polled = manager.get_operation("op-1")
        assert polled is not None
        assert polled.status == OperationStatus.TIMEOUT

        manager.cleanup_expired_operations(force=True)
        assert "op-1" in manager.operations, "still inside its terminal minute"

        manager.operations["op-1"].completion_time = (time.time() - 61) * 1000
        manager.cleanup_expired_operations(force=True)
        assert "op-1" not in manager.operations

    def test_old_timeout_operation_is_removed(self):
        manager = _manager_with(_make_operation("op-1", OperationStatus.TIMEOUT, 120))
        manager.cleanup_expired_operations(force=True)
        assert "op-1" not in manager.operations

    def test_young_timeout_operation_is_kept(self):
        manager = _manager_with(_make_operation("op-1", OperationStatus.TIMEOUT, 10))
        manager.cleanup_expired_operations(force=True)
        assert "op-1" in manager.operations

    def test_terminal_ttl_anchors_on_completion_time(self):
        # A long-timeout op can be flipped to TIMEOUT well after start_time
        # (get_operation's read path). Its terminal minute counts from
        # completion_time, so a just-timed-out op must survive cleanup even
        # when start_time is already old.
        manager = _manager_with(_make_operation("op-1", OperationStatus.TIMEOUT, 300))
        manager.operations["op-1"].completion_time = time.time() * 1000
        manager.cleanup_expired_operations(force=True)
        assert "op-1" in manager.operations

    def test_old_failed_operation_is_removed(self):
        manager = _manager_with(_make_operation("op-1", OperationStatus.FAILED, 120))
        manager.cleanup_expired_operations(force=True)
        assert "op-1" not in manager.operations

    def test_young_failed_operation_is_kept(self):
        manager = _manager_with(_make_operation("op-1", OperationStatus.FAILED, 10))
        manager.cleanup_expired_operations(force=True)
        assert "op-1" in manager.operations

    def test_completed_ttl_is_five_minutes(self):
        manager = _manager_with(
            _make_operation("young", OperationStatus.COMPLETED, 120),
            _make_operation("old", OperationStatus.COMPLETED, 400),
        )
        manager.cleanup_expired_operations(force=True)
        assert "young" in manager.operations
        assert "old" not in manager.operations

    def test_expired_pending_is_marked_timeout_and_removed(self):
        manager = _manager_with(
            _make_operation("op-1", OperationStatus.PENDING, 120, timeout_ms=1000)
        )
        manager.cleanup_expired_operations(force=True)
        assert "op-1" not in manager.operations

    def test_live_pending_operation_is_kept(self):
        manager = _manager_with(
            _make_operation("op-1", OperationStatus.PENDING, 1, timeout_ms=60000)
        )
        manager.cleanup_expired_operations(force=True)
        assert "op-1" in manager.operations

    def test_overflow_trims_oldest_terminal_never_pending(self):
        # Over the cap with young (not-yet-TTL-expired) operations: the
        # overflow trim must evict the oldest terminal ones and never an
        # in-flight PENDING op.
        manager = _manager_with(
            _make_operation("done-old", OperationStatus.COMPLETED, 200),
            _make_operation("failed-young", OperationStatus.FAILED, 30),
            _make_operation(
                "pending-live", OperationStatus.PENDING, 1, timeout_ms=60000
            ),
        )
        manager.operations["done-old"].completion_time = (time.time() - 200) * 1000
        manager.operations["failed-young"].completion_time = (time.time() - 30) * 1000
        manager.max_operations = 2

        manager.cleanup_expired_operations(force=True)

        assert "pending-live" in manager.operations
        assert "done-old" not in manager.operations
        assert "failed-young" in manager.operations
