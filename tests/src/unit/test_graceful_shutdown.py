"""Unit tests for graceful shutdown signal handling.

These tests verify that the server properly handles SIGTERM and SIGINT signals,
exiting cleanly within the expected timeout period.
"""

import asyncio
import os
import signal
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSignalHandlerSetup:
    """Tests for signal handler registration."""

    def test_setup_signal_handlers_registers_sigterm(self):
        """Signal handlers should register SIGTERM handler."""
        from ha_mcp.__main__ import _setup_signal_handlers, _signal_handler

        with patch("signal.signal") as mock_signal:
            _setup_signal_handlers()
            # Check that SIGTERM handler was registered
            calls = [call for call in mock_signal.call_args_list if call[0][0] == signal.SIGTERM]
            assert len(calls) == 1
            assert calls[0][0][1] == _signal_handler

    def test_setup_signal_handlers_registers_sigint(self):
        """Signal handlers should register SIGINT handler."""
        from ha_mcp.__main__ import _setup_signal_handlers, _signal_handler

        with patch("signal.signal") as mock_signal:
            _setup_signal_handlers()
            # Check that SIGINT handler was registered
            calls = [call for call in mock_signal.call_args_list if call[0][0] == signal.SIGINT]
            assert len(calls) == 1
            assert calls[0][0][1] == _signal_handler


class TestSignalHandler:
    """Tests for the signal handler function."""

    def test_first_signal_sets_shutdown_in_progress(self):
        """First signal should set shutdown_in_progress flag."""
        import ha_mcp.__main__ as main_module

        # Reset global state
        main_module._shutdown_in_progress = False
        main_module._shutdown_event = None

        # Call signal handler
        main_module._signal_handler(signal.SIGTERM, None)

        assert main_module._shutdown_in_progress is True

    def test_first_signal_with_event_sets_event(self):
        """First signal should set shutdown event if available."""
        import ha_mcp.__main__ as main_module

        # Reset global state
        main_module._shutdown_in_progress = False

        # Create a mock event
        mock_event = MagicMock()
        main_module._shutdown_event = mock_event

        # Mock the event loop
        mock_loop = MagicMock()
        with patch("asyncio.get_running_loop", return_value=mock_loop):
            main_module._signal_handler(signal.SIGTERM, None)

        # Verify event.set was scheduled
        mock_loop.call_soon_threadsafe.assert_called_once_with(mock_event.set)

    def test_second_signal_forces_exit(self):
        """Second signal should force immediate exit."""
        import ha_mcp.__main__ as main_module

        # Set up as if first signal was already received
        main_module._shutdown_in_progress = True
        main_module._shutdown_event = None

        # Second signal should call sys.exit(1)
        with pytest.raises(SystemExit) as exc_info:
            main_module._signal_handler(signal.SIGINT, None)

        assert exc_info.value.code == 1

    def test_signal_without_event_loop_exits(self):
        """Signal without event loop should exit gracefully."""
        import ha_mcp.__main__ as main_module

        # Reset global state
        main_module._shutdown_in_progress = False
        main_module._shutdown_event = MagicMock()

        # Make get_running_loop raise RuntimeError (no running loop)
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no running loop")):
            with pytest.raises(SystemExit) as exc_info:
                main_module._signal_handler(signal.SIGTERM, None)

        assert exc_info.value.code == 0


