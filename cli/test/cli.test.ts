import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { after, before, test } from "node:test";
import { main, shellQuote, type Io } from "../src/cli.js";
import { API_KEY, FakeEngineServer, nodeCommand, tempDir } from "./helpers.js";

const server = new FakeEngineServer();
before(() => server.start());
after(() => server.stop());

function makeIo(cwd: string, env: NodeJS.ProcessEnv = { OPS_SENTINEL_API_KEY: API_KEY }) {
  const out: string[] = [];
  const err: string[] = [];
  const io: Io = { out: (l) => out.push(l), err: (l) => err.push(l), env, cwd, version: "9.9.9", retryDelayMs: 1 };
  return { io, out, err };
}

function writeConfig(dir: string, sources: unknown[], extra: Record<string, unknown> = {}): string {
  const file = path.join(dir, "sentinel.config.json");
  writeFileSync(file, JSON.stringify({ engine: { url: server.url }, pollSeconds: 1, approvalTimeoutMinutes: 5, sources, ...extra }));
  return file;
}

const never = new AbortController().signal;

test("help, version and unknown commands", async () => {
  const a = makeIo(tempDir());
  assert.equal(await main(["help"], a.io, never), 0);
  assert.match(a.out.join("\n"), /sentinel run/);

  const b = makeIo(tempDir());
  assert.equal(await main(["--version"], b.io, never), 0);
  assert.deepEqual(b.out, ["9.9.9"]);

  const c = makeIo(tempDir());
  assert.equal(await main(["bogus"], c.io, never), 2);
  assert.match(c.err.join("\n"), /unknown command 'bogus'/);

  assert.equal(await main([], makeIo(tempDir()).io, never), 2);
});

test("init writes a sample config that passes validation, and refuses to overwrite it", async () => {
  const dir = tempDir();
  const first = makeIo(dir);
  assert.equal(await main(["init"], first.io, never), 0);
  assert.equal(await main(["check", "--offline"], first.io, never), 0);

  const again = makeIo(dir);
  assert.equal(await main(["init"], again.io, never), 2);
  assert.match(again.err.join("\n"), /already exists/);
  assert.equal(await main(["init", "--force"], again.io, never), 0);
});

test("check validates the config and pings the engine", async () => {
  const dir = tempDir();
  writeConfig(dir, [{ name: "job", type: "command", run: "true", fix: { id: "retry", run: "true" } }]);
  const ok = makeIo(dir);

  assert.equal(await main(["check"], ok.io, never), 0);
  assert.match(ok.out.join("\n"), /Engine reachable/);
  assert.match(ok.out.join("\n"), /fix 'retry'/);

  writeConfig(dir, [{ name: "job", type: "command", run: "true" }], { engine: { url: "http://127.0.0.1:1" } });
  assert.equal(await main(["check"], makeIo(dir).io, never), 1);
});

test("check fails on a bad config without touching the network, and on a missing API key", async () => {
  const dir = tempDir();
  writeConfig(dir, [{ name: "x y", type: "command" }]);
  const bad = makeIo(dir);
  assert.equal(await main(["check"], bad.io, never), 2);
  assert.match(bad.err.join("\n"), /sources\[0\]/);

  const keyless = makeIo(dir, {});
  writeConfig(dir, [{ name: "job", type: "command", run: "true" }]);
  assert.equal(await main(["check"], keyless.io, never), 2);
  assert.match(keyless.err.join("\n"), /OPS_SENTINEL_API_KEY/);
});

test("run passes a successful command through and reports nothing", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  const before = server.incidents.size;

  const code = await main(["run", "--name", "ok-job", "--", nodeCommand(dir, "ok", "")], makeIo(dir).io, never);

  assert.equal(code, 0);
  assert.equal(server.incidents.size, before);
});

test("run reports a failing command, keeps its exit code and does not wait by default", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  const { io, err } = makeIo(dir);

  const code = await main(["run", "--name", "bad-job", "--", nodeCommand(dir, "bad", "process.stderr.write('disk full\\n'); process.exit(7)")], io, never);

  assert.equal(code, 7);
  const incident = [...server.incidents.values()].at(-1);
  assert.equal(incident?.report.source, "bad-job");
  assert.equal(incident?.report.exit_code, 7);
  assert.match(incident?.report.error ?? "", /disk full/);
  assert.equal(incident?.report.fix_id, null);
  assert.match(err.join("\n"), /reported incident #\d+/);
});

test("run --wait runs the approved fix and exits 0 when it succeeds", async () => {
  const dir = tempDir();
  const marker = path.join(dir, "fixed");
  writeConfig(dir, []);
  const job = nodeCommand(dir, "job", `import { existsSync } from "node:fs"; process.exit(existsSync(${JSON.stringify(marker)}) ? 0 : 5)`);
  const fix = nodeCommand(dir, "fix", `import { writeFileSync } from "node:fs"; writeFileSync(${JSON.stringify(marker)}, "x")`);
  const approver = setInterval(() => {
    for (const [id, incident] of server.incidents) {
      if (incident.report.source === "waiter" && incident.status === "AWAITING_APPROVAL") server.setStatus(id, "APPROVED");
    }
  }, 100);

  try {
    const code = await main(["run", "--name", "waiter", "--fix-id", "make-fixed", "--fix-run", fix, "--wait", "--", job], makeIo(dir).io, never);

    assert.equal(code, 0);
    assert.ok(existsSync(marker));
    const incident = [...server.incidents.values()].find((i) => i.report.source === "waiter");
    assert.equal(incident?.status, "REMEDIATED");
    assert.equal(incident?.results[0]?.outcome, "SUCCESS");
  } finally {
    clearInterval(approver);
  }
});

