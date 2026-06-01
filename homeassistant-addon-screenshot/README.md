# Screenshot Home Assistant using Puppeteer

Easily create screenshots of your Home Assistant dashboards. Allowing you to put them on e-ink screens or any other screen that can display images.

[![Open your Home Assistant instance and show the dashboard of an add-on.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=0f1cc410_puppet&repository_url=https%3A%2F%2Fgithub.com%2Fballoob%2Fhome-assistant-addons)

![UI Screenshot](example/ui.png)

![UI Screenshot](example/device.jpg)

You will need to create a long lived access token and add it as an add-on option.

Enable the watch dog option to restart the add-on when the browser fails to launch (happens sometimes, still investigating).

_This is a prototype, there is NO security. Anyone can access the server and make screenshots of any Home Assistant page._

[![ESPHome device showing a screenshot of a Home Assistant dashboard](https://raw.githubusercontent.com/balloob/home-assistant-addons/main/puppet/example/screenshot.jpg)](./example/)

## Configuration

- access_token: Long-lived access token used to authenticate against Home Assistant.

## Advanced configuration

- home_assistant_url: Base URL of your Home Assistant instance that the add-on browser should open when taking screenshots. Defaults to `http://homeassistant:8123` which is the internal URL at which the add-on can reach Home Assistant. You can override it if your instance has configured SSL certificates inside Home Assistant and requires to be reached via a different hostname or port (e.g., http://my-ha.local:8123 or https://example.duckdns.org).
- keep_browser_open: If true, keeps the Chromium browser alive between requests.

## Web UI

The add-on now includes a web-based user interface to help you easily configure and preview screenshots. You can access it by:

1. Opening the add-on's Web UI from the Home Assistant Supervisor interface
2. Or navigating directly to `http://homeassistant.local:10000/`

The Web UI provides:
- Interactive form to configure screenshot parameters (path, viewport size, format, theme, etc.)
- Live preview of screenshots
- Automatically generated URL that you can copy and use in your automations or external applications

This is particularly useful for testing different settings and finding the perfect configuration before using the URLs in your automations.

## Usage

Starting the add-on will launch a new server on port 10000. Any path you request will return a screenshot of that page. You will need to specify the viewport size you want.

For example, to get a 1000px x 1000px screenshot of your default dashboard, fetch:

```
http://homeassistant.local:10000/home?viewport=1000x1000
```

### e-ink displays

To reduce the color palette for e-ink displays, you can add the `colors` parameter. The value is a comma-separated list of hex colors to use. For example, for a 2-color e-ink display (black and white):

```
http://homeassistant.local:10000/home?viewport=1000x1000&colors=000000,FFFFFF
```

You can also invert the colors by adding the `invert` parameter:

```
http://homeassistant.local:10000/home?viewport=1000x1000&colors=000000,FFFFFF&invert
```

It's recommended to use an e-ink theme like [Graphite](https://github.com/TilmanGriesel/graphite?tab=readme-ov-file#e-ink-themes) to optimize readability.

### Set Theme

You can set the theme of the Home Assistant interface for the screenshot by adding the `theme` query parameter. The value should be a theme name that Home Assistant supports (e.g., `Graphite E-ink Light`).

```
http://homeassistant.local:10000/home?viewport=1000x1000&theme=Graphite%20E-ink%20Light
```

**Note:** Theme changes apply to all sessions of the user whose token is used. To avoid having your normal browsing sessions affected by screenshot themes (e.g., e-ink themes), create a separate user account specifically for Puppet and use that user's long-lived access token in the addon configuration.

### Finish loading detection

By default, on a cold start the server will wait for 2.5 extra seconds after the loading is considered done, to give things that are not tracked by loading spinners to load (ie icons, pictures). When the browser is active, it waits 750ms. You can control this wait time by adding a `wait` query parameter. For example, to wait 10 seconds:

```
http://homeassistant.local:10000/home?viewport=1000x1000&wait=10000
```

You can control the zoom level of the page using the `zoom` query parameter. The default zoom level is 1. For example, to zoom in 1.3x:

```
http://homeassistant.local:10000/home?viewport=1000x1000&zoom=1.3
```

### Output formats

By default, the output format is PNG. You can request a JPEG, WebP or BMP image by adding the `format=jpeg`, `format=webp`, `format=bmp` query parameter:

```
http://homeassistant.local:10000/home?viewport=1000x1000&format=jpeg
```

```
http://homeassistant.local:10000/home?viewport=1000x1000&format=webp
```

```
http://homeassistant.local:10000/home?viewport=1000x1000&format=bmp
```

### Rotate screenshot

You can rotate the screenshot by adding the `rotate` query parameter. Valid values are 90, 180, and 270.

```
http://homeassistant.local:10000/home?viewport=1000x1000&rotate=90
```

### Set Language

You can set the language of the Home Assistant interface for the screenshot by adding the `lang` query parameter. The value should be a language code that Home Assistant supports (e.g., `en`, `nl`, `de`, `ko`, `ja`, `zh-Hans`, `zh-Hant`).

```
http://homeassistant.local:10000/home?viewport=1000x1000&lang=nl
```

### Set Dark Mode

You can enable dark mode for the screenshot by adding the `dark` query parameter. This parameter doesn't require a value.

```
http://homeassistant.local:10000/home?viewport=1000x1000&dark
```

### Preloading requests

To improve performance for subsequent requests, you can schedule the browser to navigate to the desired page ahead of time using the `next` parameter. Provide the number of seconds when you expect the *next* screenshot request to occur. The add-on will attempt to navigate the browser to the specified path 10 seconds *before* this timestamp.

```
# Example how the browser will warm up so it's ready to take a screenshot
# in 300 seconds.
http://homeassistant.local:10000/home?next=300
```

Providing a `next` parameter will not affect the current request. It will only be used for the next request.

## Using images inside Home Assistant

You can use a template image entity to pull Puppet output into Home Assistant to make it possible to send it in notifications or use for other purposes.

```
template:
  - image:
      name: My dashboard
      url: "http://homeassistant.local:10000/home?viewport=1000x1000"
```

## Proxmox

If you're running Home Assistant OS in a virtual machine under Proxmox, make sure the host type of your virtual machine is set to `host`.

## Speed (or lack thereof)

This add-on is slow. On a Home Assistant Green, on cold-start, it takes ~10s. The browser is kept alive for up to 30 seconds.

If the same page is requested, a screenshot is returned as fast as possible (0.6s on HA Green). If a different page is requested, it takes ~1.5s because it needs to navigate.
