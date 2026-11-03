"""Where a BAT run's agents find ha-mcp: the MCP client configs they get,
and ``--dev-env`` / ``--mcp-url`` for a server that is already running."""

from __future__ import annotations

import argparse
import atexit
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]


def mcp_server_command(branch: str | None) -> list[str]:
    """Build the MCP server command for stdio mode."""
    if branch:
        return [
            "uvx",
            "--from",
            f"git+https://github.com/homeassistant-ai/ha-mcp.git@{branch}",
            "ha-mcp",
        ]
    return ["uv", "run", "--project", str(REPO_ROOT), "ha-mcp"]


def mcp_server_action(args: argparse.Namespace) -> str:
    """What a run does to get its MCP server, for the log (never the URL)."""
    if args.mcp_url:
        return "Connecting to the running MCP server"
    source = f"uvx download @ {args.branch}" if args.branch else "local"
    return f"Starting MCP server ({source})"


def running_server_revision(args: argparse.Namespace) -> tuple[str, str] | None:
    """The ``(sha, version)`` a story result records for a running server.

    Its code is not the local checkout and the runner cannot see its commit,
    so the result says so instead of recording the local revision.
    """
    return ("unknown", "running server") if args.mcp_url else None


def read_dev_env_urls(path: str | Path) -> tuple[str, str]:
    """``(ha_url, mcp_url)`` from a decrypted dev environment URLs file.

    The file holds ``HA: <url>`` and ``MCP: <url>`` lines
    (docs/dev-ha-env.md); a tunnel being restarted shows as ``down``.
    """
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise ValueError(f"cannot read the dev environment URLs file: {e}") from e
    urls: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        urls[key.strip()] = value.strip()

    def usable(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)

    missing = [k for k in ("HA", "MCP") if not usable(urls.get(k, ""))]
    if missing:
        raise ValueError(
            f"{path} has no usable {' or '.join(missing)} URL; download and "
            "decrypt the run's dev-ha-env-urls again (docs/dev-ha-env.md)"
        )
    return urls["HA"], urls["MCP"]


def _build_mcp_env(
    ha_url: str, ha_token: str, extra_env: dict[str, str] | None
) -> dict[str, str]:
    # Override with --mcp-env LOG_LEVEL=INFO when debugging the server.
    # A fresh config dir keeps the developer's ~/.ha-mcp tool pins and
    # settings out of the results; --mcp-env HA_MCP_CONFIG_DIR=... opts in.
    # The backup dir is pinned too: the server otherwise keeps using a
    # populated ~/.local/share/ha_mcp/backups.
    config_dir = tempfile.mkdtemp(prefix="ha-mcp-bat-")
    atexit.register(shutil.rmtree, config_dir, ignore_errors=True)
    env = {
        "HOMEASSISTANT_URL": ha_url,
        "HOMEASSISTANT_TOKEN": ha_token,
        "LOG_LEVEL": "WARNING",
        "HA_MCP_CONFIG_DIR": config_dir,
        "HAMCP_BACKUP_DIR": str(Path(config_dir) / "backups"),
        "HA_MCP_DISABLE_SETTINGS_UI": "1",
    }
    if extra_env:
        env.update(extra_env)
    return env