class TestCleanupResources:
    """Tests for resource cleanup function."""

    @pytest.mark.asyncio
    async def test_cleanup_stops_websocket_listener(self):
        """Cleanup should stop the WebSocket listener service."""
        import ha_mcp.__main__ as main_module

        mock_stop = AsyncMock()

        with patch("ha_mcp.client.websocket_listener.stop_websocket_listener", mock_stop):
            with patch("ha_mcp.client.websocket_client.websocket_manager", MagicMock(disconnect=AsyncMock())):
                main_module._server = None
                await main_module._cleanup_resources()
                mock_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_disconnects_websocket_manager(self):
        """Cleanup should disconnect the WebSocket manager."""
        import ha_mcp.__main__ as main_module

        mock_manager = MagicMock()
        mock_manager.disconnect = AsyncMock()

        with patch("ha_mcp.client.websocket_listener.stop_websocket_listener", AsyncMock()):
            with patch("ha_mcp.client.websocket_client.websocket_manager", mock_manager):
                main_module._server = None
                await main_module._cleanup_resources()

                mock_manager.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_closes_server(self):
        """Cleanup should close the server if it exists."""
        import ha_mcp.__main__ as main_module

        mock_server = MagicMock()
        mock_server.close = AsyncMock()
        main_module._server = mock_server

        with patch("ha_mcp.client.websocket_listener.stop_websocket_listener", AsyncMock()):
            with patch("ha_mcp.client.websocket_client.websocket_manager", MagicMock(disconnect=AsyncMock())):
                await main_module._cleanup_resources()

                mock_server.close.assert_called_once()

        # Reset global state
        main_module._server = None

    @pytest.mark.asyncio
    async def test_cleanup_handles_exceptions_gracefully(self):
        """Cleanup should handle exceptions without crashing."""
        import ha_mcp.__main__ as main_module

        # Make everything raise exceptions
        with patch("ha_mcp.client.websocket_listener.stop_websocket_listener", AsyncMock(side_effect=Exception("test error"))):
            with patch("ha_mcp.client.websocket_client.websocket_manager", MagicMock(disconnect=AsyncMock(side_effect=Exception("test error")))):
                main_module._server = None
                # Should not raise
                await main_module._cleanup_resources()


class TestShutdownTimeout:
    """Tests for shutdown timeout configuration."""

    def test_shutdown_timeout_is_two_seconds(self):
        """Shutdown timeout should be 2 seconds as per requirements."""
        from ha_mcp.__main__ import SHUTDOWN_TIMEOUT_SECONDS

        assert SHUTDOWN_TIMEOUT_SECONDS == 2.0


