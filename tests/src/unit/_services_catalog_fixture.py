"""A production-shaped service catalog captured from Home Assistant 2026.11.0.

The records are verbatim ``GET /api/services`` / ``ha_mcp_tools/services_list``
output for the services this suite exercises, trimmed to the keys under test.
``TRANSLATIONS`` is the flat ``frontend/get_translations`` map for the same
entries, also verbatim. Keeping the fixture in Core's shape is what lets the
tests catch drift in the server's merge instead of restating it.
"""

from __future__ import annotations

from typing import Any

SERVICES: list[dict[str, Any]] = [
    {
        "domain": "light",
        "services": {
            "turn_on": {
                "fields": {
                    "transition": {
                        "filter": {"supported_features": [32]},
                        "selector": {
                            "number": {
                                "min": 0.0,
                                "max": 300.0,
                                "unit_of_measurement": "seconds",
                                "step": 1.0,
                                "mode": "slider",
                            }
                        },
                    },
                    "rgb_color": {
                        "filter": {
                            "attribute": {
                                "supported_color_modes": [
                                    "hs",
                                    "xy",
                                    "rgb",
                                    "rgbw",
                                    "rgbww",
                                ]
                            }
                        },
                        "example": "[255, 100, 100]",
                        "selector": {"color_rgb": {}},
                    },
                    "brightness_pct": {
                        "filter": {
                            "attribute": {
                                "supported_color_modes": [
                                    "brightness",
                                    "color_temp",
                                    "hs",
                                    "xy",
                                    "rgb",
                                    "rgbw",
                                    "rgbww",
                                ]
                            }
                        },
                        "selector": {
                            "number": {
                                "min": 0.0,
                                "max": 100.0,
                                "unit_of_measurement": "%",
                                "step": 1.0,
                                "mode": "slider",
                            }
                        },
                    },
                    "additional_fields": {
                        "collapsed": True,
                        "fields": {
                            "rgbw_color": {
                                "filter": {
                                    "attribute": {
                                        "supported_color_modes": [
                                            "hs",
                                            "xy",
                                            "rgb",
                                            "rgbw",
                                            "rgbww",
                                        ]
                                    }
                                },
                                "example": "[255, 100, 100, 50]",
                                "selector": {"object": {"multiple": False}},
                            },
                            "color_name": {
                                "filter": {
                                    "attribute": {
                                        "supported_color_modes": [
                                            "hs",
                                            "xy",
                                            "rgb",
                                            "rgbw",
                                            "rgbww",
                                        ]
                                    }
                                },
                                "selector": {
                                    "select": {
                                        "translation_key": "color_name",
                                        "options": [
                                            {"value": "red", "label": "Red"},
                                            {"value": "green", "label": "Green"},
                                            {"value": "blue", "label": "Blue"},
                                            {"value": "yellow", "label": "Yellow"},
                                            {"value": "orange", "label": "Orange"},
                                            {"value": "purple", "label": "Purple"},
                                        ],
                                    }
                                },
                            },
                        },
                    },
                },
                "target": {"entity": [{"domain": ["light"]}]},
            },
            "turn_off": {"fields": {}, "target": {"entity": [{"domain": ["light"]}]}},
        },
    },
    {
        "domain": "calendar",
        "services": {
            "get_events": {
                "fields": {
                    "start_date_time": {
                        "example": "2022-03-22 20:00:00",
                        "selector": {"datetime": {}},
                    },
                    "end_date_time": {
                        "example": "2022-03-22 22:00:00",
                        "selector": {"datetime": {}},
                    },
                    "duration": {"selector": {"duration": {"enable_second": True}}},
                },
                "target": {"entity": [{"domain": ["calendar"]}]},
                "response": {"optional": False},
            }
        },
    },
    {
        "domain": "conversation",
        "services": {
            "process": {
                "fields": {
                    "text": {
                        "example": "Turn all lights on",
                        "required": True,
                        "selector": {"text": {"multiline": False, "multiple": False}},
                    },
                    "language": {
                        "example": "NL",
                        "selector": {"text": {"multiline": False, "multiple": False}},
                    },
                },
                "response": {"optional": True},
            }
        },
    },
    {
        "domain": "zwave_js",
        "services": {
            "invoke_cc_api": {
                "fields": {
                    "command_class": {
                        "example": 132,
                        "required": True,
                        "selector": {"text": {"multiline": False, "multiple": False}},
                    },
                    "method_name": {
                        "example": "setInterval",
                        "required": True,
                        "selector": {"text": {"multiline": False, "multiple": False}},
                    },
                    "parameters": {
                        "example": "[1, 1]",
                        "required": True,
                        "selector": {"object": {"multiple": False}},
                    },
                },
                "description_placeholders": {
                    "api_docs_url": (
                        "https://zwave-js.github.io/node-zwave-js/#/api/CCs/index"
                    )
                },
            }
        },
    },
    {
        "domain": "fan",
        "services": {
            "increase_speed": {
                "fields": {
                    "additional_fields": {
                        "collapsed": True,
                        "fields": {
                            "percentage_step": {
                                "required": False,
                                "selector": {
                                    "number": {
                                        "min": 0.0,
                                        "max": 100.0,
                                        "unit_of_measurement": "%",
                                        "step": 1.0,
                                        "mode": "slider",
                                    }
                                },
                            }
                        },
                    }
                },
                "target": {"entity": [{"domain": ["fan"], "supported_features": [1]}]},
            }
        },
    },
    {
        "domain": "climate",
        "services": {
            "set_temperature": {
                "fields": {
                    "temperature_range": {
                        "fields": {
                            "target_temp_high": {
                                "filter": {"supported_features": [2]},
                                "selector": {"number": {"min": 0.0, "max": 100.0}},
                            },
                            "target_temp_low": {
                                "filter": {"supported_features": [2]},
                                "selector": {"number": {"min": 0.0, "max": 100.0}},
                            },
                        }
                    }
                },
                "target": {"entity": [{"domain": ["climate"]}]},
            }
        },
    },
    {
        "domain": "script",
        "services": {
            "on_the_way_home": {
                "name": "On the Way Home",
                "description": "Notifies both phones with ETA and visual confirmation",
                "fields": {},
                "response": {"optional": True},
            },
            "voice_play_music": {
                "fields": {
                    "player": {
                        "name": "Player",
                        "required": True,
                        "selector": {
                            "entity": {
                                "domain": ["media_player"],
                                "multiple": False,
                                "reorder": False,
                            }
                        },
                        "advanced": False,
                    },
                    "radio_mode": {
                        "name": "Radio mode",
                        "required": False,
                        "default": False,
                        "selector": {"boolean": {}},
                        "advanced": False,
                    },
                }
            },
        },
    },
    {
        "domain": "switch",
        "services": {
            "turn_on": {"fields": {}, "target": {"entity": [{"domain": ["switch"]}]}},
            "turn_off": {"fields": {}, "target": {"entity": [{"domain": ["switch"]}]}},
        },
    },
]

