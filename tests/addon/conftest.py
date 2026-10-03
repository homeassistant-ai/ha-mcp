"""Shared setup for the app tests."""

import sys
from pathlib import Path

# The app image copies start.py and its sibling modules to ``/``, so
# start.py imports them as top-level modules.
sys.path.insert(0, str(Path(__file__).parents[2] / "homeassistant-addon"))
