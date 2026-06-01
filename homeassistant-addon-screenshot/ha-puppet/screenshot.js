import puppeteer from "puppeteer";
import sharp from "sharp"; // Import sharp
import { BMPEncoder } from "./bmp.js";
import {
  debug,
  isAddOn,
  chromiumExecutable,
  currentHassToken,
  useSupervisorAuth,
} from "./const.js";
import { CannotOpenPageError } from "./error.js";

const HEADER_HEIGHT = 56;

// Dithering algorithms
function applyDithering(data, width, height, palette, channels = 4, algorithm = "atkinson", paletteColors = null) {
  // Convert hex colors to RGB
  const rgbPalette = palette.map(hex => {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return [r, g, b];
  });

  // If paletteColors is provided, use it for matching (quantization palette)
  const rgbQuantizationPalette = paletteColors ? paletteColors.map(hex => {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return [r, g, b];
  }) : rgbPalette;

  // Function to find the closest color in the quantization palette
  // Returns the index (which maps to both quantization and output palette)
  function findClosestColorIndex(r, g, b) {
    let minDistanceSq = Infinity;
    let closestIndex = 0;

    for (let i = 0; i < rgbQuantizationPalette.length; i++) {
      const color = rgbQuantizationPalette[i];

      const dr = r - color[0];
      const dg = g - color[1];
      const db = b - color[2];

      // Do comparisons using squared distances to avoid extra computational overhead
      // of Math.sqrt and Math.pow
      const distanceSq = dr * dr + dg * dg + db * db;

      if (distanceSq < minDistanceSq) {
        minDistanceSq = distanceSq;
        closestIndex = i;
      }
    }

    return closestIndex;
  }

  // Function to find the closest color (returns the output color)
  function findClosestColor(r, g, b) {
    const index = findClosestColorIndex(r, g, b);
    return rgbPalette[index];
  }

  if (algorithm === "none") {
    // Simple nearest color mapping without dithering
    const result = new Uint8Array(data);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const idx = (y * width + x) * channels;
        const [newR, newG, newB] = findClosestColor(result[idx], result[idx + 1], result[idx + 2]);
        result[idx] = newR;
        result[idx + 1] = newG;
        result[idx + 2] = newB;
      }
    }
    return result;
  }

  // Apply error diffusion dithering
  return applyErrorDiffusionDithering(data, width, height, channels, algorithm, findClosestColor);
}

