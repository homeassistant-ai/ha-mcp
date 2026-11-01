// Bundle an inline entry script with esbuild so its relative imports resolve,
// and print the result as one classic-script IIFE for the JSDOM harness.
//
// Astro pages keep their behaviour in ES modules under site/src/scripts/ and
// load them from a short `<script>` body (`import { init } from '...'; init();`).
// JSDOM's vm context runs classic scripts only, so the tests bundle that entry
// body with the same relative base the page has (`resolveDir`) and run the
// bundle. `define` maps Vite-time constants such as `import.meta.env.BASE_URL`
// to literal values, the way Astro's env plugin does at build time.
//
// With `names`, the bundle is evaluated here instead and the named globals it
// sets are printed as JSON. The Python wrapper uses that to read exported data
// (`import { x } from './mod'; globalThis.x = x;`) without a second node
// process per name.
//
// Usage (from Python):
//   echo '{"contents": "...", "resolveDir": "/abs/dir", "define": {...}, "names": [...]}' \
//     | node tests/js/bundle_script.mjs
// Outputs: the bundled code, or {"<name>": <value>, ...} when `names` is given.

import { buildSync } from "esbuild";
import { createContext, runInContext } from "node:vm";

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

async function main() {
  const req = JSON.parse(await readStdin());
  const result = buildSync({
    stdin: {
      contents: req.contents,
      resolveDir: req.resolveDir,
      loader: req.loader || "ts",
    },
    bundle: true,
    write: false,
    format: "iife",
    target: "es2020",
    define: req.define || {},
    logLevel: "silent",
  });
  const code = result.outputFiles[0].text;
  const names = req.names || [];
  if (!names.length) {
    process.stdout.write(code);
    return;
  }
  const ctx = createContext({});
  runInContext(code, ctx, { filename: "bundle_script:names" });
  const payload = {};
  for (const name of names) {
    payload[name] = ctx[name] === undefined ? null : ctx[name];
  }
  process.stdout.write(JSON.stringify(payload));
}

main().catch((e) => {
  process.stderr.write(`bundle_script: ${(e && e.stack) || e}\n`);
  process.exit(1);
});
