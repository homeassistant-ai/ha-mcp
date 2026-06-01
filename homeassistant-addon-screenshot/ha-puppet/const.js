import { readFileSync, existsSync } from "fs";

import { resolveAuth, shouldUseSupervisorAuth } from "./auth.js";

// load first file that exists
const optionsFile = ["./options-dev.json", "/data/options.json"].find(
  existsSync,
);
if (!optionsFile) {
  console.error(
    "No options file found. Please copy options-dev.json.sample to options-dev.json",
  );
  process.exit(1);
}
export const isAddOn = optionsFile === "/data/options.json";
const options = JSON.parse(readFileSync(optionsFile));

// Auto-auth for whoever installs the add-on (ha-mcp modification): in
// add-on mode, if no access_token option is set, fall back to the add-on's
// Supervisor token (config.yaml declares homeassistant_api: true) — so
// screenshots work out-of-the-box with no long-lived token to paste. A
// configured access_token (+ optional home_assistant_url) overrides this,
// e.g. to target an external HA URL.
//
// IMPORTANT: only the *token* changes for auto-auth — NOT the origin. We
// keep ha-puppet's default internal origin (http://homeassistant:8123),
// because screenshot.js derives the injected hassTokens.hassUrl/clientId via
// `new URL("/", hassUrl)`, which strips any path. A path-bearing origin like
// http://supervisor/core would collapse to http://supervisor and break the
// frontend's required origin match. http://homeassistant:8123 is a clean
// origin, is the hostname ha-puppet was designed around, and is reachable on
// the Supervisor network.
// Auth selection lives in auth.js (pure, unit-tested in const.test.mjs); here
// we apply it to the real options file + environment.
const supervisorToken = process.env.SUPERVISOR_TOKEN;
export const useSupervisorAuth = shouldUseSupervisorAuth({
  isAddOn,
  accessToken: options.access_token,
  supervisorToken,
});

export const hassUrl = isAddOn
  ? (options.home_assistant_url || "http://homeassistant:8123")
  : (options.home_assistant_url || "http://localhost:8123");
export const hassToken = resolveAuth({
  isAddOn,
  accessToken: options.access_token,
  supervisorToken,
});

/**
 * Return the token to inject for THIS render. In Supervisor auto-auth mode the
 * Supervisor token rotates, so re-read it from the environment each time
 * rather than reusing the snapshot captured at import (which would go stale on
 * a long-lived keep_browser_open session and silently render the login page).
 * With a configured access_token, that token is authoritative and returned
 * as-is.
 */
export function currentHassToken() {
  if (useSupervisorAuth) {
    return process.env.SUPERVISOR_TOKEN || hassToken;
  }
  return hassToken;
}
export const debug = false;

export const chromiumExecutable = isAddOn ? "/usr/bin/chromium" : (options.chromium_executable || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");

export const keepBrowserOpen = options.keep_browser_open || false;

if (useSupervisorAuth) {
  console.warn(
    "No access_token configured — using the add-on's Supervisor token. The " +
      "screenshot engine now has FULL Home Assistant API access. To limit " +
      "this, set a long-lived access_token from a dedicated low-privilege user.",
  );
}

if (!hassToken) {
  console.warn("No access token configured and no Supervisor token available. UI will show configuration instructions.");
}
