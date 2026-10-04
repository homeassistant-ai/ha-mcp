# Z-Wave configuration parameter reads

`ha_manage_radio(radio="zwave", ...)` reads configuration parameters directly
from Z-Wave JS. It never creates or enables parameter entities or sets device
configuration. An administrator's HA connection is required by the underlying
WebSocket APIs.

Use a Home Assistant `device_id`, found with
`ha_get_device(integration="zwave_js")`. An existing `entity_id` can optionally
resolve that device, including a disabled registry entity. A parameter entity
is not required. Bare node numbers are not accepted: device IDs disambiguate
nodes across multiple Z-Wave controllers.

## Examples

List all cached parameters across endpoints, retaining HA's value IDs and
metadata (including its type, plus label, description, min/max, default, unit,
states and readable/writeable flags when supplied), and each parameter's
`configuration_value_type`:

```python
ha_manage_radio(radio="zwave", action="get_config_params", device_id="example_device_id")
```

Read parameter 3 from the cache:

```python
ha_manage_radio(radio="zwave", action="get_config_param",
                device_id="example_device_id", params={"property": 3})
```

Read a partial parameter at endpoint 1. `property_key` is the bit mask advertised
by `get_config_params`, not a sub-parameter index. Values are already decoded
by Z-Wave JS; the tool does not apply the mask again.

```python
ha_manage_radio(radio="zwave", action="get_config_param",
                device_id="example_device_id",
                params={"property": 3, "endpoint": 1, "property_key": 15})
```

Explicitly query a full root parameter, including one missing from the cache:

```python
ha_manage_radio(radio="zwave", action="get_config_param",
                device_id="example_device_id",
                params={"property": 3, "refresh": True})
```

## Freshness and limits

- `get_config_params` and ordinary `get_config_param` use
  `zwave_js/get_config_parameters`. HA reads `node.get_configuration_values()`;
  it does not send a device request. Results carry `source="cache"` and
  `refresh_requested=false`. The API supplies no last-read timestamp, so cache
  age is unknown. An asleep or unavailable node may still have cached values.
- `refresh=True` calls `zwave_js/get_raw_config_parameter`. HA awaits the
  Z-Wave client's raw read on endpoint 0; the server invokes Configuration CC
  `get`. Results carry `source="device_request"` and `refresh_requested=true`.
  This describes a requested read, not an independently verified freshness
  timestamp. Metadata still comes from the cache (`metadata_source="cache"`
  when available), and is `null` for an unknown full parameter. Partial metadata
  is never attached to a raw full value.
- HA's raw-read schema accepts only `device_id` and `property`. Refreshing an
  endpoint other than 0 or a `property_key` bit mask is rejected before any
  command is sent. Cached reads support both. `get_config_params` is cache-only.
- Missing cached parameters raise a structured error; they never trigger an
  implicit poll. Unknown cached values remain `null` with a warning. A device
  request returning no value raises an error without substituting cached data.
- Permission failures, missing/unloaded integrations or nodes, unknown commands
  on unsupported HA versions, and device/transport timeouts propagate as tool
  errors with recovery suggestions. A refresh timeout means the command did not
  answer before the wait expired; its Get may still be queued until the node
  wakes. Wake/check the node and allow that queued request to finish before
  retrying. An HA `unknown_error` on the raw read also produces no value: verify
  parameter support and node availability, and inspect HA logs if it persists.
  The error code alone does not establish the cause.
  There is no entity-enabling or broader node-refresh fallback.
