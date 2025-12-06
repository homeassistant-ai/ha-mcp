/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,svelte,ts,tsx,vue}'],
  theme: {
    extend: {
      colors: {
        'ha-blue': '#03a9f4',
        'ha-dark': '#1c1c1c',
        'primary': '#3b82f6',
      },
    },
  },
  plugins: [],
};
