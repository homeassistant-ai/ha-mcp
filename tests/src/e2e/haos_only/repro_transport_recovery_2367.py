"""Branch-only fault injection; execute exclusively on disposable GitHub HAOS VMs."""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
import httpx
import pytest
from ruamel.yaml import YAML

from .repro_desktop_issue_2367 import DesktopClient
from .repro_issue_2367 import DATA, EXACT_TRANSFORM, call, get_dashboard, make_client, record

pytestmark = [pytest.mark.timeout(1800)]
SMALL = "config['views'][0]['sections'][1]['cards'][0]['icon'] = 'mdi:music-box-multiple'"
HOP_HEADERS = {"host", "connection", "transfer-encoding", "content-length", "content-encoding"}


class FaultProxy:
    """Observe bridge requests and inject a single, explicitly armed failure."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.mode = None
        self.fired = 0
        self.sequence = 0
        self.requests = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=300),
            auto_decompress=False,
        )
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp"
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()
        await self.session.close()

    async def handle(self, request):
        body = await request.read()
        message = json.loads(body) if body else {}
        tool = message.get("params", {}).get("name")
        self.sequence += 1
        seq = self.sequence
        self.requests.append({"seq": seq, "id": message.get("id"), "tool": tool})
        record("proxy_received", seq=seq, rpc_id=message.get("id"),
               method=message.get("method", request.method), tool=tool, bytes=len(body))
        mode = self.mode if tool else None
        if mode:
            self.mode = None
            self.fired += 1
            record("fault_fired", seq=seq, mode=mode, tool=tool)
        if mode == "upload_abort":
            # Advertise the entire JSON body, deliver half, then close upstream.
            # On the add-on route this reaches the MCP body reader directly;
            # embedded adds HA's webhook proxy before the MCP reader.
            target = urlsplit(self.upstream)
            assert target.scheme == "http"
            _, writer = await asyncio.open_connection(target.hostname, target.port or 80)
            headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
            headers.update({"Host": target.netloc, "Content-Length": str(len(body)), "Connection": "close"})
            head = "POST " + target.path + " HTTP/1.1\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            writer.write(head.encode() + body[:len(body) // 2])
            await writer.drain()
            await asyncio.sleep(0.5)
            writer.close()
            await writer.wait_closed()
            return web.Response(status=502, text="Injected incomplete upstream upload")
        if mode == "outage_502":
            return web.Response(status=502, text="Injected unavailable upstream")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        response = None
        try:
            async with self.session.request(request.method, self.upstream, data=body,
                                            headers=headers, allow_redirects=False) as upstream:
                response = web.StreamResponse(status=upstream.status,
                    headers={k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS - {"content-encoding"}})
                await response.prepare(request)
                received = 0
                async for chunk in upstream.content.iter_any():
                    received += len(chunk)
                    if mode == "response_abort":
                        await response.write(chunk[:max(1, len(chunk) // 2)])
                        request.transport.abort()
                        record("proxy_response_aborted", seq=seq, upstream_bytes=received,
                               upstream_status=upstream.status)
                        return response
                    await response.write(chunk)
                await response.write_eof()
                record("proxy_complete", seq=seq, status=upstream.status, bytes=received)
                return response
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError) as exc:
            record("proxy_error", seq=seq, exception=type(exc).__name__)
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.abort()
                return response
            return web.Response(status=502, text="Upstream unavailable")


async def outcome(client, tool, arguments, label, timeout=240):
    start = time.monotonic()
    result = {"ok": False, "exception": "Cancelled"}
    try:
        value = await call(client, tool, arguments, label, timeout=timeout)
        result = {"ok": True, "elapsed": time.monotonic() - start}
        return result, value
    except Exception as exc:
        result = {"ok": False, "elapsed": time.monotonic() - start,
                  "exception": type(exc).__name__}
        return result, None
    finally:
        record("probe_outcome", label=label, **result)


async def restored_server(info, url):
    """Require a real Core down/up cycle and fresh MCP read before probing reuse."""
    deadline = time.monotonic() + 300
    saw_down = False
    async with httpx.AsyncClient(timeout=3) as rest:
        while time.monotonic() < deadline:
            try:
                response = await rest.get(info["base_url"] + "/api/",
                    headers={"Authorization": "Bearer " + info["token"]})
                running = response.status_code == 200
            except httpx.HTTPError:
                running = False
            if not running:
                saw_down = True
            if running and saw_down:
                try:
                    async with asyncio.timeout(10):
                        async with make_client(url, False) as observer:
                            await get_dashboard(observer, "restart/readiness")
                    record("server_restored", down_observed=saw_down)
                    return
                except Exception:
                    pass
            await asyncio.sleep(1)
    raise AssertionError(f"Core down/up plus MCP recovery not observed; down={saw_down}")


async def probe(writer, observer, baseline, transform, label):
    before = await get_dashboard(observer, label + "/independent-before")
    assert before["config"] == baseline["config"]
    args = {"url_path": "dashboard-media", "config_hash": before["config_hash"],
            "python_transform": transform, "MandatoryBPS": False}
    task = asyncio.create_task(outcome(writer, "ha_config_set_dashboard", args, label + "/write"))
    # A stuck write gets both same-Desktop and independent read probes while pending.
    await asyncio.wait({task}, timeout=2)
    pending = not task.done()
    same_read, _ = await outcome(writer, "ha_config_get_dashboard",
        {"url_path": "dashboard-media"}, label + "/same-desktop-read", timeout=30)
    independent_read, _ = await outcome(observer, "ha_config_get_dashboard",
        {"url_path": "dashboard-media"}, label + "/independent-read", timeout=30)
    result, _ = await task
    after = await get_dashboard(observer, label + "/independent-after")
    changed = after["config_hash"] != before["config_hash"]
    if result["ok"]:
        expected_change = transform == EXACT_TRANSFORM or baseline["config"]["views"][0]["sections"][1]["cards"][0]["icon"] != "mdi:music-box-multiple"
        assert changed == expected_change, "Saved dashboard does not match expected change"
        if transform == EXACT_TRANSFORM:
            assert len(after["config"]["views"][1]["sections"]) == 4
            assert after["config"]["views"][1]["sections"][-1]["cards"][0]["heading"] == "Stresstest"
        else:
            assert after["config"]["views"][0]["sections"][1]["cards"][0]["icon"] == "mdi:music-box-multiple"
    record("recovery_probe", label=label, write=result, write_pending_at_reads=pending,
           same_desktop_read=same_read, independent_read=independent_read, changed=changed,
           issue_signature=result.get("exception") == "TimeoutError" and same_read["ok"] and independent_read["ok"])
    # Never blindly retry an unknown write: inspect first, then reset with fresh hash.
    if changed:
        await call(observer, "ha_config_set_dashboard", {
            "url_path": "dashboard-media", "config_hash": after["config_hash"],
            "config": baseline["config"], "MandatoryBPS": False}, label + "/reset")
    restored = await get_dashboard(observer, label + "/verified-baseline")
    assert restored["config_hash"] == baseline["config_hash"]
    return result


@pytest.mark.parametrize("fault", ["upload_abort", "response_abort", "outage_502", "core_restart"])
async def test_transport_recovery(ha_container_with_fresh_config, fault):
    info = ha_container_with_fresh_config
    url = info["embedded_webhook_url"] or info["addon_mcp_url"]
    fixture = DATA / "dashboard-media-sanitized.yaml"
    original = YAML(typ="safe").load(fixture.read_text())
    label = info["backend"] + "/" + fault
    record("recovery_case_start", label=label, fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
           transform_sha256=hashlib.sha256(EXACT_TRANSFORM.encode()).hexdigest(),
           desktop_session="simulated signed-in renderer; extracted real preload/lifecycle/stdio",
           bridge="fastmcp-remote==4.0.3 --auth none")
    async with make_client(url, False) as observer:
        await call(observer, "ha_config_set_dashboard", {"url_path": "dashboard-media",
            "config": original, "MandatoryBPS": False}, label + "/setup")
        baseline = await get_dashboard(observer, label + "/baseline")
    results = []
    async with FaultProxy(url) as proxy:
        routed_info = {**info, "embedded_webhook_url": proxy.url, "addon_mcp_url": None}
        async with DesktopClient(routed_info, Path("/tmp/desktop-measurements/recovery") / label,
                                 False, "2025-11-25") as writer:
            async with make_client(url, False) as observer:
                assert (await probe(writer, observer, baseline, EXACT_TRANSFORM, label + "/control"))["ok"]
            for cycle in range(1 if fault == "core_restart" else 3):
                cycle_label = label + f"/cycle-{cycle}"
                if fault == "core_restart":
                    recovery = asyncio.create_task(restored_server(info, url))
                    trigger, _ = await outcome(writer, "ha_call_service", {
                        "domain": "homeassistant", "service": "restart"},
                        cycle_label + "/restart", timeout=240)
                    await recovery
                    record("restart_trigger_result", label=cycle_label, result=trigger)
                else:
                    proxy.mode = fault
                    trigger, _ = await outcome(writer, "ha_config_set_dashboard", {
                        "url_path": "dashboard-media", "config_hash": baseline["config_hash"],
                        "python_transform": EXACT_TRANSFORM, "MandatoryBPS": False},
                        cycle_label + "/faulted-write")
                    assert proxy.mode is None, "The armed fault never reached the proxy"
                    record("faulted_write_result", label=cycle_label, result=trigger)
                # Fresh independent observer; Desktop and its bridge stay alive.
                async with make_client(url, False) as observer:
                    after = await get_dashboard(observer, cycle_label + "/fault-readback")
                    record("fault_readback", label=cycle_label,
                           changed=after["config_hash"] != baseline["config_hash"])
                    if after["config_hash"] != baseline["config_hash"]:
                        await call(observer, "ha_config_set_dashboard", {"url_path": "dashboard-media",
                            "config_hash": after["config_hash"], "config": original,
                            "MandatoryBPS": False}, cycle_label + "/restore-after-fault")
                    for index in range(4):
                        results.append(await probe(writer, observer, baseline,
                            EXACT_TRANSFORM if index % 2 == 0 else SMALL,
                            cycle_label + f"/post-{index}"))
            record("desktop_preserved", label=label, electron_pid=writer.proc.pid,
                   electron_returncode=writer.proc.returncode, injected_faults=proxy.fired)
    async with make_client(url, False) as observer:
        await call(observer, "ha_config_delete_dashboard", {"url_path": "dashboard-media"}, label + "/cleanup")
    record("recovery_case_complete", label=label, probes=len(results),
           successful=sum(result["ok"] for result in results), failed=sum(not result["ok"] for result in results))
    assert all(result["ok"] for result in results), "Recovery failure captured; inspect events.jsonl"
