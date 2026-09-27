"""Fresh, MCP-only Codex CLI phases for the BAT runner.

The caller supplies a dedicated CODEX_HOME containing auth.json. Each scenario
gets a private copy so repository configuration cannot add tools or MCP servers.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


def auth_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if not configured:
        raise ValueError("Codex BAT requires an explicit CODEX_HOME/auth.json")
    return Path(configured) / "auth.json"


def prepare_home(mcp_config: dict) -> tuple[Path, Path]:
    source = auth_path()
    if not source.is_file():
        raise ValueError("Codex BAT requires CODEX_HOME/auth.json")
    root = Path(tempfile.mkdtemp(prefix="codex_bat_"))
    try:
        root.chmod(0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        shutil.copyfile(source, home / "auth.json")
        (home / "auth.json").chmod(0o600)

        server = mcp_config["mcpServers"]["home-assistant"]
        profile = [
            'default_permissions = "bat"',
            "[permissions.bat]",
            'extends = ":read-only"',
            "[permissions.bat.filesystem]",
            f'{json.dumps(str(home))} = "deny"',
            "[permissions.bat.network]",
            "enabled = false",
            "[mcp_servers.home-assistant]",
            f"command = {json.dumps(server['command'])}",
            f"args = {json.dumps(server['args'])}",
            'default_tools_approval_mode = "approve"',
            "required = true",
            "startup_timeout_sec = 120",
            "tool_timeout_sec = 120",
            "[mcp_servers.home-assistant.env]",
        ]
        profile.extend(
            f"{json.dumps(k)} = {json.dumps(v)}" for k, v in server["env"].items()
        )
        path = home / "bat.config.toml"
        path.write_text("\n".join(profile) + "\n", encoding="utf-8")
        path.chmod(0o600)
        (root / "work").mkdir()
        return root, home
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def persist_auth(home: Path) -> None:
    """Return a rotated credential to the caller's dedicated auth directory."""
    target = auth_path()
    current = home / "auth.json"
    data = current.read_bytes()
    auth = json.loads(data)
    if not isinstance(auth, dict) or not auth.get("auth_mode"):
        raise ValueError("Codex BAT produced an invalid auth.json")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        tmp.write(data)
        staging = Path(tmp.name)
    try:
        staging.chmod(0o600)
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)


def command(prompt: str, model: str, workdir: Path) -> list[str]:
    return [
        "codex",
        "exec",
        "--profile",
        "bat",
        "--strict-config",
        "--skip-git-repo-check",
        "-c",
        "apps._default.enabled=false",
        "-c",
        'web_search="disabled"',
        "--disable",
        "shell_tool",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--cd",
        str(workdir),
        "--model",
        model,
        prompt,
    ]


def isolation_probe(home: Path, workdir: Path) -> list[str]:
    """Check the exact profile used by the agent before granting MCP writes."""
    return [
        "codex",
        "sandbox",
        "--profile",
        "bat",
        "--permission-profile",
        "bat",
        "--cd",
        str(workdir),
        "--",
        "bash",
        "-c",
        '[[ ! -r "$1" && ! -w "$1" ]]',
        "_",
        str(home / "auth.json"),
    ]


def parse_events(stdout: str, result: dict) -> dict:
    """Extract only observed CLI metrics; a zero exit alone is not a BAT pass."""
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    messages = [
        e["item"].get("text", "")
        for e in events
        if e.get("type") == "item.completed"
        and isinstance(e.get("item"), dict)
        and e["item"].get("type") == "agent_message"
    ]
    tools = [
        e["item"]
        for e in events
        if e.get("type") == "item.completed"
        and isinstance(e.get("item"), dict)
        and e["item"].get("type") == "mcp_tool_call"
    ]
    completed = [e for e in events if e.get("type") == "turn.completed"]
    failed = any(e.get("type") == "turn.failed" for e in events)
    usage = completed[-1].get("usage", {}) if completed else {}
    result.update(
        completed=result["exit_code"] == 0 and bool(completed) and not failed,
        output=messages[-1] if messages else "",
        num_turns=sum(e.get("type") == "turn.started" for e in events),
        tool_stats={
            "totalCalls": len(tools),
            "totalSuccess": sum(t.get("status") == "completed" for t in tools),
            "totalFail": sum(t.get("status") != "completed" for t in tools),
        },
        tool_sequence=[t.get("tool", "") for t in tools],
        raw_json=events,
    )
    if isinstance(usage, dict):
        for source, target in (
            ("input_tokens", "tokens_input"),
            ("output_tokens", "tokens_output"),
            ("cached_input_tokens", "tokens_cached"),
        ):
            if isinstance(usage.get(source), int):
                result[target] = usage[source]
    return result
