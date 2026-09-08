"""GitHub-only integration check using the existing FastMCP client API."""
import asyncio
import json
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

SERVER = '''from fastmcp import FastMCP
app=FastMCP('Desktop transport SDK smoke')
@app.tool
def echo(text: str) -> dict:
    return {'text': text}
@app.resource('test://template')
def template() -> str:
    return "{% if state %} {{ value }} % ü"
@app.prompt
def template_prompt(text: str) -> str:
    return text
app.run(transport='stdio')
'''


async def main():
    server = {'command': sys.executable, 'args': ['-u', '-c', SERVER], 'env': {}}
    transport = StdioTransport(
        command=sys.executable, args=['-u', str(Path(__file__).with_name('desktop_sdk_driver.py'))],
        env={'DESKTOP_MCP_SERVER': json.dumps(server), 'PATH': os.environ['PATH']}, keep_alive=False,
    )
    async with Client(transport, timeout=60) as client:
        assert any(t.name == 'echo' for t in await client.list_tools())
        payload = "{% if state %} {{ states('sensor.test') }} 100% ü" * 3000
        replies = await asyncio.gather(*[client.call_tool('echo', {'text': payload + str(i)}) for i in range(8)])
        for i, reply in enumerate(replies):
            assert json.loads(reply.content[0].text) == {'text': payload + str(i)}
        resources = await client.list_resources()
        assert any(str(r.uri) == 'test://template' for r in resources)
        content = await client.read_resource('test://template')
        assert content[0].text == '{% if state %} {{ value }} % ü'
        prompt = await client.get_prompt('template_prompt', {'text': payload})
        assert prompt.messages[0].content.text == payload
    Path('/tmp/desktop-inspection/sdk-pass.txt').write_text(
        'FastMCP Client initialized through Desktop; listed tools/resources; read a resource; '
        'retrieved a large prompt; preserved eight concurrent large Unicode/template tool calls; closed cleanly.\n')


asyncio.run(main())
