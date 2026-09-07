"""Branch-only HAOS investigation; deliberately excluded from normal discovery."""

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from ruamel.yaml import YAML

from ..utilities.assertions import parse_mcp_result

pytestmark = [pytest.mark.haos_embedded_only, pytest.mark.timeout(1200)]
ROOT = Path(__file__).resolve().parents[4]
DATA = ROOT / "investigation" / "issue-2367"
OUTPUT = Path(os.environ.get("REPRO_OUTPUT", "/tmp/issue-2367"))
DASHBOARD = "dashboard-media"
EXACT_TRANSFORM = """
config['views'][1]['sections'].append({'type': 'grid', 'cards': [
  {'type': 'heading', 'heading': 'Stresstest', 'heading_style': 'subtitle'},
  {'type': 'custom:mushroom-chips-card', 'alignment': 'start', 'chips': [
    {'type': 'template', 'icon': 'mdi:test-tube',
     'content': "{{ states('input_select.radio_sender') }}",
     'icon_color': "{% if is_state('script.radio_play','on') %}amber{% else %}green{% endif %}",
     'tap_action': {'action': 'none'}},
    {'type': 'template', 'icon': 'mdi:volume-high',
     'content': "{{ (state_attr('media_player.vsx_832','volume_level') | float(0) * 100) | round(0) }}%",
     'tap_action': {'action': 'none'}},
    {'type': 'template', 'icon': 'mdi:remote',
     'content': "{{ states('select.elite_aktivitaten') }}",
     'tap_action': {'action': 'none'}}]},
  {'type': 'tile', 'entity': 'media_player.shield_2',
   'features': [{'type': 'media-player-playback'}],
   'features_position': 'bottom'}]})
"""


