# Reporter dashboard fixture

`reporter-media.yaml` is the sanitized attachment from
[HA-MCP issue #2367](https://github.com/homeassistant-ai/ha-mcp/issues/2367):
[original YAML](https://github.com/user-attachments/files/31871296/dashboard-media-sanitized.yaml).
Its SHA-256 is `2ccc763882e7abe4dd74b4a55bbc293db23e7a647f20696f0e2b23106c160329`.

`reporter-transform.txt` preserves the reporter's large section-append transform.
`reporter-section.json` contains that same literal section for the structured
patch test. Custom-card configuration and templates are preserved; these tests
exercise configuration storage and editing, not frontend rendering.

Passing these cases establishes functional compatibility with this payload. It
does not reproduce or establish a fix for the intermittent Desktop timeout.
