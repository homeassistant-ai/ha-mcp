import { readFileSync, existsSync } from "fs";

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

// Authentication: a user-pasted long-lived access token (LLAT) set as the
// add-on's `access_token` option, matching upstream ha-puppet. screenshot.js
// seeds it into the browser's localStorage `hassTokens` before navigation so
// the HA frontend boots already-authenticated. There is no token-less path:
// the HA frontend rejects the add-on's Supervisor token (it is not a frontend
// session and redirects to /auth/authorize), and a long-lived token cannot be
// minted programmatically — so the user must create + paste one. With no
// token, ui.js serves an instruction page instead of rendering.
export const hassUrl = isAddOn
  ? (options.home_assistant_url || "http://homeassistant:8123")
  : (options.home_assistant_url || "http://localhost:8123");
export const hassToken = options.access_token;
export const debug = false;

export const chromiumExecutable = isAddOn ? "/usr/bin/chromium" : (options.chromium_executable || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");

export const keepBrowserOpen = options.keep_browser_open || false;

if (!hassToken) {
  console.warn("No access token configured. Create a long-lived access token in Home Assistant (Profile > Security) and set it as the add-on's access_token option. The UI will show configuration instructions until then.");
}
