"""
Label management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant labels, as well as managing label assignments for entities.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import coerce_bool_param, parse_string_list_param

logger = logging.getLogger(__name__)


def register_label_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant label management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["label"], "title": "Get Label"})
    @log_tool_usage
    async def ha_config_get_label(
        label_id: Annotated[
            str | None,
            Field(
                description="ID of the label to retrieve. If omitted, lists all labels.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get label info - list all labels or get a specific one by ID.

        Without a label_id: Lists all Home Assistant labels with their configurations.
        With a label_id: Returns configuration for that specific label.

        LABEL PROPERTIES:
        - ID (label_id), Name
        - Color (optional), Icon (optional), Description (optional)

        EXAMPLES:
        - List all labels: ha_config_get_label()
        - Get specific label: ha_config_get_label("my_label_id")

        Use ha_config_set_label() to create or update labels.
        Use ha_manage_entity_labels() to manage label assignments for entities.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/label_registry/list",
            }

            result = await client.send_websocket_message(message)

            if not result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to get labels: {result.get('error', 'Unknown error')}",
                    "label_id": label_id,
                }

            labels = result.get("result", [])

            # List mode - return all labels
            if label_id is None:
                return {
                    "success": True,
                    "count": len(labels),
                    "labels": labels,
                    "message": f"Found {len(labels)} label(s)",
                }

            # Get mode - find specific label
            label = next(
                (lbl for lbl in labels if lbl.get("label_id") == label_id), None
            )

            if label:
                return {
                    "success": True,
                    "label_id": label_id,
                    "label": label,
                    "message": f"Found label: {label.get('name', label_id)}",
                }
            else:
                available_ids = [lbl.get("label_id") for lbl in labels[:10]]
                return {
                    "success": False,
                    "error": f"Label not found: {label_id}",
                    "label_id": label_id,
                    "available_label_ids": available_ids,
                    "suggestion": "Use ha_config_get_label() without label_id to see all labels",
                }

        except Exception as e:
            logger.error(f"Error getting labels: {e}")
            return {
                "success": False,
                "error": f"Failed to get labels: {str(e)}",
                "label_id": label_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["label"], "title": "Create or Update Label"})
    @log_tool_usage
    async def ha_config_set_label(
        name: Annotated[str, Field(description="Display name for the label")],
        label_id: Annotated[
            str | None,
            Field(
                description="Label ID for updates. If not provided, creates a new label.",
                default=None,
            ),
        ] = None,
        color: Annotated[
            str | None,
            Field(
                description="Color for the label (e.g., 'red', 'blue', 'green', or hex like '#FF5733')",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:tag', 'mdi:label')",
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="Description of the label's purpose",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant label.

        Creates a new label if label_id is not provided, or updates an existing label if label_id is provided.

        Labels are a flexible tagging system that can be applied to entities,
        devices, and areas for organization and automation purposes.

        EXAMPLES:
        - Create simple label: ha_config_set_label("Critical")
        - Create colored label: ha_config_set_label("Outdoor", color="green")
        - Create label with icon: ha_config_set_label("Battery Powered", icon="mdi:battery")
        - Create full label: ha_config_set_label("Security", color="red", icon="mdi:shield", description="Security-related devices")
        - Update label: ha_config_set_label("Updated Name", label_id="my_label_id", color="blue")

        After creating a label, use ha_manage_entity_labels() to assign it to entities.
        """
        try:
            # Determine if this is a create or update
            action = "update" if label_id else "create"

            message: dict[str, Any] = {
                "type": f"config/label_registry/{action}",
                "name": name,
            }

            if action == "update":
                message["label_id"] = label_id
                # Note: name is always provided as it's a required parameter
                # The validation of at least one field is satisfied by name being required

            # Add optional fields only if they are explicitly provided (not None)
            if color is not None:
                message["color"] = color
            if icon is not None:
                message["icon"] = icon
            if description is not None:
                message["description"] = description

            result = await client.send_websocket_message(message)

            if result.get("success"):
                label_data = result.get("result", {})
                action_past = "created" if action == "create" else "updated"
                return {
                    "success": True,
                    "label_id": label_data.get("label_id"),
                    "label_data": label_data,
                    "message": f"Successfully {action_past} label: {name}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to {action} label: {result.get('error', 'Unknown error')}",
                    "name": name,
                }

        except Exception as e:
            logger.error(f"Error setting label: {e}")
            return {
                "success": False,
                "error": f"Failed to set label: {str(e)}",
                "name": name,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the label name is valid",
                    "For updates, verify the label_id exists using ha_config_get_label()",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["label"], "title": "Remove Label"})
    @log_tool_usage
    async def ha_config_remove_label(
        label_id: Annotated[
            str,
            Field(description="ID of the label to delete"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant label.

        Removes the label from the label registry. This will also remove the label
        from all entities, devices, and areas that have it assigned.

        EXAMPLES:
        - Delete label: ha_config_remove_label("my_label_id")

        Use ha_config_get_label() to find label IDs.

        **WARNING:** Deleting a label will remove it from all assigned entities.
        This action cannot be undone.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/label_registry/delete",
                "label_id": label_id,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "label_id": label_id,
                    "message": f"Successfully deleted label: {label_id}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to delete label: {result.get('error', 'Unknown error')}",
                    "label_id": label_id,
                }

        except Exception as e:
            logger.error(f"Error deleting label: {e}")
            return {
                "success": False,
                "error": f"Failed to delete label: {str(e)}",
                "label_id": label_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the label_id exists using ha_config_get_label()",
                ],
            }

    # Helper functions for label management
    async def _get_entity_labels(entity_id: str) -> list[str]:
        """
        Fetch current labels for an entity from entity registry.

        Args:
            entity_id: Entity to query

        Returns:
            List of current label IDs (empty list if none)

        Raises:
            ValueError: If entity not found or API error
        """
        message: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }

        result = await client.send_websocket_message(message)

        if not result.get("success"):
            error_msg = result.get("error", {}).get("message", "Unknown error")
            raise ValueError(f"Entity not found or API error: {entity_id} - {error_msg}")

        entity_entry = result.get("result", {})
        return entity_entry.get("labels", [])

    async def _set_labels_single(entity_id: str, labels: list[str]) -> dict[str, Any]:
        """
        Set labels for a single entity (replaces all existing labels).

        Args:
            entity_id: Entity to update
            labels: Complete list of label IDs to set

        Returns:
            Operation result dictionary
        """
        try:
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "labels": labels,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                entity_entry = result.get("result", {}).get("entity_entry", {})
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "labels": labels,
                    "entity_data": entity_entry,
                    "message": f"Successfully set {len(labels)} label(s) for {entity_id}",
                }
            else:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Failed to set labels: {result.get('error', 'Unknown error')}",
                }
        except Exception as e:
            logger.error(f"Error setting labels for {entity_id}: {e}")
            return {
                "success": False,
                "entity_id": entity_id,
                "error": f"Failed to set labels: {str(e)}",
            }

    async def _add_labels_single(entity_id: str, labels: list[str]) -> dict[str, Any]:
        """
        Add labels to a single entity (preserves existing labels).

        Args:
            entity_id: Entity to update
            labels: Label IDs to add

        Returns:
            Operation result dictionary
        """
        try:
            # Fetch current labels
            current_labels = await _get_entity_labels(entity_id)

            # Merge and deduplicate
            final_labels = list(set(current_labels + labels))

            # Update entity registry
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "labels": final_labels,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                entity_entry = result.get("result", {}).get("entity_entry", {})
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "labels": final_labels,
                    "entity_data": entity_entry,
                    "message": f"Successfully updated labels for {entity_id}. It now has {len(final_labels)} label(s).",
                }
            else:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Failed to add labels: {result.get('error', 'Unknown error')}",
                }
        except ValueError as e:
            # Entity not found
            return {
                "success": False,
                "entity_id": entity_id,
                "error": str(e),
            }
        except Exception as e:
            logger.error(f"Error adding labels to {entity_id}: {e}")
            return {
                "success": False,
                "entity_id": entity_id,
                "error": f"Failed to add labels: {str(e)}",
            }

    async def _remove_labels_single(entity_id: str, labels: list[str]) -> dict[str, Any]:
        """
        Remove labels from a single entity (preserves remaining labels).

        Args:
            entity_id: Entity to update
            labels: Label IDs to remove

        Returns:
            Operation result dictionary
        """
        try:
            # Fetch current labels
            current_labels = await _get_entity_labels(entity_id)

            # Remove specified labels (convert to set for O(1) lookup)
            final_labels = [lbl for lbl in current_labels if lbl not in set(labels)]

            # Update entity registry
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "labels": final_labels,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                entity_entry = result.get("result", {}).get("entity_entry", {})
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "labels": final_labels,
                    "entity_data": entity_entry,
                    "message": f"Successfully updated labels for {entity_id}. It now has {len(final_labels)} label(s).",
                }
            else:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Failed to remove labels: {result.get('error', 'Unknown error')}",
                }
        except ValueError as e:
            # Entity not found
            return {
                "success": False,
                "entity_id": entity_id,
                "error": str(e),
            }
        except Exception as e:
            logger.error(f"Error removing labels from {entity_id}: {e}")
            return {
                "success": False,
                "entity_id": entity_id,
                "error": f"Failed to remove labels: {str(e)}",
            }

    async def _process_entity_safe(
        entity_id: str, operation: str, labels: list[str]
    ) -> dict[str, Any]:
        """
        Process single entity with full error handling.

        Errors are isolated - one entity failure doesn't stop others in bulk operations.

        Args:
            entity_id: Entity to process
            operation: Operation type ('add', 'remove', or 'set')
            labels: Label IDs to apply

        Returns:
            Operation result dictionary
        """
        try:
            if operation == "add":
                return await _add_labels_single(entity_id, labels)
            elif operation == "remove":
                return await _remove_labels_single(entity_id, labels)
            else:  # set
                return await _set_labels_single(entity_id, labels)
        except Exception as e:
            logger.error(f"Error processing {entity_id} with operation '{operation}': {e}")
            return {
                "entity_id": entity_id,
                "success": False,
                "error": f"Failed to {operation} labels: {str(e)}",
            }

    async def _process_bulk_operation(
        entity_ids: list[str], operation: str, labels: list[str], parallel: bool
    ) -> dict[str, Any]:
        """
        Process multiple entities with error isolation.

        Args:
            entity_ids: List of entities to process
            operation: Operation type ('add', 'remove', or 'set')
            labels: Label IDs to apply
            parallel: Execute in parallel if True, sequential if False

        Returns:
            Bulk operation result dictionary
        """
        if parallel:
            # Execute all entities in parallel using asyncio.gather
            tasks = [_process_entity_safe(entity, operation, labels) for entity in entity_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Convert exceptions to error dicts
            results = [
                (
                    {
                        "entity_id": entity_ids[i],
                        "success": False,
                        "error": f"Exception: {str(r)}",
                    }
                    if isinstance(r, Exception)
                    else r
                )
                for i, r in enumerate(results)
            ]
        else:
            # Execute sequentially
            results = []
            for entity in entity_ids:
                result = await _process_entity_safe(entity, operation, labels)
                results.append(result)

        # Count successes/failures
        successful = len([r for r in results if isinstance(r, dict) and r.get("success")])
        failed = len(results) - successful

        return {
            "mode": "bulk",
            "operation": operation,
            "total_operations": len(entity_ids),
            "successful": successful,
            "failed": failed,
            "execution_mode": "parallel" if parallel else "sequential",
            "results": results,
        }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["label", "entity"],
            "title": "Manage Entity Labels",
        }
    )
    @log_tool_usage
    async def ha_manage_entity_labels(
        entity_id: Annotated[
            str | list[str],
            Field(
                description="Entity ID(s) to manage labels for. Single string for one entity, "
                "list of strings for bulk operations (e.g., 'light.bedroom' or ['light.bedroom', 'switch.kitchen'])"
            ),
        ],
        operation: Annotated[
            Literal["add", "remove", "set"],
            Field(
                description="Label operation: 'add' appends labels (preserves existing), "
                "'remove' removes specified labels (preserves others), "
                "'set' replaces all labels"
            ),
        ],
        labels: Annotated[
            str | list[str],
            Field(
                description="Label ID(s) to apply. Can be a single label ID string, "
                "a list of label IDs, or a JSON array string (e.g., '[\"label1\", \"label2\"]')"
            ),
        ],
        parallel: Annotated[
            bool,
            Field(
                description="Execute bulk operations in parallel (default: True). Only applies when entity_id is a list.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Manage label assignments for entities with add, remove, or set operations.

        This tool provides three operations for managing entity labels:
        - **add**: Append labels to existing labels (preserves all current labels)
        - **remove**: Remove specific labels (preserves remaining labels)
        - **set**: Replace all labels with provided list (same as old ha_assign_label)

        Supports both single entity and bulk operations. For bulk, entities can be
        processed in parallel (default) or sequentially.

        EXAMPLES:
        - Add labels: ha_manage_entity_labels("light.bedroom", "add", ["outdoor", "smart"])
        - Remove label: ha_manage_entity_labels("light.bedroom", "remove", "old_label")
        - Set all labels: ha_manage_entity_labels("light.bedroom", "set", ["new1", "new2"])
        - Clear all labels: ha_manage_entity_labels("light.bedroom", "set", [])
        - Bulk add: ha_manage_entity_labels(["light.bedroom", "light.kitchen"], "add", "evening")
        - Bulk parallel: ha_manage_entity_labels(["light.1", "light.2", "light.3"], "set", ["outdoor"], parallel=True)

        Use ha_config_get_label() to find available label IDs.
        Use ha_search_entities() to find entity IDs.

        **OPERATION DETAILS:**
        - add: Fetches current labels, merges with new (2 API calls)
        - remove: Fetches current labels, removes specified (2 API calls)
        - set: Direct replacement (1 API call - fastest)

        **BULK OPERATIONS:**
        - parallel=True: Process all entities simultaneously (faster, default)
        - parallel=False: Process entities one by one (useful for debugging)
        - Failures are isolated: one entity error doesn't stop others
        """
        try:
            # Validate and parse entity_id parameter
            entity_ids: list[str]
            is_bulk: bool

            if isinstance(entity_id, str):
                entity_ids = [entity_id]
                is_bulk = False
            elif isinstance(entity_id, list):
                if not entity_id:
                    return {
                        "success": False,
                        "error": "entity_id list cannot be empty",
                        "suggestions": ["Provide at least one entity_id"],
                    }
                if not all(isinstance(e, str) for e in entity_id):
                    return {
                        "success": False,
                        "error": "All entity_id values must be strings",
                    }
                entity_ids = entity_id
                is_bulk = True
            else:
                return {
                    "success": False,
                    "error": f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
                }

            # Parse labels parameter with backward compatibility for plain strings
            if isinstance(labels, str):
                # Try JSON parsing first (for JSON array strings)
                try:
                    parsed_labels = parse_string_list_param(labels, "labels")
                    if parsed_labels is None:
                        parsed_labels = []
                except ValueError:
                    # Plain string - wrap in list for backward compatibility
                    parsed_labels = [labels]
            elif isinstance(labels, list):
                # Validate all items are strings
                if not all(isinstance(item, str) for item in labels):
                    return {
                        "success": False,
                        "error": "All labels must be strings",
                        "suggestions": [
                            "Provide labels as a string, list of strings, or JSON array",
                            "Example: labels='label_id' or labels=['label1', 'label2']",
                        ],
                    }
                parsed_labels = labels
            elif labels is None:
                parsed_labels = []
            else:
                return {
                    "success": False,
                    "error": f"labels must be string or list, got {type(labels).__name__}",
                    "suggestions": [
                        "Provide labels as a string, list of strings, or JSON array",
                        "Example: labels='label_id' or labels=['label1', 'label2']",
                    ],
                }

            # Allow empty list for set operation (clears labels)
            if len(parsed_labels) == 0 and operation in ["add", "remove"]:
                # Empty list for add/remove is no-op but valid
                pass
            # Deduplicate labels
            parsed_labels = list(set(parsed_labels))

            # Validate that all label IDs exist before attempting assignment
            if parsed_labels:
                try:
                    # Fetch all available labels via WebSocket
                    message: dict[str, Any] = {
                        "type": "config/label_registry/list",
                    }
                    label_list_result = await client.send_websocket_message(message)

                    if not label_list_result.get("success"):
                        return {
                            "success": False,
                            "error": "Failed to fetch available labels for validation",
                            "suggestions": ["Check Home Assistant connection"],
                        }

                    available_labels = label_list_result.get("result", [])
                    available_label_ids = {lbl['label_id'] for lbl in available_labels if isinstance(lbl, dict) and 'label_id' in lbl}

                    # Check for non-existent labels
                    invalid_labels = [lbl for lbl in parsed_labels if lbl not in available_label_ids]

                    if invalid_labels:
                        return {
                            "success": False,
                            "error": f"Labels do not exist: {', '.join(invalid_labels)}",
                            "invalid_labels": invalid_labels,
                            "suggestions": [
                                "Use ha_config_get_label() to see all available labels",
                                "Create missing labels with ha_config_set_label()",
                            ],
                        }
                except Exception as e:
                    logger.error(f"Error validating label IDs: {e}")
                    return {
                        "success": False,
                        "error": f"Failed to validate label IDs: {str(e)}",
                        "suggestions": ["Check Home Assistant connection"],
                    }

            # Coerce parallel parameter
            parallel_bool = coerce_bool_param(parallel, "parallel", default=True)
            if parallel_bool is None:
                parallel_bool = True

            # Validate operation
            if operation not in ["add", "remove", "set"]:
                return {
                    "success": False,
                    "error": f"Invalid operation: {operation}",
                    "suggestions": ["Use 'add', 'remove', or 'set'"],
                }

            # Route to appropriate handler
            if is_bulk:
                return await _process_bulk_operation(entity_ids, operation, parsed_labels, parallel_bool)
            else:
                # Single entity operation
                result = await _process_entity_safe(entity_ids[0], operation, parsed_labels)
                # Add mode indicator for single operations
                if result.get("success"):
                    result["mode"] = "single"
                    result["operation"] = operation
                return result

        except Exception as e:
            logger.error(f"Error in ha_manage_entity_labels: {e}")
            return {
                "success": False,
                "error": f"Failed to manage labels: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify entity_id exists using ha_search_entities()",
                    "Verify label IDs exist using ha_config_get_label()",
                ],
            }
