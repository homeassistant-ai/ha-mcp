import devicesConfig from "./devices.json" with { type: "json" };

/**
 * Get device configurations
 * @returns {Object} The devices configuration
 */
export function loadDevicesConfig() {
  return devicesConfig;
}

/**
 * Resolve device name (handle aliases)
 * @param {string} deviceName - The device name or alias
 * @param {Object} config - The devices configuration
 * @returns {string} The resolved device name
 */
export function resolveDeviceName(deviceName, config) {
  if (config.aliases && config.aliases[deviceName]) {
    return config.aliases[deviceName];
  }
  return deviceName;
}

/**
 * Get device configuration
 * @param {string} deviceName - The device name or alias
 * @param {Object} config - The devices configuration
 * @returns {Object|null} The device configuration or null if not found
 */
export function getDeviceConfig(deviceName, config) {
  const resolvedName = resolveDeviceName(deviceName, config);
  return config.devices[resolvedName] || null;
}