function applyErrorDiffusionDithering(data, width, height, channels, algorithm, findClosestColor) {
  // Create a copy of the data in float to avoid quantization/truncation noise on each diffusion step
  const work = new Float32Array(data.length);
  for (let i = 0; i < data.length; i++) work[i] = data[i];

  const result = new Uint8Array(data.length);

  const clamp255 = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);
  const idxOf = (x, y) => (y * width + x) * channels;

  // Distribute helper operates on float buffer
  const distribute = (x, y, dx, dy, factor, er, eg, eb) => {
    const nx = x + dx, ny = y + dy;
    if (nx < 0 || nx >= width || ny < 0 || ny >= height) return;
    const ni = idxOf(nx, ny);
    work[ni]     = clamp255(work[ni]     + er * factor);
    work[ni + 1] = clamp255(work[ni + 1] + eg * factor);
    work[ni + 2] = clamp255(work[ni + 2] + eb * factor);
    // leave alpha alone
  };

  // Diffusion kernels (weights sum to 1 for FS; Atkinson intentionally < 1)
  // For serpentine scan we mirror dx.
  const kernels = {
    "floyd-steinberg": [
      [ 1, 0, 7/16],
      [-1, 1, 3/16],
      [ 0, 1, 5/16],
      [ 1, 1, 1/16],
    ],
    "atkinson": [
      [ 1, 0, 1/8],
      [ 2, 0, 1/8],
      [-1, 1, 1/8],
      [ 0, 1, 1/8],
      [ 1, 1, 1/8],
      [ 0, 2, 1/8],
    ],
    "jarvis-judice-ninke": [
      [ 1, 0, 7/48], [ 2, 0, 5/48],
      [-2, 1, 3/48], [-1, 1, 5/48], [0, 1, 7/48], [1, 1, 5/48], [2, 1, 3/48],
      [-2, 2, 1/48], [-1, 2, 3/48], [0, 2, 5/48], [1, 2, 3/48], [2, 2, 1/48],
    ],
    "stucki": [
      [ 1, 0, 8/42], [ 2, 0, 4/42],
      [-2, 1, 2/42], [-1, 1, 4/42], [0, 1, 8/42], [1, 1, 4/42], [2, 1, 2/42],
      [-2, 2, 1/42], [-1, 2, 2/42], [0, 2, 4/42], [1, 2, 2/42], [2, 2, 1/42],
    ],
    "burkes": [
      [ 1, 0, 8/32], [ 2, 0, 4/32],
      [-2, 1, 2/32], [-1, 1, 4/32], [0, 1, 8/32], [1, 1, 4/32], [2, 1, 2/32],
    ],
    "sierra": [
      [ 1, 0, 5/32], [ 2, 0, 3/32],
      [-2, 1, 2/32], [-1, 1, 4/32], [0, 1, 5/32], [1, 1, 4/32], [2, 1, 2/32],
      [-1, 2, 2/32], [0, 2, 3/32], [1, 2, 2/32],
    ],
    "sierra-lite": [
      [ 1, 0, 2/4],
      [-1, 1, 1/4],
      [ 0, 1, 1/4],
    ],
  };

  // select which error-diffusion kernel to use for dithering and ensure a default of floyd-steinberg
  const kernel = kernels[algorithm] || kernels["floyd-steinberg"];

  // For Atkinson dithering reduce error intensity for softer dithering
  const errorGain = (algorithm === "atkinson") ? 0.75 : 1.0;
  const minTotalError = 8;

  for (let y = 0; y < height; y++) {

    // Do a serpentine scan to reduce directional artifacts
    const serpentine = (y & 1) === 1; // alternate direction each row
    const xStart = serpentine ? width - 1 : 0;
    const xEnd = serpentine ? -1 : width;
    const xStep = serpentine ? -1 : 1;

    for (let x = xStart; x !== xEnd; x += xStep) {
      const i = idxOf(x, y);

      // If you ever have transparency, skip fully transparent pixels
      if (channels === 4 && work[i + 3] <= 0) {
        result[i] = result[i + 1] = result[i + 2] = 0;
        result[i + 3] = 0;
        continue;
      }

      const oldR = work[i], oldG = work[i + 1], oldB = work[i + 2];

      const [newR, newG, newB] = findClosestColor(oldR, oldG, oldB);

      // Write quantized result
      result[i]     = newR;
      result[i + 1] = newG;
      result[i + 2] = newB;
      if (channels === 4) result[i + 3] = Math.round(clamp255(work[i + 3]));

      let errorR = (oldR - newR) * errorGain;
      let errorG = (oldG - newG) * errorGain;
      let errorB = (oldB - newB) * errorGain;

      // Only distribute error if there's significant quantization error
      // This prevents artifacts in solid color areas (like white backgrounds and borders)
      const totalError = Math.abs(errorR) + Math.abs(errorG) + Math.abs(errorB);

      // Skip error diffusion for near-perfect matches
      if (totalError < minTotalError) {
        continue;
      }

      // Also skip error diffusion for near-white or near-black pixels
      // This prevents artifacts in solid areas and borders
      const isNearWhite = oldR > 245 && oldG > 245 && oldB > 245;
      const isNearBlack = oldR < 10 && oldG < 10 && oldB < 10;
      const quantizedIsWhite = newR > 245 && newG > 245 && newB > 245;
      const quantizedIsBlack = newR < 10 && newG < 10 && newB < 10;

      if ((isNearWhite && quantizedIsWhite) || (isNearBlack && quantizedIsBlack)) {
        continue;
      }

      // Distribute error to neighboring pixels
      for (const [dx0, dy, w] of kernel) {
        const dx = serpentine ? -dx0 : dx0; // mirror horizontally when scanning right-to-left
        distribute(x, y, dx, dy, w, errorR, errorG, errorB);
      }
    }
  }

  return result;
}

// These are JSON stringified values
const hassLocalStorageDefaults = {
  dockedSidebar: `"always_hidden"`,
  selectedTheme: `{"dark": false}`,
};

