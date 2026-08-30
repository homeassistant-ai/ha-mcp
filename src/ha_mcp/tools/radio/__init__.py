"""Per-radio handler modules for the ``ha_manage_radio`` tool.

Each radio (Z-Wave, Zigbee/ZHA, Matter, Thread) gets its own handler module so
no single file spans every protocol (see `.gemini/styleguide.md` § Tool
Consolidation and Module Size). The ``tools_radio`` module wires them
together behind one MCP tool.
"""
