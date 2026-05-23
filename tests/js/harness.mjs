// JSDOM harness for behavioral tests of in-page <script> bodies.
//
// Reads a JSON request from stdin, evaluates the script inside a JSDOM
// window with stubbed fetch / BroadcastChannel / timers / dialogs, then
// writes a JSON record of observed side effects to stdout. The Python
// wrapper in tests/src/unit/_js_harness.py owns the contract.
//
// Request shape:
//   {
//     script:           string,   // body to eval (no <script> tags)
//     prelude:          string,   // JS to run before `script` (e.g. Astro define:vars injection)
//     invoke:           string,   // JS to run after `script` (e.g. "await window.restartAddon()")
//     initialHtml:      string,   // DOM seed (defaults to a blank page)
//     fetchMap:         { [pattern]: { status, ok?, body?, json?, throw? } },
//     broadcastEvents:  [{ channel, data, delayMs? }],
//     settleMs:         number,   // virtual-time advance after script + invoke (defaults to 120000)
//     language:         "js" | "ts",  // when "ts", `script` and `invoke` are transpiled via esbuild
//                                     //   before being evaluated (no type-checking, just type-stripping)
//   }
//
// Response shape:
//   {
//     fetches:    [{ url, method, body }],
//     broadcasts: [{ channel, data }],
//     reloads:    number,
//     alerts:     [string],
//     confirms:   [string],
//     console:    [{ level, args }],
//     status:     string | null,        // last value passed to updateStatus()
//     dom:        string,                // window.document.body.innerHTML
//     errors:     [string],              // thrown errors during script / invoke
//   }
//
// Time is faked: setTimeout / setInterval / Date.now run on a virtual
// clock that advances by `settleMs` after the script finishes. Real
// wall-clock waits would make a 60s addon-restart probe untestable.

import { JSDOM, VirtualConsole } from "jsdom";
import { transformSync } from "esbuild";

function maybeTranspile(source, language) {
  if (!source || language !== "ts") return source;
  // type-strip only — `loader: "ts"` keeps esbuild from doing TSX/JSX
  // transforms; ES2020 target keeps modern syntax (top-level await,
  // optional chaining) intact for jsdom + node-24 to evaluate.
  return transformSync(source, {
    loader: "ts",
    target: "es2020",
    format: "esm",
  }).code;
}

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

function buildFetchStub(fetchMap, fetches) {
  return async function fetch(url, init) {
    const method = (init && init.method) || "GET";
    const body = init && init.body != null ? String(init.body) : null;
    fetches.push({ url: String(url), method, body });

    let entry = null;
    for (const [pattern, value] of Object.entries(fetchMap)) {
      if (String(url).includes(pattern)) {
        entry = value;
        break;
      }
    }
    if (entry == null) {
      // Unmatched URL: 404 by default so tests that forget to register a
      // route get a loud, predictable failure mode.
      entry = { status: 404, body: "" };
    }
    if (entry.throw) {
      throw new TypeError(entry.throw);
    }
    const status = entry.status ?? 200;
    const ok = entry.ok ?? (status >= 200 && status < 300);
    const bodyText =
      entry.json !== undefined ? JSON.stringify(entry.json) : (entry.body ?? "");
    return {
      ok,
      status,
      async json() {
        // Honour the "truncated body that fails to parse" scenario the
        // saveFeatureFlag fallback specifically guards against.
        return JSON.parse(bodyText);
      },
      async text() {
        return bodyText;
      },
    };
  };
}

