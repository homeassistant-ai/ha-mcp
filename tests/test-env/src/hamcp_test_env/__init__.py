"""Entry point for `hamcp-test-env`: runs tests/test_env_manager.py.

The package is installed editable from the checkout, so the runner and the
test constants it imports are found relative to this file.
"""

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[3]


def main() -> None:
    sys.path.insert(0, str(_TESTS_DIR))
    from test_env_manager import main as run

    run()