TRANSLATIONS: dict[str, str] = {
    "component.light.services.turn_on.name": "Turn on light",
    "component.light.services.turn_on.description": (
        "Turns on one or more lights and adjusts their properties, even when "
        "they are turned on already."
    ),
    "component.light.services.turn_on.fields.transition.name": "Transition",
    "component.light.services.turn_on.fields.transition.description": (
        "Duration it takes to get to next state."
    ),
    "component.light.services.turn_on.fields.rgb_color.name": "Color",
    "component.light.services.turn_on.fields.rgb_color.description": (
        "The color in RGB format. A list of three integers between 0 and 255 "
        "representing the values of red, green and blue."
    ),
    "component.light.services.turn_on.fields.brightness_pct.name": "Brightness",
    "component.light.services.turn_on.fields.brightness_pct.description": (
        "Number indicating the percentage of full brightness, where 0 turns the "
        "light off, 1 is the minimum brightness, and 100 is the maximum "
        "brightness."
    ),
    "component.light.services.turn_on.fields.rgbw_color.name": "RGBW-color",
    "component.light.services.turn_on.fields.rgbw_color.description": (
        "The color in RGBW format. A list of four integers between 0 and 255 "
        "representing the values of red, green, blue and white."
    ),
    "component.light.services.turn_on.fields.color_name.name": "Color name",
    "component.light.services.turn_on.fields.color_name.description": (
        "A human-readable color name."
    ),
    "component.light.services.turn_on.sections.additional_fields.name": (
        "Additional options"
    ),
    "component.light.services.turn_off.name": "Turn off light",
    "component.light.services.turn_off.description": "Turns off one or more lights.",
    "component.calendar.services.get_events.name": "Get calendar events",
    "component.calendar.services.get_events.description": (
        "Retrieves events on a calendar within a time range."
    ),
    "component.calendar.services.get_events.fields.start_date_time.name": "Start time",
    "component.calendar.services.get_events.fields.start_date_time.description": (
        "Returns active events after this time (exclusive). When not set, "
        "defaults to now."
    ),
    "component.conversation.services.process.name": "Process conversation",
    "component.conversation.services.process.description": (
        "Sends text to a conversation agent for processing."
    ),
    "component.conversation.services.process.fields.text.name": "Text",
    "component.conversation.services.process.fields.text.description": (
        "Transcribed text input."
    ),
    "component.zwave_js.services.invoke_cc_api.name": (
        "Invoke a Command Class API on a node"
    ),
    "component.zwave_js.services.invoke_cc_api.description": (
        "Calls a Command Class API on a node. Some Command Classes can't be "
        "fully controlled via the `set_value` action and require direct calls "
        "to the Command Class API."
    ),
    "component.zwave_js.services.invoke_cc_api.fields.method_name.name": (
        "Method name"
    ),
    "component.zwave_js.services.invoke_cc_api.fields.method_name.description": (
        "The name of the API method to call. Refer to the Z-Wave Command Class "
        "API documentation ({api_docs_url}) for available methods."
    ),
    "component.zwave_js.services.invoke_cc_api.fields.parameters.name": "Parameters",
    "component.zwave_js.services.invoke_cc_api.fields.parameters.description": (
        "A list of parameters to pass to the API method. Refer to the Z-Wave "
        "Command Class API documentation ({api_docs_url}) for parameters."
    ),
    "component.fan.services.increase_speed.name": "Increase fan speed",
    "component.fan.services.increase_speed.description": (
        "Increases the speed of a fan."
    ),
    "component.fan.services.increase_speed.fields.percentage_step.name": "Increment",
    "component.fan.services.increase_speed.fields.percentage_step.description": (
        "Percentage step by which the speed should be increased."
    ),
    "component.fan.services.increase_speed.sections.additional_fields.name": (
        "Additional options"
    ),
    "component.climate.services.set_temperature.name": "Set temperature",
    "component.climate.services.set_temperature.fields.target_temp_high.name": (
        "Upper target temperature"
    ),
    "component.climate.services.set_temperature.fields.target_temp_high.description": (
        "The max temperature setpoint."
    ),
    "component.climate.services.set_temperature.fields.target_temp_low.name": (
        "Lower target temperature"
    ),
    "component.climate.services.set_temperature.fields.target_temp_low.description": (
        "The min temperature setpoint."
    ),
    "component.climate.services.set_temperature.sections.temperature_range.name": (
        "Temperature range"
    ),
    "component.switch.services.turn_on.name": "Turn on switch",
    "component.switch.services.turn_on.description": "Turns on a switch.",
}

# The language the catalog was captured in.
LANGUAGE = "en"

# Services keyed the way ``_process_services`` emits them.
BY_KEY = {
    f"{domain_entry['domain']}.{service_name}": service_def
    for domain_entry in SERVICES
    for service_name, service_def in domain_entry["services"].items()
}
