#!/usr/bin/env python3
"""
Fuzzing script for label operations to reproduce Issue #396.

This script performs hundreds of rapid label operations on entities to test for
entity registry corruption. The bug (Issue #396) manifested when 5+ rapid label
operations would corrupt the entity registry, making the label UI inaccessible.

Usage:
    python fuzz_label_operations.py [--operations N] [--entities M] [--labels K]

Requirements:
    - Home Assistant instance running (local Docker or remote)
    - Set HOMEASSISTANT_URL and HOMEASSISTANT_TOKEN environment variables

The script will:
1. Create test labels
2. Find test entities (light entities)
3. Perform rapid label operations (add/remove/set cycles)
4. Validate entity registry integrity every 20 operations
5. Report corruption or success

Exit codes:
    0 - No corruption detected (success)
    1 - Corruption detected or errors encountered (failure)
"""

import asyncio
import logging
import os
import random
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    """Main fuzzing entry point."""
    import argparse

    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

    parser = argparse.ArgumentParser(
        description="Fuzz label operations to detect entity registry corruption (Issue #396)"
    )
    parser.add_argument(
        "--operations",
        type=int,
        default=100,
        help="Number of operations to perform (default: 100)",
    )
    parser.add_argument(
        "--entities",
        type=int,
        default=5,
        help="Number of entities to use (default: 5)",
    )
    parser.add_argument(
        "--labels",
        type=int,
        default=10,
        help="Number of labels to create (default: 10)",
    )
    parser.add_argument(
        "--url",
        help="Home Assistant URL (default: from HOMEASSISTANT_URL env)",
    )
    parser.add_argument(
        "--token",
        help="Home Assistant token (default: from HOMEASSISTANT_TOKEN env)",
    )

    args = parser.parse_args()

    logger.info("=" * 80)
    logger.info("LABEL OPERATION FUZZING - Issue #396 Regression Test")
    logger.info("=" * 80)
    logger.info("Configuration:")
    logger.info(f"  Operations: {args.operations}")
    logger.info(f"  Entities: {args.entities}")
    logger.info(f"  Labels: {args.labels}")
    logger.info("=" * 80)
    logger.info("")

    # Get credentials
    url = args.url or os.getenv("HOMEASSISTANT_URL")
    token = args.token or os.getenv("HOMEASSISTANT_TOKEN")

    if not url or not token:
        logger.error("❌ Error: HOMEASSISTANT_URL and HOMEASSISTANT_TOKEN must be set")
        logger.error("")
        logger.error("Set environment variables:")
        logger.error("  export HOMEASSISTANT_URL=http://localhost:8123")
        logger.error("  export HOMEASSISTANT_TOKEN=your_long_lived_access_token")
        logger.error("")
        return 1

    logger.info(f"Target: {url}")
    logger.info("")

    # Initialize clients
    rest_client = HomeAssistantClient(url, token)
    ws_client = HomeAssistantWebSocketClient(url, token)

    created_labels = []
    test_entities = []

    try:
        # Connect WebSocket
        await ws_client.connect()
        logger.info("✅ Connected to Home Assistant")
        logger.info("")

        # Step 1: Create test labels
        logger.info(f"Creating {args.labels} test labels...")
        for i in range(args.labels):
            result = await ws_client.send_command(
                "config/label_registry/create",
                name=f"fuzz_test_label_{i+1}",
                icon="mdi:test-tube",
            )
            if result.get("success"):
                label_id = result["result"]["label_id"]
                created_labels.append(label_id)
                logger.info(f"  Created label {i+1}/{args.labels}: {label_id}")
            else:
                logger.warning(f"  Failed to create label {i+1}: {result}")

        if len(created_labels) < args.labels:
            logger.warning(
                f"⚠️  Only created {len(created_labels)}/{args.labels} labels"
            )
        logger.info("")

        # Step 2: Find test entities
        logger.info(f"Finding {args.entities} test entities...")
        states = await rest_client.get_states()
        light_entities = [s["entity_id"] for s in states if s["entity_id"].startswith("light.")]

        if len(light_entities) < args.entities:
            logger.warning(
                f"⚠️  Only found {len(light_entities)} light entities, requested {args.entities}"
            )
            test_entities = light_entities
        else:
            test_entities = light_entities[: args.entities]

        for i, entity_id in enumerate(test_entities, 1):
            logger.info(f"  Using entity {i}/{len(test_entities)}: {entity_id}")
        logger.info("")

        # Step 3: Perform fuzzing operations
        logger.info(f"Starting fuzzing operations ({args.operations} operations)...")
        start_time = time.time()

        successful_ops = 0
        failed_ops = 0

        for op_num in range(1, args.operations + 1):
            # Pick random entity and operation
            entity_id = random.choice(test_entities)
            operation_type = random.choice(["add", "remove", "set"])

            # Pick random labels
            if operation_type == "add":
                label_ids = random.sample(created_labels, k=random.randint(1, min(3, len(created_labels))))
            elif operation_type == "remove":
                label_ids = random.sample(created_labels, k=random.randint(1, min(2, len(created_labels))))
            else:  # set
                num_labels = random.randint(0, min(5, len(created_labels)))
                label_ids = random.sample(created_labels, k=num_labels) if num_labels > 0 else []

            try:
                # Get current labels
                get_result = await ws_client.send_command(
                    "config/entity_registry/get",
                    entity_id=entity_id,
                )

                if not get_result.get("success"):
                    logger.warning(f"  [{op_num:4d}/{args.operations}] Failed to get entity {entity_id}")
                    failed_ops += 1
                    continue

                current_labels = get_result["result"].get("labels", [])

                # Calculate new labels based on operation
                if operation_type == "add":
                    new_labels = list(set(current_labels + label_ids))
                elif operation_type == "remove":
                    new_labels = [lbl for lbl in current_labels if lbl not in label_ids]
                else:  # set
                    new_labels = label_ids

                # Update entity
                update_result = await ws_client.send_command(
                    "config/entity_registry/update",
                    entity_id=entity_id,
                    labels=new_labels,
                )

                if update_result.get("success"):
                    logger.info(
                        f"  [{op_num:4d}/{args.operations}] {operation_type:6s} on {entity_id:30s} "
                        f"with {len(label_ids)} label(s) ✅"
                    )
                    successful_ops += 1
                else:
                    logger.warning(
                        f"  [{op_num:4d}/{args.operations}] {operation_type:6s} on {entity_id:30s} FAILED"
                    )
                    failed_ops += 1

            except Exception as e:
                logger.error(
                    f"  [{op_num:4d}/{args.operations}] Exception during {operation_type}: {e}"
                )
                failed_ops += 1

            # Health check every 20 operations
            if op_num % 20 == 0:
                logger.info("")
                logger.info(f"  Checking registry health after {op_num} operations...")

                try:
                    # Try to list labels
                    list_result = await ws_client.send_command("config/label_registry/list")
                    if not list_result.get("success"):
                        logger.error("  ❌ CORRUPTION DETECTED: Cannot list labels!")
                        return 1

                    # Try to get an entity
                    test_entity = test_entities[0]
                    get_result = await ws_client.send_command(
                        "config/entity_registry/get",
                        entity_id=test_entity,
                    )
                    if not get_result.get("success"):
                        logger.error(f"  ❌ CORRUPTION DETECTED: Cannot get entity {test_entity}!")
                        return 1

                    logger.info("  ✅ Registry health check passed")
                except Exception as e:
                    logger.error(f"  ❌ CORRUPTION DETECTED: Health check exception: {e}")
                    return 1

                logger.info("")

        elapsed = time.time() - start_time
        logger.info("")
        logger.info("=" * 80)
        logger.info("FUZZING COMPLETE")
        logger.info("=" * 80)
        logger.info(f"Total operations: {args.operations}")
        logger.info(f"Successful: {successful_ops}")
        logger.info(f"Failed: {failed_ops}")
        logger.info(f"Time elapsed: {elapsed:.2f}s")
        logger.info(f"Operations/sec: {args.operations / elapsed:.2f}")
        logger.info("")

        # Final comprehensive health check
        logger.info("Performing final comprehensive health check...")
        try:
            # List all labels
            list_result = await ws_client.send_command("config/label_registry/list")
            if not list_result.get("success"):
                logger.error("❌ FINAL HEALTH CHECK FAILED: Cannot list labels")
                return 1

            # Get all test entities
            for entity_id in test_entities:
                get_result = await ws_client.send_command(
                    "config/entity_registry/get",
                    entity_id=entity_id,
                )
                if not get_result.get("success"):
                    logger.error(f"❌ FINAL HEALTH CHECK FAILED: Cannot get entity {entity_id}")
                    return 1

            # Try one more label operation
            test_entity = test_entities[0]
            update_result = await ws_client.send_command(
                "config/entity_registry/update",
                entity_id=test_entity,
                labels=[],
            )
            if not update_result.get("success"):
                logger.error("❌ FINAL HEALTH CHECK FAILED: Cannot perform label operation")
                return 1

            logger.info("✅ FINAL HEALTH CHECK PASSED - NO CORRUPTION DETECTED")
            logger.info("=" * 80)
            return 0

        except Exception as e:
            logger.error(f"❌ FINAL HEALTH CHECK FAILED: {e}")
            return 1

    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        return 1

    finally:
        # Cleanup: Delete created labels
        logger.info("")
        logger.info("Cleaning up test labels...")
        for label_id in created_labels:
            try:
                await ws_client.send_command(
                    "config/label_registry/delete",
                    label_id=label_id,
                )
                logger.info(f"  Deleted label: {label_id}")
            except Exception as e:
                logger.warning(f"  Failed to delete label {label_id}: {e}")

        await ws_client.disconnect()
        logger.info("✅ Cleanup complete")
        logger.info("")


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("")
        logger.info("❌ Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)
