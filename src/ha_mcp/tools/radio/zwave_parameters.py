"""Entity-independent reads through Home Assistant's Z-Wave JS API."""

from __future__ import annotations

from typing import Any, NoReturn

from ...client.rest_client import (
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ...errors import ErrorCode, create_error_response
from ..helpers import exception_to_structured_error, raise_tool_error
from .base import ok, ws_call


def _invalid(message: str) -> NoReturn:
    raise_tool_error(
        create_error_response(ErrorCode.VALIDATION_INVALID_PARAMETER, message)
    )


def _integer(args: dict[str, Any], name: str, minimum: int, maximum: int) -> None:
    value = args.get(name)
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid(f"params.{name} must be an integer between {minimum} and {maximum}")


def _validate(action: str, args: dict[str, Any]) -> bool:
    """Validate read arguments before traffic; return whether refresh was requested."""
    device_id = args["device_id"]
    if not isinstance(device_id, str) or not device_id.strip():
        _invalid("device_id must be a non-empty Home Assistant device ID")
    refresh = args.get("refresh", False)
    if not isinstance(refresh, bool):
        _invalid("params.refresh must be a boolean")
    if action == "get_config_params" and refresh:
        _invalid(
            "get_config_params is cache-only; use get_config_param to refresh one parameter"
        )
    if action == "get_config_param":
        _integer(args, "property", 0, 65535)
        if "endpoint" in args:
            _integer(args, "endpoint", 0, 127)
        if args.get("property_key") is not None:
            _integer(args, "property_key", 1, 0xFFFFFFFF)
        if refresh and (
            args.get("endpoint", 0) != 0 or args.get("property_key") is not None
        ):
            _invalid(
                "HA's raw-read API cannot refresh endpoint or partial parameters; use a cached read"
            )

    return refresh


def _no_raw_value(context: dict[str, Any], details: str | None = None) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Device parameter read returned no value; no cached fallback was used",
            details=details,
            context=context,
            suggestions=[
                "Verify that the parameter number is supported by this device",
                "Check node availability or wake a sleeping device before retrying",
                "Inspect Home Assistant logs if the failure persists; unknown_error does not identify the cause",
            ],
        )
    )


def _raw_read_error(reply: dict[str, Any], context: dict[str, Any]) -> None:
    # HA reports unknown_error when the raw client asserts on an empty result;
    # the code can also mean another upstream failure, so preserve its details.
    if reply.get("error_code") == "unknown_error":
        _no_raw_value(context, reply.get("error"))


async def _read_raw(client: Any, device_id: str, property_: int) -> Any:
    context: dict[str, Any] = {
        "radio": "zwave",
        "action": "get_config_param",
        "ws_type": "zwave_js/get_raw_config_parameter",
        "device_id": device_id,
        "property": property_,
        "refresh_requested": True,
    }
    try:
        raw = await ws_call(
            client,
            context["ws_type"],
            device_id=device_id,
            property=property_,
            context=context,
            on_error=_raw_read_error,
        )
    except (HomeAssistantCommandTimeout, HomeAssistantConnectionError) as error:
        # The REST bridge wraps its command deadline in a connection error.
        # A socket failure without that cause must remain a transport failure.
        if isinstance(error, HomeAssistantCommandTimeout) or isinstance(
            error.__cause__, HomeAssistantCommandTimeout
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.TIMEOUT_WEBSOCKET,
                    "Device parameter read did not answer before the command timeout; "
                    "no cached fallback was used. The Get may still be queued until the node wakes",
                    context=context,
                    suggestions=[
                        "Wake/check the device and allow any queued Get to finish before retrying"
                    ],
                )
            )
        exception_to_structured_error(error, context=context)
    if not isinstance(raw, dict) or raw.get("value") is None:
        _no_raw_value(context)
    return raw["value"]


async def read(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Read cached values, or explicitly request one root parameter from the device."""
    refresh = _validate(action, args)
    device_id = args["device_id"]

    parameters = await ws_call(
        client,
        "zwave_js/get_config_parameters",
        device_id=device_id,
        context={"device_id": device_id},
    )
    if not isinstance(parameters, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "HA returned an invalid configuration-parameter response",
                context={"device_id": device_id},
            )
        )
    if action == "get_config_params":
        return ok(
            "zwave",
            action,
            device_id=device_id,
            parameters=parameters,
            source="cache",
            refresh_requested=False,
        )

    property_ = args["property"]
    endpoint = args.get("endpoint", 0)
    property_key = args.get("property_key")
    matches = [
        {"value_id": value_id, **parameter}
        for value_id, parameter in parameters.items()
        if parameter.get("property") == property_
        and parameter.get("endpoint") == endpoint
        and parameter.get("property_key") == property_key
    ]
    if len(matches) > 1:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "HA returned multiple matching configuration parameters",
                context={"device_id": device_id, "property": property_},
            )
        )
    if not refresh and not matches:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                "Configuration parameter not found in the Z-Wave JS cache",
                context={
                    "device_id": device_id,
                    "property": property_,
                    "endpoint": endpoint,
                    "property_key": property_key,
                },
                suggestions=[
                    "Use get_config_params to inspect available endpoints and property_key bit masks",
                    "For a full root parameter, request params.refresh=True to query the device",
                ],
            )
        )

    parameter = (
        matches[0]
        if matches
        else {
            "property": property_,
            "endpoint": 0,
            "property_key": None,
            "metadata": None,
        }
    )
    if refresh:
        parameter = {
            **parameter,
            "value": await _read_raw(client, device_id, property_),
        }

    result = ok(
        "zwave",
        action,
        device_id=device_id,
        parameter=parameter,
        source="device_request" if refresh else "cache",
        refresh_requested=refresh,
    )
    if parameter.get("value") is None:
        result["warnings"] = [
            "Z-Wave JS has no cached value for this parameter; the device was not queried"
        ]
    if refresh and matches:
        result["metadata_source"] = "cache"
    return result
