/** @type {import('tailwindcss').Config} */
import colors from 'tailwindcss/colors';

/*
 * Light-scheme token indirection (#1572).
 *
 * The site was authored dark-only, with hard-coded color utilities
 * (bg-slate-800, text-slate-300, ...) across pages — including @apply-baked
 * custom classes that class-level CSS overrides cannot reach. Instead of
 * re-authoring every call site with dark: variants, the shades listed below
 * are routed through a CSS custom property:
 *
 *   rgb(var(--tw-slate-800, <original channels>) / <alpha-value>)
 *
 * In dark mode (default) the var is UNDEFINED, so the var() fallback — the
 * exact channels from tailwindcss/colors, derived programmatically, never
 * transcribed — renders pixel-identically to stock Tailwind. The light
 * theme (global.css, :root[data-theme="light"]) defines the vars to remap
 * each shade to a light-scheme value. This reaches every utility variant
 * (hover:, /alpha, gradients) and every @apply-baked class in one place.
 *
 * Coverage is enforced by tests/src/unit/test_light_scheme_token_coverage.py:
 * any color utility used in site/src must be themed here, exempted with a
 * reason there, or the test fails.
 */
const channels = (hex) => {
  const h = hex.replace('#', '');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(' ');
};
const themed = (family, ...shades) =>
  Object.fromEntries(
    shades.map((s) => [
      s,
      `rgb(var(--tw-${family}-${s}, ${channels(colors[family][s])}) / <alpha-value>)`,
    ]),
  );

export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,svelte,ts,tsx,vue}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: 'rgb(var(--brand) / <alpha-value>)',
          light: 'rgb(var(--brand-light) / <alpha-value>)',
          dark: 'rgb(var(--brand-dark) / <alpha-value>)',
        },
        surface: {
          0: 'rgb(var(--surface-0) / <alpha-value>)',
          1: 'rgb(var(--surface-1) / <alpha-value>)',
          2: 'rgb(var(--surface-2) / <alpha-value>)',
          3: 'rgb(var(--surface-3) / <alpha-value>)',
        },
        // Foreground shades (100–500 read light-on-dark) and container
        // shades (700–950 are dark fills/borders) get light-mode remaps.
        // slate-600 is intentionally NOT themed: the stock value reads
        // acceptably on both schemes (see LIGHT_SAFE in
        // tests/src/unit/test_light_scheme_token_coverage.py).
        slate: themed('slate', 100, 200, 300, 400, 500, 700, 800, 900, 950),
        blue: themed('blue', 200, 300, 400, 900),
        green: themed('green', 300, 400, 900),
        amber: themed('amber', 200, 300, 400, 900),
        red: themed('red', 200, 300, 400, 900),
        purple: themed('purple', 200, 300, 400, 900),
        violet: themed('violet', 300, 400),
        cyan: themed('cyan', 300, 500, 900),
        emerald: themed('emerald', 500),
        yellow: themed('yellow', 300, 400, 900),
        orange: themed('orange', 300, 900),
      },
      fontFamily: {
        display: ['"Plus Jakarta Sans"', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
