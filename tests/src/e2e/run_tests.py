#!/usr/bin/env python3
"""
E2E Test Runner

Simple test runner script for Home Assistant MCP Server E2E tests.
"""

import subprocess
import sys
from pathlib import Path


def main():
    """Run E2E tests with appropriate arguments."""
    test_dir = Path(__file__).parent

    # Default pytest arguments
    args = ["pytest", str(test_dir), "-v", "--tb=short", "-ra"]

    # Add command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "fast":
            args.extend(["-m", "not slow"])
        elif sys.argv[1] == "automation":
            args.append(str(test_dir / "workflows/automation/"))
        elif sys.argv[1] == "device":
            args.append(str(test_dir / "workflows/device_control/"))
        elif sys.argv[1] == "workflows":
            args.append(str(test_dir / "workflows/"))
        elif sys.argv[1] == "basic":
            args.append(str(test_dir / "basic/"))
        elif sys.argv[1] == "scripts":
            args.append(str(test_dir / "workflows/scripts/"))
        elif sys.argv[1] == "convenience":
            args.append(str(test_dir / "workflows/convenience/"))
        elif sys.argv[1] == "error":
            args.append(str(test_dir / "error_handling/"))
        else:
            # Pass through other arguments
            args.extend(sys.argv[1:])

    print(f"🧪 Running E2E tests: {' '.join(args)}")

    # Run pytest
    result = subprocess.run(args)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
