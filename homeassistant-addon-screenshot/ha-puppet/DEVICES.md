# Device Configuration Guide

This guide explains how to add new device configurations to `devices.json`.

## File Structure

The `devices.json` file contains two main sections:

1. **devices** - Device definitions with display settings
2. **aliases** - Alternative names that map to device IDs

## Schema

Device configurations are validated against `devices.schema.json`. The schema enforces:

- Device IDs must use lowercase letters, numbers, hyphens, and dots only
- Required fields: `width` and `height`
- Optional fields: `name`, `colors`, `palette_colors`, `dithering`
- Viewport dimensions must be between 100-4000 pixels
- Colors must be valid hex values (without #)
- If both `colors` and `palette_colors` are provided, they must have the same count
- Dithering must be one of the supported algorithms

## Adding a New Device

### 1. Add Device Definition

Add a new entry to the `devices` object:

```json
{
  "devices": {
    "your-device-id": {
      "name": "Display Name",
      "width": 1024,
      "height": 768,
      "colors": "000000,FFFFFF,FF0000",
      "palette_colors": "000000,FFFFFF,FF0000",
      "dithering": "floyd-steinberg"
    }
  }
}
```

### 2. Field Descriptions

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `name` | No | Human-readable device name | `"Spectra E6 7.3\""` |
| `width` | **Yes** | Viewport width in pixels (100-4000) | `800` |
| `height` | **Yes** | Viewport height in pixels (100-4000) | `480` |
| `colors` | No | Output colors (comma-separated hex, no #) | `"000000,FFFFFF,FF0000"` |
| `palette_colors` | No | Quantization colors (must match color count) | `"000000,FFFFFF,FF0000"` |
| `dithering` | No | Dithering algorithm | `"stucki"` |

### 3. Device ID Guidelines

- Use lowercase letters, numbers, hyphens, and dots only
- Include manufacturer or model information
- Be specific (e.g., `spectra-e6-7.3in` not just `spectra`)

### 4. Color Configuration

The color system works in two stages:

1. **palette_colors** - Pixels are matched to these colors during quantization
2. **colors** - Matched pixels are then mapped to these output colors

Both must:
- Be valid 6-digit hex values without the `#` prefix
- Have the same number of colors if both are specified
- Be comma-separated with no spaces

Example for a 3-color e-ink display:
```json
{
  "colors": "000000,FFFFFF,FF0000",
  "palette_colors": "000000,E8E8E8,B21318"
}
```

### 5. Dithering Algorithms

Supported algorithms (from `dithering` field):

- `none` - No dithering
- `floyd-steinberg` - Classic algorithm, good balance
- `atkinson` - Softer, works well for e-ink
- `jarvis-judice-ninke` - High quality, more processing
- `stucki` - Detailed, good for complex images
- `burkes` - Fast algorithm
- `sierra` - Smooth gradients
- `sierra-lite` - Simpler version of Sierra

### 6. Adding Aliases

If a device has multiple names or model numbers, add aliases:

```json
{
  "aliases": {
    "alternative-name": "your-device-id",
    "model-xyz": "your-device-id"
  }
}
```

The alias must point to a valid device ID in the `devices` object.

## Validation

The GitHub workflow automatically validates:

1. **Schema compliance** - File structure matches `devices.schema.json`
2. **Color count matching** - `colors` and `palette_colors` have same count
3. **Valid aliases** - All aliases point to existing devices

To validate locally before committing:

```bash
# Install ajv-cli
npm install -g ajv-cli

# Validate against schema
ajv validate -s devices.schema.json -d devices.json --strict=false

# Run custom validation (color counts, aliases, etc.)
node validate-devices.js
```

## Example: Complete Device Entry

```json
{
  "devices": {
    "waveshare-7.5in-v2": {
      "name": "Waveshare 7.5\" V2",
      "width": 800,
      "height": 480,
      "colors": "000000,FFFFFF",
      "palette_colors": "000000,FFFFFF",
      "dithering": "floyd-steinberg"
    }
  },
  "aliases": {
    "waveshare-7.5-v2": "waveshare-7.5in-v2",
    "ws-7.5-v2": "waveshare-7.5in-v2"
  }
}
```

## Testing Your Changes

After adding a device:

1. Ensure the file validates against the schema
2. Test in the web UI - the device should appear in the dropdown
3. Test the URL parameter: `?device=your-device-id`
4. Verify the viewport and colors are applied correctly
5. Test any aliases you added

## Questions?

If you're unsure about any settings, check existing device configurations or refer to your display's documentation for optimal color palettes and resolution.
