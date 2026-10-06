# Custom card DOM fixture

`linkedom-0.18.13.js` is the output of `custom_cards.dom_script()` on the
published linkedom 0.18.13 npm tarball. That function verifies the pinned
SHA-512 integrity before converting `package/worker.js` to a scoped script.
The upstream ISC license is in `linkedom-LICENSE`.

Seeding this cache makes custom-card E2E tests independent of npm availability.
Regenerate it with `dom_script()` when the pinned version changes.