// From https://www.bannerbear.com/blog/ways-to-speed-up-puppeteer-screenshots/
const puppeteerArgs = [
  "--autoplay-policy=user-gesture-required",
  "--disable-background-networking",
  "--disable-background-timer-throttling",
  "--disable-backgrounding-occluded-windows",
  "--disable-breakpad",
  "--disable-client-side-phishing-detection",
  "--disable-component-update",
  "--disable-default-apps",
  "--disable-dev-shm-usage",
  "--disable-domain-reliability",
  "--disable-extensions",
  "--disable-features=AudioServiceOutOfProcess",
  "--disable-hang-monitor",
  "--disable-ipc-flooding-protection",
  "--disable-notifications",
  "--disable-offer-store-unmasked-wallet-cards",
  "--disable-popup-blocking",
  "--disable-print-preview",
  "--disable-prompt-on-repost",
  "--disable-renderer-backgrounding",
  "--disable-setuid-sandbox",
  "--disable-speech-api",
  "--disable-sync",
  "--hide-scrollbars",
  "--ignore-gpu-blacklist",
  "--metrics-recording-only",
  "--mute-audio",
  "--no-default-browser-check",
  "--no-first-run",
  "--no-pings",
  "--no-sandbox",
  "--no-zygote",
  "--password-store=basic",
  "--use-gl=swiftshader",
  "--use-mock-keychain",
];
if (isAddOn) {
  puppeteerArgs.push("--enable-low-end-device-mode");
}

export class Browser {
  constructor(homeAssistantUrl, token) {
    this.homeAssistantUrl = homeAssistantUrl;
    this.token = token;
    this.browser = undefined;
    this.page = undefined;
    this.busy = false;

    // The last path we requested a screenshot for
    // We store this instead of using page.url() because panels can redirect
    // users, ie / -> /home.
    this.lastRequestedPath = undefined;
    this.lastRequestedLang = undefined;
    this.lastRequestedTheme = undefined;
    this.lastRequestedDarkMode = undefined;
  }

  async cleanup() {
    const { browser, page } = this;

    if (!this.browser && !this.page) {
      return;
    }

    this.page = undefined;
    this.browser = undefined;
    this.lastRequestedPath = undefined;
    this.lastRequestedLang = undefined;
    this.lastRequestedTheme = undefined;
    this.lastRequestedDarkMode = undefined;

    try {
      if (page) {
        await page.close();
      }
    } catch (err) {
      console.error("Error closing page during cleanup:", err);
    }

    try {
      if (browser) {
        await browser.close();
      }
    } catch (err) {
      console.error("Error closing browser during cleanup:", err);
    }

    console.log("Closed browser");
  }

  async getPage() {
    if (this.page) {
      return this.page;
    }

    console.log("Starting browser");
    // We don't catch these errors on purpose, as we're
    // not able to recover once the app fails to start.
    const browser = await puppeteer.launch({
      headless: "shell",
      executablePath: chromiumExecutable,
      args: puppeteerArgs,
    });
    const page = await browser.newPage();

    // Route all log messages from browser to our add-on log
    // https://pptr.dev/api/puppeteer.pageevents
    page
      .on("framenavigated", (frame) =>
        // Why are we seeing so many frame navigated ??
        console.log("Frame navigated", frame.url()),
      )
      .on("console", (message) =>
        console.log(
          `CONSOLE ${message
            .type()
            .substr(0, 3)
            .toUpperCase()} ${message.text()}`,
        ),
      )
      .on("error", (err) => console.error("ERROR", err))
      .on("pageerror", ({ message }) => console.log("PAGE ERROR", message))
      .on("requestfailed", (request) =>
        console.log(
          `REQUEST-FAILED ${request.failure().errorText} ${request.url()}`,
        ),
      );
    if (debug)
      page.on("response", (response) =>
        console.log(
          `RESPONSE ${response.status()} ${response.url()} (cache: ${response.fromCache()})`,
        ),
      );

    this.browser = browser;
    this.page = page;
    return this.page;
  }

