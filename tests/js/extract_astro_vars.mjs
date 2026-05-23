// Read an .astro file, strip imports / `import.meta` refs out of its
// frontmatter, evaluate the remaining declarations as TypeScript via
// esbuild, and dump the named identifiers as JSON.
//
// Used by the JS behaviour tests that need to drive Astro pages: those
// pages declare wizard data (clientsData, platformsData, …) in the
// frontmatter and reference them through `<script define:vars={...}>`.
// We rebuild the same injection here so the in-page script sees the
// real production data when run inside JSDOM.
//
// Usage (from Python):
//   echo '{"path": "...", "names": ["clientsData", "platformsData"]}' \
//     | node tests/js/extract_astro_vars.mjs
// Outputs: {"clientsData": [...], "platformsData": [...]}

import { readFileSync } from "node:fs";
import { transformSync } from "esbuild";

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = "";
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => {
      buf += chunk;
    });
    process.stdin.on("end", () => resolve(buf));
    process.stdin.on("error", reject);
  });
}

function extractFrontmatter(source) {
  const m = source.match(/^---\n([\s\S]*?)\n---\n/);
  if (!m) throw new Error("no Astro frontmatter (--- ... ---) in source");
  return m[1];
}

function sanitiseFrontmatter(fm) {
  // Drop imports (would fail to resolve in this context) and lines that
  // reach into `import.meta` (Astro-only). Leave const / let / function
  // declarations intact so consts the test asks for are still in scope.
  //
  // Then prepend stubs for the most common Astro-injected globals so
  // helper functions in the frontmatter (e.g. `withBase` referencing
  // `base = import.meta.env.BASE_URL`) don't ReferenceError when re-
  // evaluated outside Astro.
  const stubs = `const base = "";\n`;
  return (
    stubs +
    fm
      .split("\n")
      .filter((line) => !/^\s*import\b/.test(line))
      .filter((line) => !/\bimport\.meta\b/.test(line))
      .join("\n")
  );
}

async function main() {
  const raw = await readStdin();
  const req = JSON.parse(raw);
  const src = readFileSync(req.path, "utf-8");
  const cleaned = sanitiseFrontmatter(extractFrontmatter(src));

  // Append a JSON serialiser for each requested name so we get a single
  // structured payload back. ``stringify`` runs after every const in
  // ``cleaned`` is in scope.
  const names = req.names || [];
  const payload = names.map((n) => `"${n}": typeof ${n} !== 'undefined' ? ${n} : null`);
  const program = `${cleaned}\n;process.stdout.write(JSON.stringify({${payload.join(",")}}));`;

  const transpiled = transformSync(program, {
    loader: "ts",
    target: "es2020",
    format: "esm",
  }).code;

  // Evaluate in this same module — esbuild output is plain JS now.
  // Using indirect eval keeps the top-level scope clean.
  (0, eval)(transpiled);
}

main().catch((e) => {
  process.stderr.write(`extract_astro_vars: ${(e && e.stack) || e}\n`);
  process.exit(1);
});
