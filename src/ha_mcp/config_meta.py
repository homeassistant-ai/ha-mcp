"""Metadata that ``Settings`` fields carry about where each setting appears.

Each field declares its facts once, as ``Annotated[<type>, Setting(...)]``.
``config_registry`` derives the web UI and override tables from it, and
``scripts/generate_app_options.py`` derives the app (add-on) options.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic.fields import FieldInfo

__all__ = [
    "BETA_MASTER",
    "AdvancedSection",
    "AppOption",
    "Setting",
    "SettingSurface",
    "setting_of",
]

# Closed set of UI section names. The advanced renderer in
# settings_ui/__init__.py picks a DOM container per section; a typo would render
# the row into nothing.
AdvancedSection = Literal[
    "connection",
    "search",
    "operations",
    "diagnostics",
    "tools_surface",
    "sidecar",
    "beta_codemode",
    "beta_yamlkeys",
    "developer",
]

# The feature flag that gates every ``beta`` flag.
BETA_MASTER = "enable_beta_features"

# Which web UI surface edits the setting: the feature toggles, the Advanced
# section, or the auto-backup settings. Each has its own override file.
SettingSurface = Literal["feature", "advanced", "backup"]


@dataclass(frozen=True)
class AppOption:
    """The setting is an option on the app's Configuration page.

    ``start.py`` exports the option to the setting's env var. An option that
    only some flavors declare is exported only when ``options.json`` has the
    key: an exported value marks the setting as app-managed in the web UI,
    and Supervisor rejects a save of a key the flavor does not declare.
    """

    flavors: tuple[Literal["stable", "dev"], ...] = ("stable", "dev")
    # The dev flavor's default when it differs from the field default.
    dev_default: bool | None = None
    # Supervisor requires the option; every other option is optional ("?").
    required: bool = False


@dataclass(frozen=True)
class Setting:
    """What a ``Settings`` field means outside the model itself."""

    surface: SettingSurface | None = None
    # Advanced settings only: the UI section, and whether the UI may edit it.
    section: AdvancedSection | None = None
    editable: bool = True
    # Gated by the ``enable_beta_features`` master toggle.
    beta: bool = False
    app: AppOption | None = None
    restart_required: bool = False
    # Inclusive bounds. ``off_value`` is a value outside them that means off.
    range: tuple[float, float] | None = None
    off_value: int | None = None
    choices: tuple[str, ...] | None = None
    # An env value that does not parse or is out of range logs a warning and
    # gives the default, instead of failing the settings validation.
    lenient: bool = False


def setting_of(field: FieldInfo) -> Setting | None:
    """Return the ``Setting`` a model field is annotated with, if any."""
    return next((m for m in field.metadata if isinstance(m, Setting)), None)