  async navigatePage({
    pagePath,
    viewport,
    extraWait,
    zoom,
    lang,
    theme,
    dark,
  }) {
    let start = new Date();
    if (this.busy) {
      throw new Error("Browser is busy");
    }
    start = new Date();
    this.busy = true;
    const headerHeight = Math.round(HEADER_HEIGHT * zoom);

    try {
      const page = await this.getPage();

      // We add 56px to the height to account for the header
      // We'll cut that off from the screenshot
      viewport.height += headerHeight;

      const curViewport = page.viewport();

      if (
        !curViewport ||
        curViewport.width !== viewport.width ||
        curViewport.height !== viewport.height
      ) {
        await page.setViewport(viewport);
      }

      let defaultWait = isAddOn ? 750 : 500;
      let openedNewPage = false;

      // In Supervisor auto-auth mode the Supervisor token rotates, so refresh
      // from the live environment before each render. Otherwise a long-lived
      // keep_browser_open session would keep injecting the stale snapshot
      // captured at import and silently render the login page after a rotation.
      if (useSupervisorAuth) {
        const fresh = currentHassToken();
        if (fresh && fresh !== this.token) {
          this.token = fresh;
          // Force a full re-navigation so the refreshed token is re-injected
          // into localStorage (the SPA-nav branch below does not re-seed).
          this.lastRequestedPath = undefined;
        }
      }

      // If we're still on about:blank, navigate to HA UI
      if (this.lastRequestedPath === undefined) {
        openedNewPage = true;

        // Ensure we have tokens when we open the UI
        const clientId = new URL("/", this.homeAssistantUrl).toString(); // http://homeassistant.local:8123/
        const hassUrl = clientId.substring(0, clientId.length - 1); // http://homeassistant.local:8123
        const browserLocalStorage = {
          ...hassLocalStorageDefaults,
          hassTokens: JSON.stringify({
            access_token: this.token,
            token_type: "Bearer",
            expires_in: 1800,
            hassUrl,
            clientId,
            expires: 9999999999999,
            refresh_token: "",
          }),
        };
        const evaluateIdentifier = await page.evaluateOnNewDocument(
          (hassLocalStorage) => {
            for (const [key, value] of Object.entries(hassLocalStorage)) {
              localStorage.setItem(key, value);
            }
          },
          browserLocalStorage,
        );

        // Open the HA UI
        const pageUrl = new URL(pagePath, this.homeAssistantUrl).toString();
        const response = await page.goto(pageUrl);
        if (!response.ok()) {
          throw new CannotOpenPageError(response.status(), pageUrl);
        }
        page.removeScriptToEvaluateOnNewDocument(evaluateIdentifier.identifier);

        // Launching browser is slow inside the add-on, give it extra time
        if (isAddOn) {
          defaultWait += 2000;
        }
      } else if (this.lastRequestedPath !== pagePath) {
        // mimick HA frontend navigation (no full reload)
        await page.evaluate((pagePath) => {
          history.replaceState(
            history.state?.root ? { root: true } : null,
            "",
            pagePath,
          );
          const event = new Event("location-changed");
          event.detail = { replace: true };
          window.dispatchEvent(event);
        }, pagePath);
      } else {
        // We are already on the correct page
        defaultWait = 0;
      }

      this.lastRequestedPath = pagePath;

      // Dismiss any dashboard update avaiable toasts
      if (
        !openedNewPage &&
        (await page.evaluate((zoomLevel) => {
          // Set zoom level
          document.body.style.zoom = zoomLevel;

          const haEl = document.querySelector("home-assistant");
          if (!haEl) return false;
          const notifyEl = haEl.shadowRoot?.querySelector(
            "notification-manager",
          );
          if (!notifyEl) return false;
          const actionEl = notifyEl.shadowRoot.querySelector(
            "ha-toast *[slot=action]",
          );
          if (!actionEl) return false;
          actionEl.click();
          return true;
        }, zoom))
      ) {
        // If we dismissed a toast, let's wait a bit longer
        defaultWait += 1000;
      } else {
        // Set zoom level
        await page.evaluate((zoomLevel) => {
          document.body.style.zoom = zoomLevel;
        }, zoom);
      }

      // Wait for the page to be loaded.
      try {
        await page.waitForFunction(
          () => {
            const haEl = document.querySelector("home-assistant");
            if (!haEl) return false;
            const mainEl = haEl.shadowRoot?.querySelector(
              "home-assistant-main",
            );
            if (!mainEl) return false;
            const panelResolver = mainEl.shadowRoot?.querySelector(
              "partial-panel-resolver",
            );
            if (!panelResolver || panelResolver._loading) {
              return false;
            }

            const panel = panelResolver.children[0];
            if (!panel) return false;

            return !("_loading" in panel) || !panel._loading;
          },
          {
            timeout: 10000,
            polling: 100,
          },
        );
      } catch (err) {
        console.log("Timeout waiting for HA to finish loading");
      }

      // Fail loudly if auth landed us on the login screen instead of an
      // authenticated dashboard. Returning a 200 login-page PNG would be a
      // confusing silent failure (most likely a stale/invalid token). Throwing
      // resets lastRequestedPath in screenshotPage's catch, forcing a fresh
      // navigation + token re-seed on the next request.
      const onLoginScreen = await page.evaluate(() => {
        const haEl = document.querySelector("home-assistant");
        const authEl = haEl?.shadowRoot?.querySelector("ha-authorize");
        return !!authEl;
      });
      if (onLoginScreen) {
        throw new CannotOpenPageError(401, pagePath);
      }

      // Update language
      // Should really be done via localStorage.selectedLanguage
      // but that doesn't seem to work
      if (lang !== this.lastRequestedLang) {
        await page.evaluate((newLang) => {
          document
            .querySelector("home-assistant")
            ._selectLanguage(newLang, false);
        }, lang || "en");
        this.lastRequestedLang = lang;
        defaultWait += 1000;
      }

      // Update theme and dark mode
      if (
        theme !== this.lastRequestedTheme ||
        dark !== this.lastRequestedDarkMode
      ) {
        await page.evaluate(
          ({ theme, dark }) => {
            document.querySelector("home-assistant").dispatchEvent(
              new CustomEvent("settheme", {
                detail: { theme, dark },
              }),
            );
          },
          { theme: theme || "", dark },
        );
        this.lastRequestedTheme = theme;
        this.lastRequestedDarkMode = dark;
        defaultWait += 500;
      }

      // wait for the work to be done.
      // Not sure yet how to decide that?
      if (extraWait === undefined) {
        extraWait = defaultWait;
      }
      if (extraWait) {
        await new Promise((resolve) => setTimeout(resolve, extraWait));
      }

      const end = Date.now();
      return { time: end - start };
    } finally {
      this.busy = false;
    }
  }

