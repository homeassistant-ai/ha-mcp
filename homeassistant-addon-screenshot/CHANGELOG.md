# Changelog

## 2.4.3

- Skip downloading Chrome during build since system Chromium is used, reducing addon backup size by over 2GB

## 2.4.2

- Fix colors when using deprecated eink parameter without paletteColors

## 2.4.1

- Fix empty `paletteColors` by falling back to use the colors specified in the `colors` parameter

## 2.4.0

- Add device configuration support with `device` URL parameter and web UI selector
- Devices stored in editable `devices.json` with alias support
- Initial device: Spectra E6 7.3" display (alias: `seeed-reterminal-e1002`)

## 2.3.0

- Add color dithering support with `colors` URL parameter for custom color palettes
- Add `dithering` parameter with support for Floyd-Steinberg, Atkinson and more algorithms
- Add `paletteColors` parameter to use predefined color palettes
- Replaces the previous `eink` parameter approach for limiting colors on e-ink displays

## 2.2.0

- Add URL parameter syncing: all form settings are now represented in the browser URL for easy sharing
- Add screenshot URL import feature with modal dialog
- Auto-preview when changing theme, dark mode, color inversion, format, or rotation
- Simplify footer design with centered attribution link
- Change default path from `/` to `/lovelace`

## 2.1.0

- Fetch Home Assistant data (themes, network URLs, language) and inject into UI
- Auto-populate theme picker dropdown with available themes from Home Assistant
- Use Home Assistant internal URL with port 10000 for screenshot generation
- Auto-prefill language field from Home Assistant configuration
- Add error page for missing access token configuration
- Add error page for connection failures (invalid token or unreachable instance)
- Reorganize HTML files into dedicated html/ folder
- Add link to Home Assistant Community themes

## 2.0.0

- Add user interface to generate screenshot URLs with custom parameters.
