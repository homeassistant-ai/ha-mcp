import { readFileSync } from "node:fs";

const yaml = readFileSync(
  new URL("../../.github/workflows/close-needs-info.yml", import.meta.url),
  "utf8",
);
const closeWorkflowCode = yaml
  .split("script: |\n")[1]
  .split("\n")
  .map((line) => line.replace(/^ {12}/, ""))
  .join("\n");

export async function runCloseWorkflow(github, hooks = {}) {
  const failures = [];
  await new Function(
    "github",
    "context",
    "core",
    `return (async () => {${closeWorkflowCode}})()`,
  )(
    github,
    { repo: { owner: "test", repo: "repo" } },
    {
      info: hooks.info ?? (() => {}),
      warning: hooks.warning ?? (() => {}),
      setFailed(message) {
        failures.push(message);
      },
    },
  );
  if (failures.length) throw Error(failures.join("\n"));
}
