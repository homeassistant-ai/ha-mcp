"""The storage-helper types the helper tools manage.

Kept outside ``ha_mcp.tools`` so ``backup_manager`` can import it: importing the
tools package from there would close the backup_manager -> tools -> auto_backup
cycle. The component keeps the same list (``helper_collections.py``).
"""

from typing import Literal, get_args

# Managed via {type}/create and {type}/update WebSocket APIs (not Config Entry
# Flow). The helper tools' schemas spell them out as an enum. Any other value
# must be a helper flow type Core lists, checked at call time (``helper_flows``),
# except config_subentry and ha_config_list_helpers' "all".
StorageHelperType = Literal[
    "input_button",
    "input_boolean",
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
    "counter",
    "timer",
    "schedule",
    "zone",
    "person",
    "tag",
]
SIMPLE_HELPER_TYPES: frozenset[str] = frozenset(get_args(StorageHelperType))
