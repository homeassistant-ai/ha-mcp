"""Unit tests for Zigbee2MQTT tools helper functions."""

import pytest

from ha_mcp.tools.tools_zigbee2mqtt import (
    _build_mqtt_topic,
    _extract_friendly_name_from_entity_id,
    _is_likely_z2m_entity,
    _is_z2m_device,
)


class TestExtractFriendlyNameFromEntityId:
    """Test _extract_friendly_name_from_entity_id function."""

    def test_basic_extraction(self):
        """Basic entity ID without Z2M suffix."""
        result = _extract_friendly_name_from_entity_id("light.bedroom_lamp")
        assert result == "bedroom_lamp"

    def test_linkquality_suffix(self):
        """Entity with _linkquality suffix is stripped."""
        result = _extract_friendly_name_from_entity_id("sensor.motion_linkquality")
        assert result == "motion"

    def test_occupancy_suffix(self):
        """Entity with _occupancy suffix is stripped."""
        result = _extract_friendly_name_from_entity_id(
            "binary_sensor.living_room_motion_occupancy"
        )
        assert result == "living_room_motion"

    def test_temperature_suffix(self):
        """Entity with _temperature suffix is stripped."""
        result = _extract_friendly_name_from_entity_id(
            "sensor.bedroom_temp_sensor_temperature"
        )
        assert result == "bedroom_temp_sensor"

    def test_battery_suffix(self):
        """Entity with _battery suffix is stripped."""
        result = _extract_friendly_name_from_entity_id("sensor.motion_sensor_battery")
        assert result == "motion_sensor"

    def test_action_suffix(self):
        """Entity with _action suffix is stripped."""
        result = _extract_friendly_name_from_entity_id("sensor.button_action")
        assert result == "button"

    def test_light_suffix(self):
        """Entity with _light suffix is stripped."""
        result = _extract_friendly_name_from_entity_id("light.bedroom_light")
        assert result == "bedroom"

    def test_contact_suffix(self):
        """Entity with _contact suffix is stripped."""
        result = _extract_friendly_name_from_entity_id("binary_sensor.door_contact")
        assert result == "door"

    def test_illuminance_lux_suffix(self):
        """Entity with _illuminance_lux suffix is stripped."""
        result = _extract_friendly_name_from_entity_id(
            "sensor.motion_illuminance_lux"
        )
        assert result == "motion"

    def test_no_suffix_to_strip(self):
        """Entity without recognizable Z2M suffix."""
        result = _extract_friendly_name_from_entity_id("switch.garage_door")
        assert result == "garage_door"


class TestBuildMqttTopic:
    """Test _build_mqtt_topic function."""

    def test_base_topic(self):
        """Build base MQTT topic without suffix."""
        result = _build_mqtt_topic("living_room_motion")
        assert result == "zigbee2mqtt/living_room_motion"

    def test_topic_with_suffix(self):
        """Build MQTT topic with suffix."""
        result = _build_mqtt_topic("bedroom_light", "set")
        assert result == "zigbee2mqtt/bedroom_light/set"

    def test_topic_with_action_suffix(self):
        """Build MQTT topic with action suffix."""
        result = _build_mqtt_topic("button", "action")
        assert result == "zigbee2mqtt/button/action"

    def test_topic_empty_suffix(self):
        """Build MQTT topic with empty suffix."""
        result = _build_mqtt_topic("sensor", "")
        assert result == "zigbee2mqtt/sensor"

    def test_topic_with_availability_suffix(self):
        """Build MQTT topic with availability suffix."""
        result = _build_mqtt_topic("device", "availability")
        assert result == "zigbee2mqtt/device/availability"


class TestIsLikelyZ2mEntity:
    """Test _is_likely_z2m_entity function."""

    def test_linkquality_attribute(self):
        """Entity with linkquality attribute is detected as Z2M."""
        entity = {
            "entity_id": "sensor.test",
            "attributes": {"linkquality": 150},
        }
        assert _is_likely_z2m_entity(entity) is True

    def test_update_available_attribute(self):
        """Entity with update_available attribute is detected as Z2M."""
        entity = {
            "entity_id": "sensor.test",
            "attributes": {"update_available": False},
        }
        assert _is_likely_z2m_entity(entity) is True

    def test_last_seen_attribute(self):
        """Entity with last_seen attribute is detected as Z2M."""
        entity = {
            "entity_id": "sensor.test",
            "attributes": {"last_seen": "2024-01-01T00:00:00Z"},
        }
        assert _is_likely_z2m_entity(entity) is True

    def test_linkquality_entity_pattern(self):
        """Entity with _linkquality in ID is detected as Z2M."""
        entity = {
            "entity_id": "sensor.bedroom_motion_linkquality",
            "attributes": {},
        }
        assert _is_likely_z2m_entity(entity) is True

    def test_occupancy_entity_pattern(self):
        """Entity with _occupancy in ID is detected as Z2M."""
        entity = {
            "entity_id": "binary_sensor.motion_occupancy",
            "attributes": {},
        }
        assert _is_likely_z2m_entity(entity) is True

    def test_action_entity_pattern(self):
        """Entity with _action in ID is detected as Z2M."""
        entity = {
            "entity_id": "sensor.button_action",
            "attributes": {},
        }
        assert _is_likely_z2m_entity(entity) is True

    def test_temperature_entity_pattern(self):
        """Entity with _temperature in ID is detected as Z2M."""
        entity = {
            "entity_id": "sensor.living_room_temperature",
            "attributes": {},
        }
        assert _is_likely_z2m_entity(entity) is True

    def test_non_z2m_entity(self):
        """Entity without Z2M indicators is not detected."""
        entity = {
            "entity_id": "light.kitchen",
            "attributes": {"brightness": 255, "friendly_name": "Kitchen Light"},
        }
        assert _is_likely_z2m_entity(entity) is False

    def test_non_z2m_sensor(self):
        """Generic sensor without Z2M patterns is not detected."""
        entity = {
            "entity_id": "sensor.cpu_usage",
            "attributes": {"unit_of_measurement": "%"},
        }
        assert _is_likely_z2m_entity(entity) is False