class FakeClock {
  constructor() {
    this.now = 0;
    this.nextId = 1;
    this.tasks = new Map(); // id -> { time, fn, interval }
  }
  setTimeout(fn, delay = 0) {
    const id = this.nextId++;
    this.tasks.set(id, { time: this.now + Math.max(0, delay), fn, interval: null });
    return id;
  }
  setInterval(fn, delay = 0) {
    const id = this.nextId++;
    const period = Math.max(1, delay);
    this.tasks.set(id, { time: this.now + period, fn, interval: period });
    return id;
  }
  clearTimeout(id) {
    this.tasks.delete(id);
  }
  clearInterval(id) {
    this.tasks.delete(id);
  }
  // Advance virtual time up to `untilMs`, firing every ready task in
  // chronological order. Microtasks between tasks are flushed by yielding
  // to the real event loop with setImmediate.
  async advance(untilMs) {
    const target = this.now + untilMs;
    // Outer loop in case a fired task schedules more tasks before target.
    // Cap iterations to surface runaway recursion as a test failure rather
    // than hanging the suite.
    for (let safety = 0; safety < 100000; safety++) {
      let nextId = null;
      let nextTime = Infinity;
      for (const [id, t] of this.tasks) {
        if (t.time <= target && t.time < nextTime) {
          nextTime = t.time;
          nextId = id;
        }
      }
      if (nextId == null) break;
      const task = this.tasks.get(nextId);
      this.now = task.time;
      if (task.interval != null) {
        task.time = this.now + task.interval;
      } else {
        this.tasks.delete(nextId);
      }
      try {
        task.fn();
      } catch (_e) {
        // Swallow — the test asserts on side effects, not on whether
        // a single timer callback throws.
      }
      // Yield so any microtasks queued by the callback (await chains in
      // the production code) get to run before the next timer fires.
      await new Promise((r) => setImmediate(r));
    }
    this.now = target;
  }
}

class FakeBroadcastChannel {
  constructor(name, registry) {
    this.name = name;
    this.listeners = [];
    this._registry = registry;
    if (!registry.byName.has(name)) registry.byName.set(name, []);
    registry.byName.get(name).push(this);
  }
  postMessage(data) {
    this._registry.posts.push({ channel: this.name, data });
    // Same-tab self-delivery is not part of the BroadcastChannel contract
    // (browsers only deliver to OTHER same-origin contexts), so we don't
    // dispatch back to `this`. Other channels with the same name would
    // receive it; the harness uses one channel per test, so that's a
    // no-op here.
  }
  addEventListener(type, fn) {
    if (type === "message") this.listeners.push(fn);
  }
  removeEventListener(type, fn) {
    if (type === "message") {
      this.listeners = this.listeners.filter((f) => f !== fn);
    }
  }
  close() {
    this.listeners = [];
  }
  _dispatch(data) {
    const ev = { data };
    for (const fn of [...this.listeners]) {
      try {
        fn(ev);
      } catch (_e) {
        // Same rationale as the clock: surface effects, not listener throws.
      }
    }
  }
}