test("run --wait keeps the original exit code when the fix is rejected", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  const rejecter = setInterval(() => {
    for (const [id, incident] of server.incidents) {
      if (incident.report.source === "rejected-job" && incident.status === "AWAITING_APPROVAL") server.setStatus(id, "REJECTED");
    }
  }, 100);

  try {
    const code = await main(
      ["run", "--name", "rejected-job", "--fix-id", "f", "--fix-run", "exit 0", "--wait", "--", nodeCommand(dir, "j", "process.exit(4)")],
      makeIo(dir).io,
      never,
    );
    assert.equal(code, 4);
  } finally {
    clearInterval(rejecter);
  }
});

test("run keeps the command's exit code even when the engine is unreachable", async () => {
  const dir = tempDir();
  writeConfig(dir, [], { engine: { url: "http://127.0.0.1:1" } });
  const { io, err } = makeIo(dir);

  const code = await main(["run", "--name", "j", "--", nodeCommand(dir, "j", "process.exit(9)")], io, never);

  assert.equal(code, 9);
  assert.match(err.join("\n"), /could not report/);
});

test("run takes engine settings from the environment when there is no config file", async () => {
  const dir = tempDir();
  const { io } = makeIo(dir, { OPS_SENTINEL_API_KEY: API_KEY, SENTINEL_ENGINE_URL: server.url });
  const before = server.incidents.size;

  const code = await main(["run", "--name", "env-job", "--", nodeCommand(dir, "j", "process.exit(3)")], io, never);

  assert.equal(code, 3);
  assert.equal(server.incidents.size, before + 1);
});

test("run usage errors", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  assert.equal(await main(["run"], makeIo(dir).io, never), 2);
  assert.equal(await main(["run", "--fix-id", "x", "--", "true"], makeIo(dir).io, never), 2);
  const noEngine = makeIo(tempDir(), { OPS_SENTINEL_API_KEY: API_KEY });
  assert.equal(await main(["run", "--", "true"], noEngine.io, never), 2);
  assert.match(noEngine.err.join("\n"), /SENTINEL_ENGINE_URL/);
});

test("watch --once exits 1 and reports when a source is failing, 0 when all are healthy", async () => {
  const dir = tempDir();
  writeConfig(dir, [{ name: "broken", type: "command", run: nodeCommand(dir, "b", "process.exit(1)"), fix: { id: "f", run: "true" } }]);
  const failing = makeIo(dir);
  assert.equal(await main(["watch", "--once"], failing.io, never), 1);
  assert.match(failing.out.join("\n"), /Failing: broken/);

  writeConfig(dir, [{ name: "fine", type: "command", run: nodeCommand(dir, "f", "") }]);
  assert.equal(await main(["watch", "--once"], makeIo(dir).io, never), 0);
});

test("status prints the local event log", async () => {
  const dir = tempDir();
  writeConfig(dir, [{ name: "broken", type: "command", run: nodeCommand(dir, "b", "process.exit(1)") }]);
  await main(["watch", "--once"], makeIo(dir).io, never);

  const shown = makeIo(dir);
  assert.equal(await main(["status"], shown.io, never), 0);
  assert.match(shown.out.join("\n"), /broken\s+reported #\d+/);

  const json = makeIo(dir);
  await main(["status", "--json", "-n", "1"], json.io, never);
  assert.equal((JSON.parse(json.out[0] ?? "{}") as { type: string }).type, "reported");

  assert.match(readFileSync(path.join(dir, ".sentinel/events.jsonl"), "utf8"), /"type":"reported"/);
  assert.equal(await main(["status", "-n", "0"], makeIo(dir).io, never), 2);
});

test("watch runs the whole loop: detect, wait for approval, run the fix, recover", async () => {
  const dir = tempDir();
  const healed = path.join(dir, "healed");
  const job = nodeCommand(dir, "job", `import { existsSync } from "node:fs"; if (!existsSync(${JSON.stringify(healed)})) { console.error("lock held"); process.exit(2); }`);
  const fix = nodeCommand(dir, "fix", `import { writeFileSync } from "node:fs"; writeFileSync(${JSON.stringify(healed)}, "x")`);
  writeConfig(dir, [{ name: "loop-job", type: "command", run: job, intervalSeconds: 1, fix: { id: "unlock", run: fix } }]);
  const controller = new AbortController();
  const { io, err } = makeIo(dir);
  const running = main(["watch"], io, controller.signal);

  const find = () => [...server.incidents.entries()].find(([, i]) => i.report.source === "loop-job");
  await waitFor(() => find() !== undefined, "the failure to be reported");
  const [id] = find() ?? [];
  assert.ok(id !== undefined);
  assert.ok(!existsSync(healed), "the fix must not run before approval");

  server.setStatus(id, "APPROVED");
  await waitFor(() => server.incidents.get(id)?.status === "REMEDIATED", "the fix result to be recorded");
  await waitFor(() => /recovered/.test(err.join("\n")), "the source to recover");

  controller.abort();
  assert.equal(await running, 0);
  assert.ok(existsSync(healed));
  assert.equal(server.incidents.get(id)?.results[0]?.outcome, "SUCCESS");
  assert.equal([...server.incidents.values()].filter((i) => i.report.source === "loop-job").length, 1, "must report once");
});

async function waitFor(condition: () => boolean, what: string, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) assert.fail(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

test("shellQuote leaves safe words alone and quotes everything else as one word", () => {
  assert.equal(shellQuote("./run.sh"), "./run.sh");
  if (process.platform !== "win32") {
    assert.equal(shellQuote("a b"), "'a b'");
    assert.equal(shellQuote("it's"), "'it'\\''s'");
    assert.equal(shellQuote("$(rm -rf /)"), "'$(rm -rf /)'");
  }
});
