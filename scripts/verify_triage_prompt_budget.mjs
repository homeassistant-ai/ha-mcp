#!/usr/bin/env node
// Verifies the issue-triage prompt-budgeting algorithm keeps the assembled
// prompt under the GitHub Models input cap, even with worst-case inputs.
//
// The triage workflow (.github/workflows/issue-triage.yml) runs without a repo
// checkout (it pulls files via the API), so its build_prompt step cannot import
// a shared module. This harness therefore mirrors the same pure trim algorithm
// and asserts its guarantees. The constants and drop order below are kept in
// sync with the workflow by tests/src/unit/test_triage_prompt_budget.py, which
// parses both files and fails on drift.
//
// Run: node scripts/verify_triage_prompt_budget.mjs

const TOKEN_BUDGET = 7000;
const BODY_FLOOR = 2000;
const CHANGELOG_FLOOR = 1500;
const AUTHOR_FLOOR = 1500;
const estTokens = (s) => Math.ceil((s || "").length / 4);

// Mirrors build_prompt's trim loop. `staticText` stands in for the fixed
// framing + evaluation rubric + JSON schema that always ships in the prompt.
// Drop order: duplicates -> changelog -> author comments -> body, each
// secondary section to a floor. Every variable assemble() input is bounded
// (the workflow's keywordSummary is the only one left untrimmed, and it is
// already capped at the top-5 keywords upstream).
function fitToBudget({
  staticText = "",
  issueBody = "",
  duplicateSection = "",
  changelog = "",
  authorSection = "",
}) {
  let body = issueBody;
  let dup = duplicateSection;
  let log = changelog;
  let author = authorSection;
  const total = () =>
    estTokens(staticText) +
    estTokens(body) +
    estTokens(dup) +
    estTokens(log) +
    estTokens(author);
  const trimTo = (s, floor) =>
    s.substring(0, Math.max(floor, s.length - (total() - TOKEN_BUDGET) * 4));

  if (total() > TOKEN_BUDGET && dup) dup = "";
  if (total() > TOKEN_BUDGET && log.length > CHANGELOG_FLOOR) log = trimTo(log, CHANGELOG_FLOOR);
  if (total() > TOKEN_BUDGET && author.length > AUTHOR_FLOOR) author = trimTo(author, AUTHOR_FLOOR);
  if (total() > TOKEN_BUDGET && body.length > BODY_FLOOR) body = trimTo(body, BODY_FLOOR);
  return { body, dup, log, author, tokens: total() };
}

let failures = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failures++;
    console.error(`  FAIL ${name}${detail ? " — " + detail : ""}`);
  }
};

const x = (n) => "x".repeat(n);

console.log("Triage prompt budget checks (self-imposed target", TOKEN_BUDGET, "tokens, under the 8000 model cap):");

// 1. Real worst case: full framing + 4 BM25 candidates @ ~1200 chars + a
//    16000-char body + a large release-context block + long author comments.
//    This is over budget before trimming, so it exercises the full trim chain.
{
  const r = fitToBudget({
    staticText: x(7000), // ~1750 tokens of fixed framing/rubric/schema
    issueBody: x(16000),
    duplicateSection: x(4 * 1200),
    changelog: x(21000), // verbose unreleased + 3x CHANGELOG.md head -50
    authorSection: x(30000), // many/long unbounded author follow-up comments
  });
  check("worst case fits budget", r.tokens <= TOKEN_BUDGET, `got ${r.tokens} tokens`);
}

// 1b. Long author comments alone are trimmed and the prompt fits (the gap
//     before authorSection became a trim target).
{
  const r = fitToBudget({ staticText: x(7000), issueBody: x(4000), authorSection: x(80000) });
  check("long author comments trimmed to fit", r.tokens <= TOKEN_BUDGET, `got ${r.tokens} tokens`);
  check("author trimmed before body, body untouched", r.author.length < 80000 && r.body.length === 4000, `author ${r.author.length}, body ${r.body.length}`);
}

// 1c. Author-floor clamp exercised: framing alone dominates the budget, so
//     authorSection is trimmed down to exactly AUTHOR_FLOOR (the Math.max
//     clamp). Pins the constant the same way 2b pins the body floor.
{
  const r = fitToBudget({ staticText: x(40000), issueBody: x(2000), authorSection: x(50000) });
  check("author clamped exactly to floor when framing dominates", r.author.length === AUTHOR_FLOOR, `got ${r.author.length} chars`);
}

// 2. Huge body alone fits (body truncated, floor not yet binding).
{
  const r = fitToBudget({ staticText: x(7000), issueBody: x(500000) });
  check("huge body fits budget", r.tokens <= TOKEN_BUDGET, `got ${r.tokens} tokens`);
}

// 2b. Floor clamp exercised: framing alone dominates the budget, so even a
//     fully-trimmed body lands exactly on BODY_FLOOR (the Math.max clamp).
//     This is the over-budget case the workflow warns about, so assert the
//     clamp, not that it fits.
{
  const r = fitToBudget({ staticText: x(30000), issueBody: x(500000) });
  check("body clamped exactly to floor when framing dominates", r.body.length === BODY_FLOOR, `got ${r.body.length} chars`);
}

// 3. Large changelog alone is trimmed and the prompt fits (the gap that
//    shipped before changelog became a trim target).
{
  const r = fitToBudget({ staticText: x(7000), issueBody: x(4000), changelog: x(60000) });
  check("large changelog trimmed to fit", r.tokens <= TOKEN_BUDGET, `got ${r.tokens} tokens`);
  check("changelog trimmed before body, body untouched", r.log.length < 60000 && r.body.length === 4000, `log ${r.log.length}, body ${r.body.length}`);
}

// 3b. Changelog-floor clamp exercised: framing alone dominates the budget, so
//     changelog is trimmed down to exactly CHANGELOG_FLOOR. Pins the constant
//     the same way 1c/2b pin the author and body floors.
{
  const r = fitToBudget({ staticText: x(40000), issueBody: x(2000), changelog: x(50000) });
  check("changelog clamped exactly to floor when framing dominates", r.log.length === CHANGELOG_FLOOR, `got ${r.log.length} chars`);
}

// 4. Ordering: duplicates are dropped before the body or changelog is touched,
//    and the body survives intact when dropping duplicates alone suffices.
{
  const r = fitToBudget({
    staticText: x(7000),
    issueBody: x(8000),
    duplicateSection: x(21000),
  });
  check("duplicates dropped first", r.dup === "");
  check("body intact when dropping duplicates suffices", r.body.length === 8000, `got ${r.body.length} chars`);
}

// 5. Small input is left fully intact (no trimming).
{
  const body = x(1200), dup = x(600), log = x(900);
  const r = fitToBudget({ staticText: x(7000), issueBody: body, duplicateSection: dup, changelog: log });
  check("small input untouched", r.body === body && r.dup === dup && r.log === log);
}

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nAll budget checks passed.");
