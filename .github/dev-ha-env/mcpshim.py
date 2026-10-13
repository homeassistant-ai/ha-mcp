"""Drive a dev environment's ha-mcp server one tool call at a time.

An example shim for an agent whose MCP client cannot add a server
mid-session; modify it as needed. It talks to the `MCP:` URL only, so every
action goes through ha-mcp's own tools. Standard library only.

    python3 mcpshim.py <urls-file> tools
    python3 mcpshim.py <urls-file> describe <tool>
    python3 mcpshim.py <urls-file> call <tool> '<json arguments>'

<urls-file> is the decrypted dev-ha-env-urls file. The MCP session is kept
beside it and reopened when the server restarts.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class Shim:
    def __init__(self, urls_file: Path) -> None:
        self.url = next(
            line.split(":", 1)[1].strip()
            for line in urls_file.read_text().splitlines()
            if line.startswith("MCP:")
        )
        self.session_file = urls_file.with_name(urls_file.name + ".mcp-session")

    def _post(
        self, body: dict[str, Any], session: str | None = None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        request = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(request, timeout=600) as response:
            new_session = response.headers.get("Mcp-Session-Id")
            raw = response.read().decode()
        if raw.lstrip().startswith("{"):
            return new_session, [json.loads(raw)]
        # Streamable HTTP may answer as server-sent events.
        return new_session, [
            json.loads(line[5:])
            for line in raw.splitlines()
            if line.startswith("data:") and line[5:].strip()
        ]

    def _session(self) -> str | None:
        if self.session_file.exists():
            return self.session_file.read_text().strip() or None
        session, _ = self._post(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "dev-ha-env-shim", "version": "1"},
                },
            }
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        self.session_file.write_text(session or "")
        return session

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            _, messages = self._post(body, self._session())
        except urllib.error.HTTPError as err:
            # A restarted server no longer knows the session: open a new one.
            if err.code not in (400, 404) or not self.session_file.exists():
                raise
            self.session_file.unlink()
            _, messages = self._post(body, self._session())
        return next((m for m in messages if "result" in m or "error" in m), {})

    def tools(self) -> list[dict[str, Any]]:
        return self.rpc("tools/list", {})["result"]["tools"]


def main(argv: list[str]) -> None:
    command = argv[2] if len(argv) > 2 else ""
    if command not in ("tools", "describe", "call") or (
        command != "tools" and len(argv) < 4
    ):
        sys.exit(__doc__)
    shim = Shim(Path(argv[1]))
    if command == "tools":
        for tool in shim.tools():
            summary = (tool.get("description") or "").strip().splitlines()
            print(f"{tool['name']}: {summary[0] if summary else ''}")
    elif command == "describe":
        tool = next((t for t in shim.tools() if t["name"] == argv[3]), None)
        if tool is None:
            sys.exit(f"No tool named {argv[3]!r}; list them with the tools command.")
        print(tool.get("description", ""))
        print(json.dumps(tool["inputSchema"], indent=1))
    else:
        arguments = json.loads(argv[4]) if len(argv) > 4 else {}
        reply = shim.rpc("tools/call", {"name": argv[3], "arguments": arguments})
        if "error" in reply:
            print("JSON-RPC error:", json.dumps(reply["error"], indent=1))
            return
        result = reply["result"]
        print("isError:", result.get("isError"))
        for item in result.get("content", []):
            print(item["text"] if item.get("type") == "text" else f"[{item['type']}]")


if __name__ == "__main__":
    main(sys.argv)