def record(event, **fields):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    row = {"time": time.time(), "event": event, **fields}
    with (OUTPUT / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print("REPRO2367 " + json.dumps(row, ensure_ascii=False, default=str), flush=True)


def make_client(url, bridge):
    if bridge:
        transport = StdioTransport(
            command="/tmp/repro-bin/fastmcp-remote",
            args=[url, "--auth", "none"],
        )
    else:
        transport = StreamableHttpTransport(url=url)
    return Client(transport, timeout=240)


async def call(client, tool, args, label, timeout=240):
    start = time.monotonic()
    record("dispatch", label=label, tool=tool,
           request_bytes=len(json.dumps(args).encode()))
    try:
        async with asyncio.timeout(timeout):
            result = await client.call_tool(tool, args)
        data = parse_mcp_result(result)
        record("response", label=label, tool=tool,
               elapsed=time.monotonic() - start,
               response_bytes=len(json.dumps(data, default=str).encode()),
               success=data.get("success"), config_hash=data.get("config_hash"),
               skill_bytes=len(json.dumps(data.get("skill_content", "")).encode()))
        assert not getattr(result, "is_error", False), data
        assert data.get("success") is not False, data
        assert "error" not in data, data
        return data
    except Exception as exc:
        # Never include endpoint URLs or credentials in our structured artifact.
        record("exception", label=label, tool=tool,
               elapsed=time.monotonic() - start, exception=type(exc).__name__)
        raise


async def get_dashboard(client, label):
    return await call(client, "ha_config_get_dashboard",
                      {"url_path": DASHBOARD}, label, timeout=30)


async def heartbeat(client, label):
    """Separate MCP connection probes read responsiveness during a stalled write."""
    while True:
        await asyncio.sleep(10)
        try:
            data = await get_dashboard(client, label + "/heartbeat")
            record("heartbeat", label=label, config_hash=data["config_hash"],
                   sections=len(data["config"]["views"][1]["sections"]))
        except Exception as exc:
            record("heartbeat_error", label=label, exception=type(exc).__name__)


async def attempt(writer, observer, baseline, transform, bps, label, large):
    before = await get_dashboard(writer, label + "/before")
    assert before["config"] == baseline["config"], "Baseline drift before write"
    args = {"url_path": DASHBOARD, "config_hash": before["config_hash"],
            "python_transform": transform}
    if bps == "false":
        args["MandatoryBPS"] = False
    monitor = asyncio.create_task(heartbeat(observer, label))
    try:
        try:
            await call(writer, "ha_config_set_dashboard", args, label + "/write")
        finally:
            # Independent read also runs after a timeout, before the failure propagates.
            after = await get_dashboard(observer, label + "/after")
            record("readback", label=label, changed=after["config_hash"] != before["config_hash"],
                   sections=len(after["config"]["views"][1]["sections"]))
        assert after["config_hash"] != before["config_hash"]
        if large:
            assert len(after["config"]["views"][1]["sections"]) == 4
            assert after["config"]["views"][1]["sections"][-1]["cards"][0]["heading"] == "Stresstest"
        else:
            assert after["config"]["views"][0]["sections"][1]["cards"][0]["icon"] == "mdi:music-box"
        await call(writer, "ha_config_set_dashboard", {
            "url_path": DASHBOARD, "config_hash": after["config_hash"],
            "config": baseline["config"], "MandatoryBPS": False,
        }, label + "/restore")
        restored = await get_dashboard(observer, label + "/restored")
        assert restored["config"] == baseline["config"]
        assert restored["config_hash"] == baseline["config_hash"]
        record("attempt_pass", label=label)
    finally:
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor


@pytest.mark.parametrize("bridge", [False, True], ids=["http", "fastmcp-remote"])
@pytest.mark.parametrize("bps", ["default", "false"])
async def test_issue_2367(ha_container_with_fresh_config, bridge, bps):
    info = ha_container_with_fresh_config
    assert info["backend"] == "haos_embedded"
    url = info["embedded_webhook_url"]
    fixture = DATA / "dashboard-media-sanitized.yaml"
    original = YAML(typ="safe").load(fixture.read_text())
    record("case_start", bridge=bridge, bps=bps, backend=info["backend"],
           fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
           transform_sha256=hashlib.sha256(EXACT_TRANSFORM.encode()).hexdigest(),
           client_fastmcp=importlib.metadata.version("fastmcp"))
    async with httpx.AsyncClient() as rest:
        config = await rest.get(info["base_url"] + "/api/config",
                                headers={"Authorization": "Bearer " + info["token"]})
        config.raise_for_status()
        record("ha_version", version=config.json()["version"])
    async with make_client(url, False) as observer:
        await call(observer, "ha_config_set_dashboard", {
            "url_path": DASHBOARD, "config": original, "MandatoryBPS": False,
        }, "setup")
        baseline = await get_dashboard(observer, "baseline")
        assert baseline["config"] == original
        assert len(original["views"][1]["sections"]) == 3
        # Five genuinely fresh stdio processes / HTTP clients, first write with
        # requested BPS value. Discovery and dashboard read precede each write.
        for session in range(5):
            async with make_client(url, bridge) as writer:
                catalog = await writer.list_tools()
                assert any(t.name == "ha_config_set_dashboard" for t in catalog)
                await attempt(writer, observer, baseline, EXACT_TRANSFORM, bps,
                              f"{bridge}/{bps}/fresh/{session}", True)
        # Reused session alternates the reporter's large and one-key shapes.
        async with make_client(url, bridge) as writer:
            for iteration in range(30):
                large = iteration % 2 == 0
                transform = EXACT_TRANSFORM if large else (
                    "config['views'][0]['sections'][1]['cards'][0]['icon'] = 'mdi:music-box'"
                )
                await attempt(writer, observer, baseline, transform, bps,
                              f"{bridge}/{bps}/reuse/{iteration}", large)
        await call(observer, "ha_config_delete_dashboard", {"url_path": DASHBOARD}, "cleanup")
    record("case_pass", bridge=bridge, bps=bps, attempts=35)
