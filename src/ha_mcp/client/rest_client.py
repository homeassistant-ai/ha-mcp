"""
Home Assistant HTTP client with authentication and error handling.
"""

import asyncio
import json
import logging
from typing import Any

import httpx

from ..config import get_global_settings

logger = logging.getLogger(__name__)


class HomeAssistantError(Exception):
    """Base exception for Home Assistant API errors."""

    pass


class HomeAssistantConnectionError(HomeAssistantError):
    """Connection error to Home Assistant."""

    pass


class HomeAssistantAuthError(HomeAssistantError):
    """Authentication error with Home Assistant."""

    pass


class HomeAssistantAPIError(HomeAssistantError):
    """API error from Home Assistant."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class HomeAssistantClient:
    """Authenticated HTTP client for Home Assistant API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: int | None = None,
    ):
        """
        Initialize Home Assistant client.

        Args:
            base_url: Home Assistant URL (defaults to config)
            token: Long-lived access token (defaults to config)
            timeout: Request timeout in seconds (defaults to config)
        """
        settings = get_global_settings()

        self.base_url = (base_url or settings.homeassistant_url).rstrip("/")
        self.token = token or settings.homeassistant_token
        self.timeout = timeout or settings.timeout

        # Create HTTP client with authentication headers
        self.httpx_client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(self.timeout),
        )

        logger.info(f"Initialized Home Assistant client for {self.base_url}")

    async def __aenter__(self) -> 'HomeAssistantClient':
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def close(self) -> None:
        """Close HTTP client."""
        await self.httpx_client.aclose()
        logger.debug("Closed Home Assistant client")

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """
        Make authenticated request to Home Assistant API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (without /api prefix)
            **kwargs: Additional arguments for httpx request

        Returns:
            Response data as dictionary

        Raises:
            HomeAssistantConnectionError: Connection failed
            HomeAssistantAuthError: Authentication failed
            HomeAssistantAPIError: API error
        """
        try:
            response = await self.httpx_client.request(method, endpoint, **kwargs)

            # Handle authentication errors
            if response.status_code == 401:
                raise HomeAssistantAuthError("Invalid authentication token")

            # Handle other HTTP errors
            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {"message": response.text}

                raise HomeAssistantAPIError(
                    f"API error: {response.status_code} - {error_data.get('message', 'Unknown error')}",
                    status_code=response.status_code,
                    response_data=error_data,
                )

            # Parse JSON response
            try:
                result: dict[str, Any] = response.json()
                return result
            except json.JSONDecodeError:
                # Some endpoints return empty responses
                return {}

        except httpx.ConnectError as e:
            raise HomeAssistantConnectionError(
                f"Failed to connect to Home Assistant: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise HomeAssistantConnectionError(f"Request timeout: {e}") from e
        except httpx.HTTPError as e:
            raise HomeAssistantConnectionError(f"HTTP error: {e}") from e

    async def get_config(self) -> dict[str, Any]:
        """Get Home Assistant configuration."""
        logger.debug("Fetching Home Assistant configuration")
        return await self._request("GET", "/config")

    async def get_states(self) -> list[dict[str, Any]]:
        """Get all entity states."""
        logger.debug("Fetching all entity states")
        result = await self._request("GET", "/states")
        if isinstance(result, list):
            return result
        else:
            return []

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        """
        Get specific entity state.

        Args:
            entity_id: Entity ID (e.g., 'light.living_room')

        Returns:
            Entity state data
        """
        logger.debug(f"Fetching state for entity: {entity_id}")
        return await self._request("GET", f"/states/{entity_id}")

    async def set_entity_state(
        self, entity_id: str, state: str, attributes: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Set entity state.

        Args:
            entity_id: Entity ID
            state: New state value
            attributes: Optional attributes dictionary

        Returns:
            Updated entity state
        """
        logger.debug(f"Setting state for entity {entity_id} to {state}")

        payload: dict[str, Any] = {"state": state}
        if attributes:
            payload["attributes"] = attributes

        return await self._request("POST", f"/states/{entity_id}", json=payload)

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Call Home Assistant service.

        Args:
            domain: Service domain (e.g., 'light', 'climate')
            service: Service name (e.g., 'turn_on', 'set_temperature')
            data: Optional service data

        Returns:
            Service response data
        """
        logger.debug(f"Calling service {domain}.{service}")

        payload = data or {}
        result = await self._request(
            "POST", f"/services/{domain}/{service}", json=payload
        )
        if isinstance(result, list):
            return result
        else:
            return []

    async def get_services(self) -> dict[str, Any]:
        """Get all available services."""
        logger.debug("Fetching available services")
        return await self._request("GET", "/services")

    async def get_history(
        self,
        entity_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[list[dict[str, Any]]]:
        """
        Get historical data.

        Args:
            entity_id: Optional entity ID to filter
            start_time: Optional start time (ISO format)
            end_time: Optional end time (ISO format)

        Returns:
            Historical data
        """
        logger.debug(f"Fetching history for entity: {entity_id}")

        params = {}
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time

        endpoint = "/history/period"
        if entity_id:
            endpoint += f"/{entity_id}"

        result = await self._request("GET", endpoint, params=params)
        if isinstance(result, list):
            return result
        else:
            return []

    async def get_logbook(
        self,
        entity_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get logbook entries.

        Args:
            entity_id: Optional entity ID to filter
            start_time: Optional start time (ISO format) - used as URL path component
            end_time: Optional end time (ISO format) - used as query parameter

        Returns:
            Logbook entries
        """
        logger.debug(f"Fetching logbook entries for entity: {entity_id}, start: {start_time}, end: {end_time}")

        # Build endpoint - start_time goes in URL path if provided
        if start_time:
            endpoint = f"/logbook/{start_time}"
        else:
            endpoint = "/logbook"

        # Build query parameters
        params = {}
        if entity_id:
            params["entity"] = entity_id
        if end_time:
            params["end_time"] = end_time

        result = await self._request("GET", endpoint, params=params)
        if isinstance(result, list):
            return result
        else:
            return []

    async def fire_event(
        self, event_type: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Fire Home Assistant event.

        Args:
            event_type: Event type name
            data: Optional event data

        Returns:
            Event response
        """
        logger.debug(f"Firing event: {event_type}")

        payload = data or {}
        return await self._request("POST", f"/events/{event_type}", json=payload)

    async def render_template(self, template: str) -> str:
        """
        Render Home Assistant template.

        Args:
            template: Template string

        Returns:
            Rendered template
        """
        logger.debug("Rendering template")

        payload = {"template": template}
        response = await self._request("POST", "/template", json=payload)
        result = response.get("result")
        return str(result) if result is not None else ""

    async def check_config(self) -> dict[str, Any]:
        """Check Home Assistant configuration."""
        logger.debug("Checking configuration")
        return await self._request("POST", "/config/core/check_config")

    async def get_error_log(self) -> str:
        """Get Home Assistant error log."""
        logger.debug("Fetching error log")
        response = await self._request("GET", "/error_log")
        return response if isinstance(response, str) else str(response)

    async def test_connection(self) -> tuple[bool, str | None]:
        """
        Test connection to Home Assistant.

        Returns:
            tuple: (success, error_message)
        """
        try:
            config = await self.get_config()
            if config.get("location_name"):
                logger.info(
                    f"Successfully connected to Home Assistant: {config['location_name']}"
                )
                return True, None
            else:
                return False, "Invalid response from Home Assistant"
        except Exception as e:
            logger.error(f"Failed to connect to Home Assistant: {e}")
            return False, str(e)

    async def get_system_health(self) -> dict[str, Any]:
        """Get system health information."""
        logger.debug("Fetching system health")
        try:
            return await self._request("GET", "/system_health/info")
        except HomeAssistantAPIError:
            # System health might not be available in all HA instances
            return {"status": "unknown", "message": "System health not available"}

    # Automation Configuration Management

    async def _resolve_automation_id(self, identifier: str) -> str:
        """
        Convert entity_id to unique_id if needed, or return unique_id as-is.

        Args:
            identifier: Either entity_id (automation.xxx) or unique_id

        Returns:
            The unique_id for configuration API

        Raises:
            HomeAssistantAPIError: If automation not found
        """
        # If it looks like an entity_id, convert to unique_id
        if identifier.startswith("automation."):
            try:
                state = await self.get_entity_state(identifier)
                unique_id = state.get("attributes", {}).get("id")
                if not unique_id:
                    raise HomeAssistantAPIError(
                        f"Automation {identifier} has no unique_id attribute",
                        status_code=404,
                    )
                logger.debug(
                    f"Converted entity_id {identifier} to unique_id {unique_id}"
                )
                return str(unique_id)
            except Exception as e:
                raise HomeAssistantAPIError(
                    f"Failed to resolve automation {identifier}: {str(e)}",
                    status_code=404,
                )
        else:
            # Assume it's already a unique_id
            return identifier

    async def get_automation_config(self, identifier: str) -> dict[str, Any]:
        """
        Get automation configuration by unique_id or entity_id.

        Args:
            identifier: Either automation entity_id (automation.xxx) or unique_id

        Returns:
            Automation configuration dictionary

        Raises:
            HomeAssistantAPIError: If automation not found or API error
        """
        unique_id = await self._resolve_automation_id(identifier)
        logger.debug(f"Fetching automation config for unique_id: {unique_id}")

        try:
            response = await self._request(
                "GET", f"/config/automation/config/{unique_id}"
            )
            return response
        except Exception as e:
            if "404" in str(e):
                raise HomeAssistantAPIError(
                    f"Automation not found: {identifier} (unique_id: {unique_id})",
                    status_code=404,
                )
            raise

    async def upsert_automation_config(
        self, config: dict[str, Any], identifier: str | None = None
    ) -> dict[str, Any]:
        """
        Create new automation or update existing one.

        Args:
            config: Automation configuration dictionary
            identifier: Optional automation entity_id or unique_id (None = create new)

        Returns:
            Result with automation unique_id and status

        Raises:
            HomeAssistantAPIError: If configuration invalid or API error
        """
        import time

        # Generate unique_id for new automation if not provided
        if identifier is None:
            unique_id = str(int(time.time() * 1000))
            operation = "created"
            logger.debug(f"Creating new automation with unique_id: {unique_id}")
        else:
            unique_id = await self._resolve_automation_id(identifier)
            operation = "updated"
            logger.debug(f"Updating automation with unique_id: {unique_id}")

        # Add unique_id to config for updates
        if unique_id and "id" not in config:
            config = {**config, "id": unique_id}

        try:
            response = await self._request(
                "POST", f"/config/automation/config/{unique_id}", json=config
            )

            # For new automations, query Home Assistant to get the actual entity_id that was assigned
            actual_entity_id = None
            if operation == "created":
                try:
                    # Give Home Assistant a moment to register the entity
                    import asyncio

                    await asyncio.sleep(1)

                    # Get all automations and find the one with our unique_id
                    states = await self.get_states()
                    for state in states:
                        if state.get("entity_id", "").startswith("automation."):
                            attributes = state.get("attributes", {})
                            if attributes.get("id") == unique_id:
                                actual_entity_id = state.get("entity_id")
                                logger.debug(
                                    f"Found actual entity_id for unique_id {unique_id}: {actual_entity_id}"
                                )
                                break

                    if not actual_entity_id:
                        # Fallback to predicted entity_id if we can't find it
                        actual_entity_id = f"automation.{config.get('alias', unique_id).lower().replace(' ', '_').replace('-', '_')}"
                        logger.warning(
                            f"Could not find actual entity_id for unique_id {unique_id}, using predicted: {actual_entity_id}"
                        )

                except Exception as e:
                    logger.warning(
                        f"Failed to query actual entity_id for unique_id {unique_id}: {e}"
                    )
                    # Fallback to predicted entity_id
                    actual_entity_id = f"automation.{config.get('alias', unique_id).lower().replace(' ', '_').replace('-', '_')}"

            return {
                "unique_id": unique_id,
                "entity_id": actual_entity_id,
                "result": response.get("result", "ok"),
                "operation": operation,
            }
        except Exception as e:
            if "400" in str(e):
                raise HomeAssistantAPIError(
                    f"Invalid automation configuration: {str(e)}", status_code=400
                )
            raise

    async def delete_automation_config(self, identifier: str) -> dict[str, Any]:
        """
        Delete automation configuration by entity_id or unique_id.

        Args:
            identifier: Either automation entity_id (automation.xxx) or unique_id

        Returns:
            Deletion result

        Raises:
            HomeAssistantAPIError: If automation not found or API error
        """
        unique_id = await self._resolve_automation_id(identifier)
        logger.debug(f"Deleting automation config for unique_id: {unique_id}")

        try:
            response = await self._request(
                "DELETE", f"/config/automation/config/{unique_id}"
            )
            return {
                "identifier": identifier,
                "unique_id": unique_id,
                "result": response.get("result", "ok"),
                "operation": "deleted",
            }
        except Exception as e:
            if "404" in str(e):
                raise HomeAssistantAPIError(
                    f"Automation not found: {identifier} (unique_id: {unique_id})",
                    status_code=404,
                )
            raise

    async def send_websocket_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send message via WebSocket and wait for response."""
        ws_client = None
        try:
            # Use client's own URL and token for WebSocket connection
            from .websocket_client import HomeAssistantWebSocketClient

            ws_client = HomeAssistantWebSocketClient(self.base_url, self.token)

            # Connect if not already connected
            if not ws_client.is_connected:
                await ws_client.connect()

            # Special handling for render_template which returns an event with the actual result
            if message.get("type") == "render_template":
                return await self._handle_render_template(ws_client, message)

            # Extract command type and parameters for other commands
            message_copy = message.copy()
            command_type = message_copy.pop("type")
            result = await ws_client.send_command(command_type, **message_copy)

            return result
        except Exception as e:
            logger.error(f"WebSocket message failed: {e}")
            return {"success": False, "error": str(e)}
        finally:
            # Clean up WebSocket connection
            if ws_client and ws_client.is_connected:
                await ws_client.disconnect()

    async def _handle_render_template(
        self, ws_client: Any, message: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle render_template WebSocket command with event-based response."""
        # Generate our own message ID to track the response
        message_id = ws_client.get_next_message_id()

        # Construct the full message with proper ID
        full_message = {
            "id": message_id,
            "type": "render_template",
            "template": message.get("template"),
            "timeout": message.get("timeout", 3),
            "report_errors": message.get("report_errors", True),
        }

        # Create futures for both result and event responses
        result_future = ws_client.register_pending_response(message_id)
        event_future = ws_client.register_render_template_event(message_id)

        # Use WebSocket client's send helper to transmit the message
        try:
            await ws_client.send_json_message(full_message)
        except Exception as e:
            ws_client.cancel_pending_response(message_id)
            ws_client.cancel_render_template_event(message_id)
            raise e

        try:
            # Wait for the initial result response (should be success with null result)
            result_response = await asyncio.wait_for(
                result_future, timeout=message.get("timeout", 3) + 2
            )
            logger.debug(f"WebSocket render_template result: {result_response}")

            if not result_response.get("success"):
                ws_client.cancel_render_template_event(message_id)
                error = result_response.get("error", "Unknown error")
                return {
                    "success": False,
                    "error": str(error),
                    "template": message.get("template"),
                }

            # Wait for the event with the actual template result
            try:
                event_response = await asyncio.wait_for(
                    event_future, timeout=message.get("timeout", 3) + 1
                )
                logger.debug(f"WebSocket render_template event: {event_response}")

                # Extract template result from event
                if "event" in event_response and "result" in event_response["event"]:
                    template_result = event_response["event"]["result"]
                    listeners_info = event_response["event"].get("listeners", {})

                    return {
                        "success": True,
                        "result": template_result,
                        "template": message.get("template"),
                        "listeners": listeners_info,
                    }
                else:
                    return {
                        "success": False,
                        "error": "Invalid event response format",
                        "template": message.get("template"),
                    }

            except TimeoutError:
                ws_client.cancel_render_template_event(message_id)
                return {
                    "success": False,
                    "error": "Event timeout - template result not received",
                    "template": message.get("template"),
                }

        except TimeoutError:
            ws_client.cancel_pending_response(message_id)
            ws_client.cancel_render_template_event(message_id)
            return {
                "success": False,
                "error": "Command timeout",
                "template": message.get("template"),
            }
        except Exception as e:
            ws_client.cancel_pending_response(message_id)
            ws_client.cancel_render_template_event(message_id)
            return {
                "success": False,
                "error": str(e),
                "template": message.get("template"),
            }

    async def get_script_config(self, script_id: str) -> dict[str, Any]:
        """Get Home Assistant script configuration by script_id."""
        try:
            endpoint = f"config/script/config/{script_id}"
            response = await self._request("GET", endpoint)

            return {"success": True, "script_id": script_id, "config": response}
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                raise HomeAssistantAPIError(
                    f"Script not found: {script_id}", status_code=404
                )
            raise
        except Exception as e:
            logger.error(f"Failed to get script config for {script_id}: {e}")
            raise

    async def upsert_script_config(
        self, config: dict[str, Any], script_id: str
    ) -> dict[str, Any]:
        """Create or update Home Assistant script configuration."""
        try:
            endpoint = f"config/script/config/{script_id}"

            # Validate required fields
            if "alias" not in config:
                config["alias"] = script_id
            if "sequence" not in config:
                raise ValueError("Script configuration must include 'sequence'")

            response = await self._request("POST", endpoint, json=config)

            return {
                "success": True,
                "script_id": script_id,
                "result": response.get("result", "ok"),
                "operation": "created" if response.get("result") == "ok" else "updated",
            }
        except Exception as e:
            logger.error(f"Failed to upsert script config for {script_id}: {e}")
            raise

    async def delete_script_config(self, script_id: str) -> dict[str, Any]:
        """Delete Home Assistant script configuration."""
        try:
            endpoint = f"config/script/config/{script_id}"
            response = await self._request("DELETE", endpoint)

            return {
                "success": True,
                "script_id": script_id,
                "result": response.get("result", "ok"),
                "operation": "deleted",
            }
        except Exception as e:
            if "404" in str(e):
                raise HomeAssistantAPIError(
                    f"Script not found: {script_id}", status_code=404
                )
            raise


async def create_client() -> HomeAssistantClient:
    """Create and return a new Home Assistant client."""
    return HomeAssistantClient()


async def test_connection_with_config() -> tuple[bool, str | None]:
    """Test connection using configuration settings."""
    async with HomeAssistantClient() as client:
        return await client.test_connection()
