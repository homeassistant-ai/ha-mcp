"""Per-radio handler modules for the ``ha_manage_radio`` tool.

Each radio (Z-Wave, Zigbee/ZHA, Matter, Thread) gets its own handler module so
no single file spans every protocol (keeps modules focused per AGENTS.md). The
``tools_radio`` module wires them together behind one MCP tool.
"""
