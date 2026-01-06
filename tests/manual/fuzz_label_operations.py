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

Note:
    This is a placeholder implementation. The comprehensive E2E test in
    test_label_operations.py::TestRegressionIssue396 provides full validation.
    This manual script can be extended for longer stress tests (1000+ operations)
    and custom scenarios.
"""

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    """Main fuzzing entry point."""
    import argparse

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

    logger.info("⚠️  PLACEHOLDER IMPLEMENTATION")
    logger.info("")
    logger.info("This is a simplified placeholder. For comprehensive testing:")
    logger.info("  Run: pytest tests/src/e2e/workflows/labels/test_label_operations.py")
    logger.info("       -k TestRegressionIssue396")
    logger.info("")
    logger.info("The E2E test performs:")
    logger.info("  - 15+ rapid operations (add/remove/set cycles)")
    logger.info("  - Registry health validation")
    logger.info("  - Corruption detection")
    logger.info("")

    # Get credentials (even though we're not using them yet)
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

    logger.info("Expected behavior with ha_manage_entity_labels:")
    logger.info("  ✅ No corruption after 100+ operations")
    logger.info("  ✅ All health checks pass")
    logger.info("  ✅ Registry remains accessible")
    logger.info("")

    logger.info("Expected behavior with old ha_assign_label:")
    logger.info("  ❌ Corruption after 5-10 operations")
    logger.info("  ❌ Health checks fail")
    logger.info("  ❌ UI becomes inaccessible")
    logger.info("")

    logger.info("=" * 80)
    logger.info("To extend this script:")
    logger.info("  1. Import ha_mcp.client.websocket_client")
    logger.info("  2. Create test labels via label_registry/create")
    logger.info("  3. Find entities via REST API get_states()")
    logger.info("  4. Perform operations via entity_registry/update")
    logger.info("  5. Check health via label_registry/list")
    logger.info("  6. Report results and cleanup")
    logger.info("=" * 80)
    logger.info("")

    logger.info("✅ Placeholder test complete (no actual operations performed)")
    logger.info("")

    return 0


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
