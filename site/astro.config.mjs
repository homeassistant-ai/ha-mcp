// @ts-check
import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

// https://astro.build/config
export default defineConfig({
  site: 'https://homeassistant-ai.github.io',
  base: '/ha-mcp',
  integrations: [tailwind()],
  output: 'static',
});