def parse_mcp_env(
    raw_mcp_env: list[str] | None,
    base_url: str | None = None,
    on_default_applied: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs into a dict, with the local-model default.

    A bare ``KEY`` (no ``=``) maps to an empty string — useful for
    boolean-presence flags. When ``base_url`` is set (targeting a local
    OpenAI-compatible endpoint), ``ENABLE_TOOL_SEARCH=true`` is injected
    unless the caller already set it. Local models typically can't prefill
    the full tool catalog, so tool search is the default for that path.
    Override with an explicit ``ENABLE_TOOL_SEARCH=...`` pair.

    If ``on_default_applied`` is provided, it is called with a human-readable
    message each time a default is injected.
    """
    pairs = raw_mcp_env or []
    env = {k: v for pair in pairs for k, _, v in [pair.partition("=")]}
    if base_url and "ENABLE_TOOL_SEARCH" not in env:
        env["ENABLE_TOOL_SEARCH"] = "true"
        if on_default_applied:
            on_default_applied(
                "Defaulting ENABLE_TOOL_SEARCH=true for local model (--base-url set)"
            )
    return env


def build_stdio_mcp_config(
    ha_url: str,
    ha_token: str,
    branch: str | None,
    extra_env: dict[str, str] | None = None,
    mcp_url: str | None = None,
) -> dict:
    """Build the MCP config dict (format shared with Claude's --mcp-config).

    The dict can be passed directly to ``fastmcp.Client(config)`` for in-process
    use, or serialized to a file via ``write_stdio_mcp_config`` for CLI agents
    that expect a file path. With ``mcp_url`` it points at that running server
    instead of starting one over stdio.
    """
    if mcp_url:
        return {"mcpServers": {"home-assistant": {"type": "http", "url": mcp_url}}}
    cmd = mcp_server_command(branch)
    env = _build_mcp_env(ha_url, ha_token, extra_env)
    return {
        "mcpServers": {
            "home-assistant": {
                "command": cmd[0],
                "args": cmd[1:],
                "env": env,
            }
        }
    }


def write_stdio_mcp_config(
    ha_url: str,
    ha_token: str,
    branch: str | None,
    extra_env: dict[str, str] | None = None,
    mcp_url: str | None = None,
) -> Path:
    """Write the MCP config to a temporary JSON file, return its path."""
    config = build_stdio_mcp_config(ha_url, ha_token, branch, extra_env, mcp_url)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="claude_mcp_", delete=False
    ) as f:
        json.dump(config, f)
    return Path(f.name)


def write_gemini_mcp_config(
    ha_url: str,
    ha_token: str,
    branch: str | None,
    workdir: Path,
    extra_env: dict[str, str] | None = None,
    mcp_url: str | None = None,
) -> None:
    """Write .gemini/settings.json in the given workdir."""
    gemini_dir = workdir / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    server: dict
    if mcp_url:
        server = {"httpUrl": mcp_url}
    else:
        cmd = mcp_server_command(branch)
        env = _build_mcp_env(ha_url, ha_token, extra_env)
        server = {"command": cmd[0], "args": cmd[1:], "env": env}
    # An empty core-tool list registers none of Gemini's built-in tools (shell,
    # files, web), so the agent can only use the BAT server.
    config = {"mcpServers": {"homeassistant": server}, "tools": {"core": []}}
    (gemini_dir / "settings.json").write_text(json.dumps(config))


def resolve_running_server(args: argparse.Namespace) -> None:
    """Apply ``--dev-env`` and refuse options a running MCP server ignores.

    ``--dev-env`` fills ``--ha-url`` and ``--mcp-url`` from the environment's
    URLs file. With ``--mcp-url`` the agents use that server, so nothing here
    starts one and ``--branch`` / ``--mcp-env`` would not apply.
    """
    args.mcp_url = getattr(args, "mcp_url", None)
    if getattr(args, "dev_env", None):
        if args.ha_url or args.mcp_url:
            raise ValueError("--dev-env sets --ha-url and --mcp-url; pass neither")
        args.ha_url, args.mcp_url = read_dev_env_urls(args.dev_env)
    if args.mcp_url:
        if not args.ha_url:
            raise ValueError("--mcp-url needs the HA it serves: pass --ha-url")
        ignored = [
            flag
            for flag, value in (
                ("--branch", args.branch),
                ("--mcp-env", getattr(args, "mcp_env", None)),
            )
            if value
        ]
        if ignored:
            raise ValueError(
                f"{', '.join(ignored)} configure a server this runner starts; "
                "with --mcp-url or --dev-env the agents use a running one"
            )


def add_target_args(parser: argparse.ArgumentParser) -> None:
    """The Home Assistant and ha-mcp server a story run uses."""
    parser.add_argument("--branch", help="Git branch/tag to install ha-mcp from")
    parser.add_argument("--ha-url", help="Use existing HA instance (skip container)")
    parser.add_argument("--ha-token", help="HA long-lived access token")
    parser.add_argument(
        "--dev-env",
        metavar="URLS_FILE",
        help="Run against a dev environment: its decrypted dev-ha-env-urls file "
        "gives the HA instance and the ha-mcp server the agents use "
        "(docs/dev-ha-env.md)",
    )


def apply_dev_env(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Point a story run at ``--dev-env``'s Home Assistant and ha-mcp server."""
    if any(a.split("=")[0] == "--mcp-url" for a in args.extra_args):
        # Passed through to run_uat.py alone, it would reach only the
        # subprocess agents, not the inline agent or the recorded revision.
        parser.error("pass the running server with --dev-env, not --mcp-url after --")
    try:
        resolve_running_server(args)
    except ValueError as e:
        parser.error(str(e))
    if args.dev_env and not args.ha_token:
        from test_constants import TEST_TOKEN

        args.ha_token = TEST_TOKEN
    if args.mcp_url:
        # run_uat.py runs each test prompt; it takes the server from here.
        args.extra_args = [*args.extra_args, "--mcp-url", args.mcp_url]
