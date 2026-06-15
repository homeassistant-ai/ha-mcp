// ESLint flat config for the docs site. Anchors the accessibility work from
// #1574 (conventions in .gemini/styleguide.md > Accessibility): eslint-plugin-astro
// parses .astro files and eslint-plugin-jsx-a11y flags missing alt text,
// unlabeled controls, and invalid ARIA in the templates. Kept focused on
// accessibility — general JS/TS style is out of scope here. (#1595)
import eslintPluginAstro from 'eslint-plugin-astro';
import tsParser from '@typescript-eslint/parser';

export default [
  { ignores: ['dist/', '.astro/', 'node_modules/'] },
  ...eslintPluginAstro.configs.recommended,
  ...eslintPluginAstro.configs['jsx-a11y-recommended'],
  {
    // Astro frontmatter and inline <script> blocks are TypeScript; point the
    // astro parser at the TS parser so they don't trip on type syntax.
    files: ['**/*.astro'],
    languageOptions: {
      parserOptions: { parser: tsParser },
    },
  },
];