async function main() {
  const raw = await readStdin();
  let req;
  try {
    req = JSON.parse(raw);
  } catch (e) {
    process.stderr.write(`harness: invalid JSON request: ${e.message}\n`);
    process.exit(2);
  }

  const fetches = [];
  const broadcasts = [];
  const alerts = [];
  const confirms = [];
  const consoleLog = [];
  const errors = [];
  let reloads = 0;
  let lastStatus = null;

  const fetchMap = req.fetchMap || {};
  const settleMs = req.settleMs ?? 120000;
  const initialHtml =
    req.initialHtml || "<!DOCTYPE html><html><body></body></html>";

  const virtualConsole = new VirtualConsole();
  virtualConsole.on("log", (...args) =>
    consoleLog.push({ level: "log", args: args.map(String) }),
  );
  virtualConsole.on("warn", (...args) =>
    consoleLog.push({ level: "warn", args: args.map(String) }),
  );
  virtualConsole.on("error", (...args) =>
    consoleLog.push({ level: "error", args: args.map(String) }),
  );
  // location.reload() / assign() / href= are unforgeable [Unforgeable]
  // properties in jsdom — they cannot be monkey-patched on the instance
  // (non-configurable, non-writable). JSDOM's reload impl throws a
  // "Not implemented: navigation" error onto the virtual console's
  // jsdomError channel rather than as a real JS exception. Counting
  // those errors is the supported way to observe reload calls in tests.
  virtualConsole.on("jsdomError", (err) => {
    const msg = (err && err.message) || String(err);
    if (msg.includes("Not implemented: navigation")) {
      reloads += 1;
    } else {
      consoleLog.push({ level: "error", args: [msg] });
    }
  });

  const dom = new JSDOM(initialHtml, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole,
    url: "https://test.local/",
  });
  const { window } = dom;

  const clock = new FakeClock();
  const channelRegistry = { byName: new Map(), posts: broadcasts };

  // Wire stubs onto window. Done via Object.defineProperty for setTimeout
  // because JSDOM defines it as a non-writable getter on the prototype.
  Object.defineProperty(window, "setTimeout", {
    value: (fn, delay) => clock.setTimeout(fn, delay),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window, "clearTimeout", {
    value: (id) => clock.clearTimeout(id),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window, "setInterval", {
    value: (fn, delay) => clock.setInterval(fn, delay),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window, "clearInterval", {
    value: (id) => clock.clearInterval(id),
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window.Date, "now", {
    value: () => clock.now,
    configurable: true,
    writable: true,
  });

  window.fetch = buildFetchStub(fetchMap, fetches);
  window.BroadcastChannel = function (name) {
    return new FakeBroadcastChannel(name, channelRegistry);
  };
  // Restore typeof check: the production code does
  //   `typeof BroadcastChannel === 'function' ? new BroadcastChannel(...) : null`
  // and that branch must report 'function' for our stub.
  // (Functions naturally report 'function' for typeof, so the assignment
  // above already satisfies it; no extra work required.)

  window.alert = (msg) => {
    alerts.push(String(msg));
  };
  window.confirm = (msg) => {
    confirms.push(String(msg));
    return true;
  };

  // location.reload counting is wired through the virtualConsole's
  // jsdomError handler above — see the comment there for why the
  // unforgeable IDL property forces that route.

  // Expose a status sink so the rendered script's updateStatus() — when
  // it falls back to console-log or similar — can be observed. The script
  // generally writes into a DOM element, so the DOM dump in the response
  // covers it too; we capture window.__lastStatus when set explicitly.
  Object.defineProperty(window, "__captureStatus", {
    value: (text) => {
      lastStatus = String(text);
    },
    configurable: true,
    writable: true,
  });

  const prelude = req.prelude || "";
  const language = req.language || "js";
  let scriptBody = req.script || "";
  let invoke = req.invoke || "";
  try {
    scriptBody = maybeTranspile(scriptBody, language);
    invoke = maybeTranspile(invoke, language);
  } catch (e) {
    errors.push(`transpile failure (${language}): ${(e && e.message) || e}`);
  }

  // Wrap so awaits inside `invoke` work and so we can surface throws.
  const program = `
    (async () => {
      ${prelude}
      ${scriptBody}
      ${invoke}
    })().then(
      () => { window.__harnessDone = true; },
      (e) => { window.__harnessError = (e && e.stack) || String(e); window.__harnessDone = true; },
    );
  `;

  try {
    window.eval(program);
  } catch (e) {
    errors.push((e && e.stack) || String(e));
  }

  // Drain microtasks before advancing fake time so the script reaches its
  // first `await fetch(...)` and registers the resulting `.then` chain.
  await new Promise((r) => setImmediate(r));

  // Apply scheduled broadcast injections on the virtual clock.
  for (const evt of req.broadcastEvents || []) {
    const channels = channelRegistry.byName.get(evt.channel) || [];
    const fire = () => {
      for (const ch of channels) ch._dispatch(evt.data);
    };
    if (evt.delayMs && evt.delayMs > 0) {
      clock.setTimeout(fire, evt.delayMs);
    } else {
      fire();
    }
    await new Promise((r) => setImmediate(r));
  }

  await clock.advance(settleMs);

  // One last drain so any post-advance microtasks resolve.
  await new Promise((r) => setImmediate(r));

  if (window.__harnessError) errors.push(window.__harnessError);

  const response = {
    fetches,
    broadcasts,
    reloads,
    alerts,
    confirms,
    console: consoleLog,
    status: lastStatus,
    dom: window.document.body.innerHTML,
    errors,
  };

  process.stdout.write(JSON.stringify(response));
}

main().catch((e) => {
  process.stderr.write(`harness: fatal: ${(e && e.stack) || e}\n`);
  process.exit(1);
});