  async screenshotPage({ viewport, colors, paletteColors, dithering, invert, zoom, format, rotate, bmpMode = "color" }) {
    let start = new Date();
    if (this.busy) {
      throw new Error("Browser is busy");
    }
    start = new Date();
    this.busy = true;
    const headerHeight = Math.round(HEADER_HEIGHT * zoom);

    try {
      const page = await this.getPage();

      let image = await page.screenshot({
        type: "png",
        clip: {
          x: 0,
          y: headerHeight,
          width: viewport.width,
          height: viewport.height - headerHeight,
        },
      });

      let sharpInstance = sharp(image);

      if (rotate) {
        sharpInstance = sharpInstance.rotate(rotate);
      }

      // Apply custom color dithering if colors parameter is provided
      if (colors && colors.length > 0) {
        // Convert to raw pixel data for custom dithering
        sharpInstance = sharpInstance.ensureAlpha().raw();
        const { data, info } = await sharpInstance.toBuffer({
          resolveWithObject: true,
        });

        // Apply dithering with the specified colors and algorithm
        const ditheredData = applyDithering(data, info.width, info.height, colors, info.channels, dithering, paletteColors);

        // Create new sharp instance from dithered data
        sharpInstance = sharp(ditheredData, {
          raw: {
            width: info.width,
            height: info.height,
            channels: info.channels,
          },
        });
      }

      // Apply invert if requested (after color processing)
      if (invert) {
        sharpInstance = sharpInstance.negate({
          alpha: false,
        });
      }

      // Output in the requested format
      if (format === "jpeg") {
        sharpInstance = sharpInstance.jpeg();
        image = await sharpInstance.toBuffer();
      } else if (format === "webp") {
        sharpInstance = sharpInstance.webp();
        image = await sharpInstance.toBuffer();
      } else if (format === "bmp") {
        // Support multiple BMP modes: color (24-bit), grayscale (8-bit), binary (1-bit)
        if (bmpMode === "grayscale") {
          // Generate 8-bit grayscale
          sharpInstance = sharpInstance.greyscale().removeAlpha().raw();
          const { data, info } = await sharpInstance.toBuffer({
            resolveWithObject: true,
          });
          const bmpEncoder = new BMPEncoder(info.width, info.height, 8);
          image = bmpEncoder.encode(data);
        } else if (bmpMode === "binary") {
          // Generate 1-bit black/white using threshold
          sharpInstance = sharpInstance.greyscale().threshold().raw();
          const { data, info } = await sharpInstance.toBuffer({
            resolveWithObject: true,
          });
          const bmpEncoder = new BMPEncoder(info.width, info.height, 1);
          image = bmpEncoder.encode(data);
        } else {
          // Default: 24-bit color BMP
          sharpInstance = sharpInstance.toColorspace('srgb').removeAlpha().raw();
          const { data, info } = await sharpInstance.toBuffer({
            resolveWithObject: true,
          });
          const bmpEncoder = new BMPEncoder(info.width, info.height, 24);
          image = bmpEncoder.encode(data);
        }
      } else {
        sharpInstance = sharpInstance.png();
        image = await sharpInstance.toBuffer();
      }

      const end = Date.now();
      return {
        image,
        time: end - start,
      };
    } catch (err) {
      // trigger a full page navigation on next request
      this.lastRequestedPath = undefined;
      throw err;
    } finally {
      this.busy = false;
    }
  }
}
