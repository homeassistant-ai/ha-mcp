#!/usr/bin/env node

import devices from './devices.json' with { type: 'json' };

const errors = [];

// Check that colors and palette_colors have matching counts
for (const [deviceId, device] of Object.entries(devices.devices)) {
  if (device.colors && device.palette_colors) {
    const colorCount = device.colors.split(',').length;
    const paletteCount = device.palette_colors.split(',').length;

    if (colorCount !== paletteCount) {
      errors.push(
        `Device '${deviceId}': colors (${colorCount}) and palette_colors (${paletteCount}) must have the same count`
      );
    }
  }
}

// Check that aliases point to valid devices
if (devices.aliases) {
  for (const [alias, targetDevice] of Object.entries(devices.aliases)) {
    if (!devices.devices[targetDevice]) {
      errors.push(
        `Alias '${alias}' points to non-existent device '${targetDevice}'`
      );
    }
  }
}

if (errors.length > 0) {
  console.error('❌ Validation errors found:\n');
  errors.forEach((err) => console.error(`  - ${err}`));
  process.exit(1);
}

console.log('✅ All validation checks passed');
console.log('');
console.log(`📱 Device count: ${Object.keys(devices.devices).length}`);
console.log(`🔗 Alias count: ${Object.keys(devices.aliases || {}).length}`);
