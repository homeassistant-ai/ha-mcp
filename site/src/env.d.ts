/// <reference types="astro/client" />

// Exposed by the copy-button script in src/layouts/Layout.astro so pages with
// dynamically injected content can re-run it after rendering.
interface Window {
  initCopyButtons?: () => void;
}
