import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createConnection, createLongLivedTokenAuth } from "home-assistant-js-websocket";
import { hassUrl, hassToken, isAddOn } from "./const.js";
import { loadDevicesConfig } from "./devices.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Fetch Home Assistant data via WebSocket and REST API
 * @returns {Promise<Object>} The Home Assistant data
 */
async function fetchHomeAssistantData() {
  try {
    const auth = createLongLivedTokenAuth(hassUrl, hassToken);
    const connection = await createConnection({ auth });

    // Fetch themes and network URLs via WebSocket
    const [themesResult, networkResult] = await Promise.all([
      connection.sendMessagePromise({
        type: "frontend/get_themes",
      }),
      connection.sendMessagePromise({
        type: "network/url",
      }),
    ]);

    connection.close();

    // Fetch config via REST API to get language
    const configResponse = await fetch(`${hassUrl}/api/config`, {
      headers: {
        Authorization: `Bearer ${hassToken}`,
        "Content-Type": "application/json",
      },
    });

    const config = configResponse.ok ? await configResponse.json() : null;

    return {
      themes: themesResult,
      network: networkResult,
      config: config,
    };
  } catch (err) {
    console.error("Error fetching Home Assistant data:", err);
    return {
      themes: null,
      network: null,
      config: null,
    };
  }
}

/**
 * Handle UI page request
 * @param {http.ServerResponse} response - The HTTP response object
 */
export async function handleUIRequest(response) {
  try {
    // If no token is configured, show instruction page
    if (!hassToken) {
      const htmlPath = join(__dirname, "html", "error_missing_config.html");
      let html = await readFile(htmlPath, "utf-8");

      // Replace placeholders
      const configFile = isAddOn ? "/data/options.json" : "options-dev.json";
      const configInstructions = isAddOn ? `
              <li>
                <strong>Configure the Add-on:</strong>
                <ul class="ml-6 mt-2 space-y-1 list-disc list-inside text-sm">
                  <li>Go to Settings → Add-ons</li>
                  <li>Click on the Puppet add-on</li>
                  <li>Go to the Configuration tab</li>
                  <li>Paste your token in the "access_token" field</li>
                  <li>Save and restart the add-on</li>
                </ul>
              </li>
              ` : `
              <li>
                <strong>Add to Configuration File:</strong>
                <ul class="ml-6 mt-2 space-y-1 list-disc list-inside text-sm">
                  <li>Open the file: <code class="bg-gray-100 px-2 py-1 rounded">${configFile}</code></li>
                  <li>Add or update the <code class="bg-gray-100 px-2 py-1 rounded">access_token</code> field with your token</li>
                  <li>Save the file and restart the service</li>
                </ul>
              </li>
              `;

      html = html.replace("{{CONFIG_INSTRUCTIONS}}", configInstructions);
      html = html.replace("{{HASS_URL}}", hassUrl);

      response.writeHead(200, {
        "Content-Type": "text/html",
        "Content-Length": Buffer.byteLength(html),
      });
      response.end(html);
      return;
    }

    // Normal UI flow with token
    // Fetch Home Assistant data and load device configurations
    const hassData = await fetchHomeAssistantData();
    const devicesData = loadDevicesConfig();

    // Check if we failed to connect to Home Assistant
    if (!hassData.themes || !hassData.network || !hassData.config) {
      const htmlPath = join(__dirname, "html", "error_connection_failed.html");
      let html = await readFile(htmlPath, "utf-8");

      // Replace placeholders
      const configFile = isAddOn ? "/data/options.json" : "options-dev.json";
      const configInstructions = isAddOn ? `
              <li>
                <strong>Update the Add-on Configuration:</strong>
                <ul class="ml-6 mt-2 space-y-1 list-disc list-inside text-sm">
                  <li>Go to Settings → Add-ons</li>
                  <li>Click on the Puppet add-on</li>
                  <li>Go to the Configuration tab</li>
                  <li>Update the "access_token" field with the new token</li>
                  <li>Save and restart the add-on</li>
                </ul>
              </li>
              ` : `
              <li>
                <strong>Update Configuration File:</strong>
                <ul class="ml-6 mt-2 space-y-1 list-disc list-inside text-sm">
                  <li>Open the file: <code class="bg-gray-100 px-2 py-1 rounded">${configFile}</code></li>
                  <li>Update the <code class="bg-gray-100 px-2 py-1 rounded">access_token</code> field with the new token</li>
                  <li>Save the file and restart the service</li>
                </ul>
              </li>
              `;

      html = html.replace("{{CONFIG_INSTRUCTIONS}}", configInstructions);
      html = html.replace(/{{HASS_URL}}/g, hassUrl);
      html = html.replace("{{TOKEN_LENGTH}}", hassToken?.length || 0);

      response.writeHead(200, {
        "Content-Type": "text/html",
        "Content-Length": Buffer.byteLength(html),
      });
      response.end(html);
      return;
    }

    // Successfully fetched data, serve normal UI
    const htmlPath = join(__dirname, "html", "index.html");
    let html = await readFile(htmlPath, "utf-8");

    // Inject window.hass and window.devices data into the HTML (pretty formatted)
    const hassScriptTag = `<script>window.hass = ${JSON.stringify(hassData, null, 2)};</script>`;
    const devicesScriptTag = `<script>window.devices = ${JSON.stringify(devicesData, null, 2)};</script>`;
    html = html.replace("</head>", `${hassScriptTag}\n  ${devicesScriptTag}\n  </head>`);

    response.writeHead(200, {
      "Content-Type": "text/html",
      "Content-Length": Buffer.byteLength(html),
    });
    response.end(html);
  } catch (err) {
    console.error("Error serving UI:", err);
    response.statusCode = 500;
    response.end("Error loading UI");
  }
}