class TestGracefulShutdownIntegration:
    """Integration tests for graceful shutdown behavior."""

    @pytest.mark.asyncio
    async def test_shutdown_event_cancels_server_task(self):
        """Setting shutdown event should cancel the server task."""
        import ha_mcp.__main__ as main_module

        # Create a mock MCP that runs forever
        mock_mcp = MagicMock()

        async def mock_run_async(show_banner=True):
            await asyncio.sleep(100)  # Simulate long-running server

        mock_mcp.run_async = mock_run_async

        with patch.object(main_module, "_get_mcp", return_value=mock_mcp):
            with patch.object(main_module, "_cleanup_resources", new_callable=AsyncMock):
                # Reset state
                main_module._shutdown_event = None
                main_module._shutdown_in_progress = False

                # Start the server in a task
                server_coro = main_module._run_with_graceful_shutdown()
                server_task = asyncio.create_task(server_coro)

                # Give it time to start
                await asyncio.sleep(0.1)

                # Trigger shutdown
                if main_module._shutdown_event:
                    main_module._shutdown_event.set()

                # Wait for shutdown with timeout
                try:
                    await asyncio.wait_for(server_task, timeout=3.0)
                except TimeoutError:
                    pytest.fail("Server did not shut down within timeout")
                except asyncio.CancelledError:
                    pass  # Expected

    @pytest.mark.asyncio
    async def test_cleanup_called_on_shutdown(self):
        """Resource cleanup should be called on shutdown."""
        import ha_mcp.__main__ as main_module

        cleanup_called = asyncio.Event()

        async def mock_cleanup():
            cleanup_called.set()

        mock_mcp = MagicMock()

        async def mock_run_async(show_banner=True):
            await asyncio.sleep(100)

        mock_mcp.run_async = mock_run_async

        with patch.object(main_module, "_get_mcp", return_value=mock_mcp):
            with patch.object(main_module, "_cleanup_resources", side_effect=mock_cleanup):
                main_module._shutdown_event = None
                main_module._shutdown_in_progress = False

                server_task = asyncio.create_task(main_module._run_with_graceful_shutdown())

                await asyncio.sleep(0.1)

                if main_module._shutdown_event:
                    main_module._shutdown_event.set()

                try:
                    await asyncio.wait_for(server_task, timeout=3.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass

                # Verify cleanup was called
                assert cleanup_called.is_set(), "Cleanup was not called"


class TestStdinDetection:
    """Tests for stdin availability detection (Docker without -i flag)."""

    def test_stdin_available_when_tty(self):
        """Stdin should be available when connected to a tty."""
        from ha_mcp.__main__ import _check_stdin_available
        import stat as stat_module

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.closed = False
            mock_stdin.fileno.return_value = 0

            with patch("os.fstat") as mock_fstat:
                mock_fstat.return_value = MagicMock(st_mode=stat_module.S_IFCHR)

                with patch("os.isatty", return_value=True):
                    assert _check_stdin_available() is True

    def test_stdin_available_when_pipe(self):
        """Stdin should be available when connected to a pipe (FIFO)."""
        from ha_mcp.__main__ import _check_stdin_available
        import stat as stat_module

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.closed = False
            mock_stdin.fileno.return_value = 0

            with patch("os.fstat") as mock_fstat:
                mock_fstat.return_value = MagicMock(st_mode=stat_module.S_IFIFO)

                with patch("os.isatty", return_value=False):
                    assert _check_stdin_available() is True

    def test_stdin_available_when_regular_file(self):
        """Stdin should be available when connected to a regular file."""
        from ha_mcp.__main__ import _check_stdin_available
        import stat as stat_module

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.closed = False
            mock_stdin.fileno.return_value = 0

            with patch("os.fstat") as mock_fstat:
                mock_fstat.return_value = MagicMock(st_mode=stat_module.S_IFREG)

                with patch("os.isatty", return_value=False):
                    assert _check_stdin_available() is True

    def test_stdin_not_available_when_closed(self):
        """Stdin should not be available when closed."""
        from ha_mcp.__main__ import _check_stdin_available

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.closed = True
            assert _check_stdin_available() is False

    def test_stdin_not_available_when_none(self):
        """Stdin should not be available when None."""
        from ha_mcp.__main__ import _check_stdin_available

        with patch("sys.stdin", None):
            assert _check_stdin_available() is False

    def test_stdin_not_available_when_char_device_not_tty(self):
        """Stdin should not be available when char device but not tty (like /dev/null)."""
        from ha_mcp.__main__ import _check_stdin_available
        import stat as stat_module

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.closed = False
            mock_stdin.fileno.return_value = 0

            with patch("os.fstat") as mock_fstat:
                mock_fstat.return_value = MagicMock(st_mode=stat_module.S_IFCHR)

                with patch("os.isatty", return_value=False):
                    assert _check_stdin_available() is False

    def test_stdin_not_available_when_fileno_raises(self):
        """Stdin should not be available when fileno() raises."""
        from ha_mcp.__main__ import _check_stdin_available

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.closed = False
            mock_stdin.fileno.side_effect = ValueError("no fileno")
            assert _check_stdin_available() is False

    def test_stdin_not_available_when_fstat_raises(self):
        """Stdin should not be available when fstat() raises."""
        from ha_mcp.__main__ import _check_stdin_available

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.closed = False
            mock_stdin.fileno.return_value = 0

            with patch("os.fstat", side_effect=OSError("fstat failed")):
                assert _check_stdin_available() is False

    def test_main_exits_when_stdin_not_available(self):
        """Main should exit with error when stdin is not available."""
        import ha_mcp.__main__ as main_module

        with patch.object(sys, "argv", ["ha-mcp"]):
            with patch.object(main_module, "_check_stdin_available", return_value=False):
                with pytest.raises(SystemExit) as exc_info:
                    main_module.main()

                assert exc_info.value.code == 1


class TestMainEntryPoint:
    """Tests for the main entry point function."""

    def test_smoke_test_flag_runs_smoke_test(self):
        """--smoke-test flag should run smoke test instead of server."""
        from ha_mcp.__main__ import main

        mock_smoke_test = MagicMock(return_value=0)

        with patch.object(sys, "argv", ["ha-mcp", "--smoke-test"]):
            with patch("ha_mcp.smoke_test.main", mock_smoke_test):
                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 0
                mock_smoke_test.assert_called_once()

    def test_main_sets_up_signal_handlers(self):
        """Main should set up signal handlers before running."""
        import ha_mcp.__main__ as main_module

        setup_called = False

        def mock_setup():
            nonlocal setup_called
            setup_called = True

        # Reset global state to avoid test pollution
        main_module._shutdown_in_progress = False
        main_module._shutdown_event = None

        with patch.object(main_module, "_check_stdin_available", return_value=True):
            with patch.object(main_module, "_setup_signal_handlers", side_effect=mock_setup):
                with patch.object(main_module, "_run_with_graceful_shutdown", new_callable=AsyncMock):
                    with pytest.raises(SystemExit):
                        main_module.main()

        assert setup_called, "Signal handlers were not set up"


class TestHTTPEntryPoints:
    """Tests for HTTP entry points (main_web, main_sse)."""

    def test_main_web_uses_streamable_http_transport(self):
        """main_web should use streamable-http transport."""
        import ha_mcp.__main__ as main_module

        transport_used = None

        def mock_run_http(transport, default_port=8086):
            nonlocal transport_used
            transport_used = transport
            raise SystemExit(0)

        # Reset global state
        main_module._shutdown_in_progress = False
        main_module._shutdown_event = None

        # Provide credentials to pass validation
        with patch.dict(os.environ, {
            "HOMEASSISTANT_URL": "http://test.local:8123",
            "HOMEASSISTANT_TOKEN": "test_token"
        }):
            with patch.object(main_module, "_run_http_server", side_effect=mock_run_http):
                with pytest.raises(SystemExit):
                    main_module.main_web()

        assert transport_used == "streamable-http"

    def test_main_sse_uses_sse_transport(self):
        """main_sse should use sse transport."""
        import ha_mcp.__main__ as main_module

        transport_used = None

        def mock_run_http(transport, default_port=8087):
            nonlocal transport_used
            transport_used = transport
            raise SystemExit(0)

        # Reset global state
        main_module._shutdown_in_progress = False
        main_module._shutdown_event = None

        # Provide credentials to pass validation
        with patch.dict(os.environ, {
            "HOMEASSISTANT_URL": "http://test.local:8123",
            "HOMEASSISTANT_TOKEN": "test_token"
        }):
            with patch.object(main_module, "_run_http_server", side_effect=mock_run_http):
                with pytest.raises(SystemExit):
                    main_module.main_sse()

        assert transport_used == "sse"

    def test_http_runtime_uses_env_vars(self):
        """HTTP runtime should read port and path from environment."""
        from ha_mcp.__main__ import _get_http_runtime

        with patch.dict(os.environ, {"MCP_PORT": "9000", "MCP_SECRET_PATH": "/custom"}):
            port, path = _get_http_runtime()

        assert port == 9000
        assert path == "/custom"

    def test_http_runtime_uses_defaults(self):
        """HTTP runtime should use defaults when env vars not set."""
        from ha_mcp.__main__ import _get_http_runtime

        # Clear any existing env vars
        env = os.environ.copy()
        env.pop("MCP_PORT", None)
        env.pop("MCP_SECRET_PATH", None)

        with patch.dict(os.environ, env, clear=True):
            port, path = _get_http_runtime()

        assert port == 8086
        assert path == "/mcp"
