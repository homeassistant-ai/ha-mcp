"""
In-memory operation storage and management for async device operations.

This module handles tracking of device operations that require async verification
through WebSocket state changes, providing a clean interface for storing and
retrieving operation status.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class OperationStatus(Enum):
    """Status of device operations."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class DeviceOperation:
    """Represents a device operation awaiting completion."""

    operation_id: str
    entity_id: str
    action: str
    service_domain: str
    service_name: str
    service_data: dict[str, Any]
    status: OperationStatus = OperationStatus.PENDING
    start_time: float = field(default_factory=lambda: time.time() * 1000)
    completion_time: float | None = None
    expected_state: dict[str, Any] | None = None
    result_state: dict[str, Any] | None = None
    error_message: str | None = None
    timeout_ms: int = 10000  # 10 second default timeout

    @property
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        return time.time() * 1000 - self.start_time

    @property
    def is_expired(self) -> bool:
        """Check if operation has timed out."""
        return self.elapsed_ms > self.timeout_ms

    @property
    def duration_ms(self) -> float | None:
        """Get operation duration in milliseconds."""
        if self.completion_time:
            return self.completion_time - self.start_time
        return None


class OperationManager:
    """Manages in-memory storage of device operations."""

    def __init__(self, max_operations: int = 1000, cleanup_interval: int = 300):
        """Initialize operation manager.

        Args:
            max_operations: Maximum number of operations to keep in memory
            cleanup_interval: How often to clean up expired operations (seconds)
        """
        self.operations: dict[str, DeviceOperation] = {}
        self.max_operations = max_operations
        self.cleanup_interval = cleanup_interval
        self.last_cleanup = time.time()

    def create_operation(
        self,
        entity_id: str,
        action: str,
        service_domain: str,
        service_name: str,
        service_data: dict[str, Any],
        expected_state: dict[str, Any] | None = None,
        timeout_ms: int = 10000,
    ) -> str:
        """Create a new device operation.

        Args:
            entity_id: Target entity ID
            action: Action being performed (e.g., 'turn_on', 'set_temperature')
            service_domain: Home Assistant service domain
            service_name: Home Assistant service name
            service_data: Service call data
            expected_state: Expected entity state after operation
            timeout_ms: Operation timeout in milliseconds

        Returns:
            Operation ID for tracking
        """
        operation_id = str(uuid.uuid4())

        operation = DeviceOperation(
            operation_id=operation_id,
            entity_id=entity_id,
            action=action,
            service_domain=service_domain,
            service_name=service_name,
            service_data=service_data,
            expected_state=expected_state,
            timeout_ms=timeout_ms,
        )

        self.operations[operation_id] = operation
        self._maybe_cleanup()

        logger.info(f"Created operation {operation_id} for {entity_id}: {action}")
        return operation_id

    def get_operation(self, operation_id: str) -> DeviceOperation | None:
        """Get operation by ID.

        Args:
            operation_id: Operation ID to retrieve

        Returns:
            DeviceOperation if found, None otherwise
        """
        operation = self.operations.get(operation_id)

        # Check if operation has timed out
        if (
            operation
            and operation.status == OperationStatus.PENDING
            and operation.is_expired
        ):
            operation.status = OperationStatus.TIMEOUT
            operation.completion_time = time.time() * 1000
            operation.error_message = (
                f"Operation timed out after {operation.timeout_ms}ms"
            )
            logger.warning(f"Operation {operation_id} timed out")

        return operation

    def update_operation_status(
        self,
        operation_id: str,
        status: OperationStatus,
        result_state: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Update operation status.

        Args:
            operation_id: Operation ID to update
            status: New status
            result_state: Final entity state (for completed operations)
            error_message: Error message (for failed operations)

        Returns:
            True if operation was found and updated
        """
        operation = self.operations.get(operation_id)
        if not operation:
            return False

        operation.status = status
        operation.completion_time = time.time() * 1000

        if result_state:
            operation.result_state = result_state
        if error_message:
            operation.error_message = error_message

        logger.info(f"Updated operation {operation_id} status to {status.value}")
        return True

    def get_pending_operations_for_entity(
        self, entity_id: str
    ) -> list[DeviceOperation]:
        """Get all pending operations for a specific entity.

        Args:
            entity_id: Entity ID to search for

        Returns:
            List of pending operations for the entity
        """
        pending_ops = []
        for operation in self.operations.values():
            if (
                operation.entity_id == entity_id
                and operation.status == OperationStatus.PENDING
                and not operation.is_expired
            ):
                pending_ops.append(operation)

        return pending_ops

    def process_state_change(
        self, entity_id: str, new_state: dict[str, Any]
    ) -> list[str]:
        """Process a state change event and update matching operations.

        Args:
            entity_id: Entity that changed state
            new_state: New entity state

        Returns:
            List of operation IDs that were updated
        """
        updated_operations = []
        pending_ops = self.get_pending_operations_for_entity(entity_id)

        for operation in pending_ops:
            if self._matches_expected_state(operation, new_state):
                # Operation completed successfully
                self.update_operation_status(
                    operation.operation_id,
                    OperationStatus.COMPLETED,
                    result_state=new_state,
                )
                updated_operations.append(operation.operation_id)
                logger.info(
                    f"Operation {operation.operation_id} completed for {entity_id}"
                )

            elif new_state.get("state") == "unavailable":
                # Device became unavailable - mark as failed
                self.update_operation_status(
                    operation.operation_id,
                    OperationStatus.FAILED,
                    error_message="Device became unavailable",
                )
                updated_operations.append(operation.operation_id)
                logger.warning(
                    f"Operation {operation.operation_id} failed - device unavailable"
                )

        return updated_operations

    def _matches_expected_state(
        self, operation: DeviceOperation, new_state: dict[str, Any]
    ) -> bool:
        """Check if new state matches operation's expected outcome.

        Args:
            operation: Operation to check
            new_state: New entity state

        Returns:
            True if state matches expected outcome
        """
        # If no expected state specified, any non-unavailable state counts as success
        if not operation.expected_state:
            return new_state.get("state") != "unavailable"

        # Check expected state attributes
        for key, expected_value in operation.expected_state.items():
            if key == "state":
                if new_state.get("state") != expected_value:
                    return False
            elif key in new_state.get("attributes", {}):
                if new_state["attributes"][key] != expected_value:
                    return False
            else:
                return False

        return True

    def cancel_operation(self, operation_id: str) -> bool:
        """Cancel a pending operation.

        Args:
            operation_id: Operation ID to cancel

        Returns:
            True if operation was found and cancelled
        """
        return self.update_operation_status(
            operation_id,
            OperationStatus.CANCELLED,
            error_message="Operation cancelled by user",
        )

    def get_operations_summary(self) -> dict[str, Any]:
        """Get summary of all operations.

        Returns:
            Dictionary with operation statistics
        """
        total = len(self.operations)
        by_status = {}

        for status in OperationStatus:
            by_status[status.value] = len(
                [op for op in self.operations.values() if op.status == status]
            )

        # Count expired pending operations
        expired_pending = len(
            [
                op
                for op in self.operations.values()
                if op.status == OperationStatus.PENDING and op.is_expired
            ]
        )

        return {
            "total_operations": total,
            "by_status": by_status,
            "expired_pending": expired_pending,
            "memory_usage_mb": self._estimate_memory_usage(),
        }

    def _estimate_memory_usage(self) -> float:
        """Estimate memory usage in MB (rough approximation)."""
        # Very rough estimate: ~1KB per operation
        return len(self.operations) * 1024 / (1024 * 1024)

    def cleanup_expired_operations(self, force: bool = False) -> None:
        """Clean up expired and completed operations.

        Args:
            force: Force cleanup regardless of interval
        """
        current_time = time.time()

        if not force and (current_time - self.last_cleanup) < self.cleanup_interval:
            return

        initial_count = len(self.operations)

        # Remove completed operations older than 5 minutes
        # Remove failed/cancelled operations older than 1 minute
        # Remove expired pending operations
        to_remove = []

        for op_id, operation in self.operations.items():
            age_seconds = (current_time * 1000 - operation.start_time) / 1000

            if operation.status == OperationStatus.COMPLETED and age_seconds > 300:
                to_remove.append(op_id)
            elif (
                operation.status in [OperationStatus.FAILED, OperationStatus.CANCELLED]
                and age_seconds > 60
            ):
                to_remove.append(op_id)
            elif operation.status == OperationStatus.PENDING and operation.is_expired:
                # Mark as timeout first
                operation.status = OperationStatus.TIMEOUT
                operation.completion_time = current_time * 1000
                to_remove.append(op_id)

        # Remove operations
        for op_id in to_remove:
            del self.operations[op_id]

        # If still over limit, remove oldest completed operations
        if len(self.operations) > self.max_operations:
            completed_ops = [
                (op_id, op)
                for op_id, op in self.operations.items()
                if op.status == OperationStatus.COMPLETED
            ]
            completed_ops.sort(key=lambda x: x[1].completion_time or 0)

            excess = len(self.operations) - self.max_operations
            for op_id, _ in completed_ops[:excess]:
                del self.operations[op_id]

        removed_count = initial_count - len(self.operations)
        if removed_count > 0:
            logger.info(f"Cleaned up {removed_count} expired operations")

        self.last_cleanup = current_time

    def _maybe_cleanup(self) -> None:
        """Maybe perform cleanup if needed."""
        if len(self.operations) > self.max_operations * 0.8:
            self.cleanup_expired_operations()


# Global operation manager instance
_operation_manager = None


def get_operation_manager() -> OperationManager:
    """Get the global operation manager instance."""
    global _operation_manager
    if _operation_manager is None:
        _operation_manager = OperationManager()
    return _operation_manager


# Convenience functions for external use
def store_pending_operation(
    entity_id: str,
    action: str,
    service_domain: str,
    service_name: str,
    service_data: dict[str, Any],
    expected_state: dict[str, Any] | None = None,
    timeout_ms: int = 10000,
) -> str:
    """Store a new pending operation."""
    manager = get_operation_manager()
    return manager.create_operation(
        entity_id,
        action,
        service_domain,
        service_name,
        service_data,
        expected_state,
        timeout_ms,
    )


def get_operation_from_memory(operation_id: str) -> DeviceOperation | None:
    """Get operation from memory by ID."""
    manager = get_operation_manager()
    return manager.get_operation(operation_id)


def update_pending_operations(entity_id: str, new_state: dict[str, Any]) -> list[str]:
    """Update pending operations based on state change."""
    manager = get_operation_manager()
    return manager.process_state_change(entity_id, new_state)


def get_pending_operations() -> dict[str, DeviceOperation]:
    """Get all pending operations."""
    manager = get_operation_manager()
    return {
        op_id: op
        for op_id, op in manager.operations.items()
        if op.status == OperationStatus.PENDING and not op.is_expired
    }
