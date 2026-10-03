"""Entity-independent reads through Home Assistant's Z-Wave JS API."""

from __future__ import annotations

from typing import Any, NoReturn

from ...errors import ErrorCode, create_error_response
from ..helpers import raise_tool_error
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
    """Reject unsupported refresh targets before any network traffic."""
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
        raw = await ws_call(
            client,
            "zwave_js/get_raw_config_parameter",
            device_id=device_id,
            property=property_,
            context={
                "device_id": device_id,
                "property": property_,
                "refresh_requested": True,
            },
        )
        if not isinstance(raw, dict) or raw.get("value") is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Device parameter read returned no value; no cached fallback was used",
                    context={
                        "device_id": device_id,
                        "property": property_,
                        "refresh_requested": True,
                    },
                    suggestions=[
                        "Check node availability or wake a sleeping device before retrying"
                    ],
                )
            )
        parameter = {**parameter, "value": raw["value"]}

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
