"""Runner-only adapter: let an MCP SDK drive Desktop's real renderer/pipe path.

stdin/stdout carry ordinary MCP JSON-RPC. Startup and harness control messages
are kept off that stream. The Desktop transport still handles the server pipes.
"""
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

LIMIT = 34 * 1024 * 1024


async def main():
    server = json.loads(os.environ['DESKTOP_MCP_SERVER'])
    root = Path(os.environ.get('DESKTOP_TRACE_ROOT', '/tmp/desktop-sdk-measurements'))
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='session-', dir=root))
    config = folder / 'config.json'
    config.write_text(json.dumps({'mcpServers': {'primary': server}}))
    config.chmod(0o600)
    env = {**os.environ, 'REPRO_DESKTOP_CONFIG': str(config),
           'REPRO_DESKTOP_PROFILE': str(folder / 'profile'),
           'REPRO_DESKTOP_LOG': str(folder / 'transport.jsonl')}
    proc = await asyncio.create_subprocess_exec(
        'xvfb-run', '-a', '/tmp/claude-package/usr/lib/claude-desktop/claude-desktop', '--no-sandbox',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr, env=env, limit=LIMIT,
    )
    input_task = output_task = None
    pipe = None
    try:
        async with asyncio.timeout(90):
            while line := await proc.stdout.readline():
                if not line.startswith(b'{'):
                    sys.stderr.buffer.write(line)
                    sys.stderr.buffer.flush()
                    continue
                message = json.loads(line)
                if message.get('type') != 'ready':
                    raise RuntimeError(message)
                (folder / 'versions.json').write_text(json.dumps(message, indent=2))
                break
            else:
                raise RuntimeError('Desktop exited before initializing its message port')
        reader = asyncio.StreamReader(limit=LIMIT)
        pipe, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)

        async def send_requests():
            while line := await reader.readline():
                proc.stdin.write(line)
                await proc.stdin.drain()
            proc.stdin.close()

        async def receive_responses():
            while line := await proc.stdout.readline():
                if not line.startswith(b'{'):
                    sys.stderr.buffer.write(line)
                    sys.stderr.buffer.flush()
                    continue
                message = json.loads(line)
                if message.get('type') in ('fatal', 'transport_error'):
                    raise RuntimeError(message)
                assert message.get('jsonrpc') == '2.0', message
                assert message.pop('route', 'primary') == 'primary'
                sys.stdout.write(json.dumps(message, ensure_ascii=False) + '\n')
                sys.stdout.flush()

        input_task = asyncio.create_task(send_requests())
        output_task = asyncio.create_task(receive_responses())
        done, _ = await asyncio.wait([input_task, output_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if input_task in done:
            async with asyncio.timeout(20):
                await output_task
                await proc.wait()
        else:
            raise RuntimeError('Desktop connection closed before the MCP client')
        if proc.returncode != 0:
            raise RuntimeError(f'Desktop exited with code {proc.returncode}')
    finally:
        for task in (input_task, output_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if pipe is not None:
            pipe.close()
        config.unlink(missing_ok=True)


if __name__ == '__main__':
    asyncio.run(main())
