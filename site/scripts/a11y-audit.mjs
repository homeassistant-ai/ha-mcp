// Accessibility audit (#1595): runs axe-core over the built static pages via
// jsdom. Structural rules only — jsdom has no layout engine, so color-contrast
// is disabled here and covered instead by the in-UI 4.5:1 warning path (#1574)
// and manual review. Catches landmark, label, ARIA, duplicate-id and region
// regressions cheaply, with no headless browser.
//
// Exits non-zero when violations are found; the CI `site-checks` step runs this
// as a blocking gate (the baseline is clean, so any new violation fails the
// PR). Run `npm run build` first so dist/ exists.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { JSDOM } from 'jsdom';
import axe from 'axe-core';

const root = fileURLToPath(new URL('..', import.meta.url));
const distDir = join(root, 'dist');

function htmlFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) out.push(...htmlFiles(p));
    else if (entry.endsWith('.html')) out.push(p);
  }
  return out;
}

async function auditFile(file) {
  const html = readFileSync(file, 'utf-8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  // Evaluate axe inside jsdom's VM context so it sees that document's globals
  // — vm.runInContext rather than eval(), per the repo's no-eval rule.
  vm.runInContext(axe.source, dom.getInternalVMContext());
  const results = await window.axe.run(window.document, {
    resultTypes: ['violations'],
    // No layout in jsdom — contrast can't be measured here.
    rules: { 'color-contrast': { enabled: false } },
  });
  window.close();
  return results.violations;
}

let files;
try {
  files = htmlFiles(distDir);
} catch {
  console.error(`a11y-audit: no dist/ at ${distDir} — run \`npm run build\` first.`);
  process.exit(2);
}
if (!files.length) {
  console.error('a11y-audit: no .html files in dist/.');
  process.exit(2);
}

let total = 0;
for (const file of files.sort()) {
  const violations = await auditFile(file);
  const rel = relative(root, file);
  if (!violations.length) {
    console.log(`ok    ${rel}`);
    continue;
  }
  total += violations.length;
  console.log(`FAIL  ${rel} - ${violations.length} violation(s):`);
  for (const v of violations) {
    console.log(`        [${v.impact}] ${v.id}: ${v.help}`);
    for (const node of v.nodes.slice(0, 5)) {
      console.log(`          at ${node.target.join(' ')}`);
    }
    console.log(`        ${v.helpUrl}`);
  }
}

console.log('');
if (total) {
  console.log(`a11y-audit: ${total} violation(s) across ${files.length} page(s).`);
  process.exit(1);
}
console.log(`a11y-audit: clean - 0 violations across ${files.length} page(s).`);
