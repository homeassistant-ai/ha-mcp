"""Drive a dev environment's ha-mcp server one MCP request at a time.

An example shim for an agent whose MCP client cannot add a server
mid-session; modify it as needed. It talks to the `MCP:` URL only, so every
action goes through ha-mcp's own tools. It is a real MCP client: it runs
the vendored FastMCP client, which negotiates the newest protocol revision
both sides speak (the stateless 2026-07-28 envelope, or the initialize
handshake with an older server), follows list pagination, and answers
elicitation requests. Run it from a synced checkout:

    uv run python .github/dev-ha-env/mcpshim.py <urls-file> tools
    uv run python .github/dev-ha-env/mcpshim.py <urls-file> describe <tool>
    uv run python .github/dev-ha-env/mcpshim.py <urls-file> call <tool> '<json>'
    uv run python .github/dev-ha-env/mcpshim.py <urls-file> resources
    uv run python .github/dev-ha-env/mcpshim.py <urls-file> read <uri>
    uv run python .github/dev-ha-env/mcpshim.py <urls-file> prompts

<urls-file> is the decrypted dev-ha-env-urls file. `call` prints the whole
CallToolResult as the client received it (content blocks, structuredContent,
isError, _meta); image and audio data are elided, or written to the
directory in MCPSHIM_SAVE_MEDIA. Progress and server log messages go to
stderr. An elicitation request is declined unless MCPSHIM_ELICIT holds the
JSON content to accept it with.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.client.elicitation import ElicitResult

_MEDIA_TYPES = ("image", "audio")


def _mcp_url(urls_file: Path) -> str:
    return next(
        line.split(":", 1)[1].strip()
        for line in urls_file.read_text().splitlines()
        if line.startswith("MCP:")
    )


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _show_media(item: dict[str, Any]) -> dict[str, Any]:
    raw = base64.b64decode(item.get("data", ""))
    digest = hashlib.sha256(raw).hexdigest()
    shown = {k: v for k, v in item.items() if k != "data"}
    shown["data"] = f"<{len(raw)} bytes, sha256 {digest}>"
    save_dir = os.environ.get("MCPSHIM_SAVE_MEDIA")
    if save_dir:
        suffix = item.get("mimeType", "").partition("/")[2] or "bin"
        path = Path(save_dir) / f"{digest[:16]}.{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        shown["saved_to"] = str(path)
    return shown


async def _log(message: Any) -> None:
    print(f"[server log {message.level}] {message.data}", file=sys.stderr)


async def _progress(progress: float, total: float | None, message: str | None) -> None:
    print(f"[progress {progress}/{total}] {message or ''}", file=sys.stderr)


async def _elicit(message: str, _type: Any, params: Any, _ctx: Any) -> Any:
    print(f"[elicitation] {message}\n{json.dumps(_dump(params))}", file=sys.stderr)
    answer = os.environ.get("MCPSHIM_ELICIT")
    if answer is None:
        return ElicitResult(action="decline")
    return ElicitResult(action="accept", content=json.loads(answer))


async def _tools(client: Client, _args: list[str]) -> None:
    for tool in await client.list_tools():
        summary = (tool.description or "").strip().splitlines()
        print(f"{tool.name}: {summary[0] if summary else ''}")


async def _describe(client: Client, args: list[str]) -> None:
    tool = next((t for t in await client.list_tools() if t.name == args[0]), None)
    if tool is None:
        sys.exit(f"No tool named {args[0]!r}; list them with the tools command.")
    print(json.dumps(_dump(tool), indent=1))


async def _call(client: Client, args: list[str]) -> None:
    arguments = json.loads(args[1]) if len(args) > 1 else {}
    result = _dump(await client.call_tool_mcp(args[0], arguments))
    result["content"] = [
        _show_media(item) if item.get("type") in _MEDIA_TYPES else item
        for item in result.get("content", [])
    ]
    print(json.dumps(result, indent=1, ensure_ascii=False))


async def _resources(client: Client, _args: list[str]) -> None:
    for resource in await client.list_resources():
        print(f"{resource.uri}: {resource.name}")


async def _read(client: Client, args: list[str]) -> None:
    for part in await client.read_resource(args[0]):
        print(getattr(part, "text", None) or json.dumps(_dump(part)))


async def _prompts(client: Client, _args: list[str]) -> None:
    for prompt in await client.list_prompts():
        print(f"{prompt.name}: {prompt.description or ''}")


_COMMANDS = {
    "tools": _tools,
    "describe": _describe,
    "call": _call,
    "resources": _resources,
    "read": _read,
    "prompts": _prompts,
}
_NEEDS_ARG = {"describe", "call", "read"}


async def _run(url: str, command: str, args: list[str]) -> None:
    client = Client(
        url,
        name="dev-ha-env-shim",
        log_handler=_log,
        progress_handler=_progress,
        elicitation_handler=_elicit,
        timeout=600,
    )
    async with client:
        print(f"[protocol {client.protocol_version}]", file=sys.stderr)
        await _COMMANDS[command](client, args)


def main(argv: list[str]) -> None:
    command = argv[2] if len(argv) > 2 else ""
    if command not in _COMMANDS or (command in _NEEDS_ARG and len(argv) < 4):
        sys.exit(__doc__)
    asyncio.run(_run(_mcp_url(Path(argv[1])), command, argv[3:]))


if __name__ == "__main__":
    main(sys.argv)