class TestIsZ2mDevice:
    """Test _is_z2m_device function."""

    def test_mqtt_identifier(self):
        """Device with mqtt in identifiers is detected as Z2M."""
        device = {
            "identifiers": [["mqtt", "0x00158d0001234567"]],
        }
        assert _is_z2m_device(device) is True

    def test_zigbee2mqtt_identifier(self):
        """Device with zigbee2mqtt in identifiers is detected as Z2M."""
        device = {
            "identifiers": [["zigbee2mqtt", "device_name"]],
        }
        assert _is_z2m_device(device) is True

    def test_xiaomi_manufacturer(self):
        """Device with Xiaomi manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "Xiaomi",
        }
        assert _is_z2m_device(device) is True

    def test_aqara_manufacturer(self):
        """Device with Aqara manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "Aqara",
        }
        assert _is_z2m_device(device) is True

    def test_ikea_manufacturer(self):
        """Device with IKEA manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "IKEA of Sweden",
        }
        assert _is_z2m_device(device) is True

    def test_philips_manufacturer(self):
        """Device with Philips manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "Signify Netherlands B.V.",
        }
        assert _is_z2m_device(device) is True

    def test_sonoff_manufacturer(self):
        """Device with SONOFF manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "SONOFF Zigbee",
        }
        assert _is_z2m_device(device) is True

    def test_tuya_manufacturer(self):
        """Device with TuYa manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "TuYa",
        }
        assert _is_z2m_device(device) is True

    def test_tuya_model_pattern(self):
        """Device with TuYa model pattern (TS*) is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "Unknown",
            "model": "TS0601",
        }
        assert _is_z2m_device(device) is True

    def test_xiaomi_model_pattern(self):
        """Device with Xiaomi model pattern (lumi.*) is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "",
            "model": "lumi.sensor_motion.aq2",
        }
        assert _is_z2m_device(device) is True

    def test_aqara_model_patterns(self):
        """Device with Aqara model patterns is detected as Z2M."""
        devices = [
            {"identifiers": [], "model": "RTCGQ11LM"},
            {"identifiers": [], "model": "MCCGQ11LM"},
            {"identifiers": [], "model": "WXKG11LM"},
            {"identifiers": [], "model": "WSDCGQ11LM"},
        ]
        for device in devices:
            assert _is_z2m_device(device) is True

    def test_via_device_id(self):
        """Device with via_device_id is detected as Z2M."""
        device = {
            "identifiers": [],
            "via_device_id": "coordinator_device_id",
        }
        assert _is_z2m_device(device) is True

    def test_non_z2m_device(self):
        """Device without Z2M indicators is not detected."""
        device = {
            "identifiers": [["homeassistant", "weather"]],
            "manufacturer": "Weather Underground",
            "model": "Weather Station",
        }
        assert _is_z2m_device(device) is False

    def test_empty_device(self):
        """Empty device dict is not detected as Z2M."""
        device = {}
        assert _is_z2m_device(device) is False

    def test_none_values(self):
        """Device with None values is handled gracefully."""
        device = {
            "identifiers": [],
            "manufacturer": None,
            "model": None,
            "via_device_id": None,
        }
        assert _is_z2m_device(device) is False

    def test_case_insensitive_manufacturer(self):
        """Manufacturer matching is case-insensitive."""
        device = {
            "identifiers": [],
            "manufacturer": "xiaomi",  # lowercase
        }
        assert _is_z2m_device(device) is True

    def test_innr_manufacturer(self):
        """Device with Innr manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "innr",
        }
        assert _is_z2m_device(device) is True

    def test_sengled_manufacturer(self):
        """Device with Sengled manufacturer is detected as Z2M."""
        device = {
            "identifiers": [],
            "manufacturer": "Sengled",
        }
        assert _is_z2m_device(device) is True
