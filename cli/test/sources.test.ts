import assert from "node:assert/strict";
import { appendFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { test } from "node:test";
import type { CommandSource, FileSource, HttpSource } from "../src/config.js";
import { createProbe } from "../src/sources.js";
import { nodeCommand, tempDir } from "./helpers.js";

function commandSource(run: string, timeoutSeconds = 20): CommandSource {
  return { type: "command", name: "job", cwd: process.cwd(), intervalSeconds: 1, timeoutSeconds, run };
}

test("a command that exits 0 is healthy", async () => {
  const dir = tempDir();
  const result = await createProbe(commandSource(nodeCommand(dir, "ok", "console.log('fine')")))();
  assert.deepEqual(result, { ok: true, error: "", exitCode: 0 });
});

test("a failing command reports its exit code and output", async () => {
  const dir = tempDir();
  const run = nodeCommand(dir, "bad", "console.error('disk full'); process.exit(3)");
  const result = await createProbe(commandSource(run))();

  assert.equal(result.ok, false);
  assert.equal(result.exitCode, 3);
  assert.match(result.error, /Exited with code 3/);
  assert.match(result.error, /disk full/);
});

test("a command that outlives its timeout is killed and reported as timed out", async () => {
  const dir = tempDir();
  const run = nodeCommand(dir, "hang", "setInterval(() => {}, 1000)");
  const started = Date.now();
  const result = await createProbe(commandSource(run, 1))();

  assert.equal(result.ok, false);
  assert.match(result.error, /Timed out after 1s/);
  assert.ok(Date.now() - started < 10_000, "the hung command was not stopped promptly");
});

test("a command that cannot start fails instead of throwing", async () => {
  const result = await createProbe({ ...commandSource("definitely-not-a-real-binary-xyz"), cwd: tempDir() })();
  assert.equal(result.ok, false);
  assert.notEqual(result.exitCode, 0);
});

function fileSource(file: string, pattern = /ERROR|FATAL/): FileSource {
  return { type: "file", name: "log", cwd: process.cwd(), intervalSeconds: 1, path: file, pattern };
}

test("a log file is followed from its end and only new matching lines fail", async () => {
  const file = path.join(tempDir(), "etl.log");
  writeFileSync(file, "ERROR from yesterday\n");
  const probe = createProbe(fileSource(file));

  assert.equal((await probe()).ok, true, "history must be ignored");
  appendFileSync(file, "INFO all good\n");
  assert.equal((await probe()).ok, true);
  appendFileSync(file, "INFO step 2\nFATAL out of memory\n");
  const failed = await probe();
  assert.equal(failed.ok, false);
  assert.equal(failed.error, "FATAL out of memory");
  assert.equal((await probe()).ok, true, "the same line must not be reported twice");
});

test("a line written in two pieces is matched once it is complete", async () => {
  const file = path.join(tempDir(), "etl.log");
  writeFileSync(file, "");
  const probe = createProbe(fileSource(file));
  await probe();

  appendFileSync(file, "ERR");
  assert.equal((await probe()).ok, true);
  appendFileSync(file, "OR half written\n");
  assert.equal((await probe()).error, "ERROR half written");
});

test("a missing file is healthy, and a file that appears later is read from the start", async () => {
  const file = path.join(tempDir(), "later.log");
  const probe = createProbe(fileSource(file));

  assert.equal((await probe()).ok, true);
  writeFileSync(file, "ERROR first line\n");
  assert.equal((await probe()).error, "ERROR first line");
});

test("a rotated or truncated file is read again from the start", async () => {
  const file = path.join(tempDir(), "rot.log");
  writeFileSync(file, "x".repeat(200) + "\n");
  const probe = createProbe(fileSource(file));
  await probe();

  rmSync(file);
  writeFileSync(file, "ERROR after rotation\n");
  assert.equal((await probe()).error, "ERROR after rotation");
});

test("a burst of errors reports at most the last 20 matching lines", async () => {
  const file = path.join(tempDir(), "burst.log");
  writeFileSync(file, "");
  const probe = createProbe(fileSource(file));
  await probe();

  appendFileSync(file, Array.from({ length: 50 }, (_, i) => `ERROR ${i}`).join("\n") + "\n");
  const lines = (await probe()).error.split("\n");
  assert.equal(lines.length, 20);
  assert.equal(lines.at(-1), "ERROR 49");
});

async function withServer(status: number, run: (url: string) => Promise<void>): Promise<void> {
  const server = createServer((_, res) => {
    res.writeHead(status);
    res.end("body");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    await run(`http://127.0.0.1:${(server.address() as AddressInfo).port}/health`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

function httpSource(url: string, expectStatus = [200], timeoutSeconds = 2): HttpSource {
  return { type: "http", name: "api", cwd: process.cwd(), intervalSeconds: 1, url, expectStatus, timeoutSeconds };
}

test("an HTTP source is healthy on an expected status and failing on any other", async () => {
  await withServer(200, async (url) => assert.equal((await createProbe(httpSource(url))()).ok, true));
  await withServer(503, async (url) => {
    const result = await createProbe(httpSource(url))();
    assert.equal(result.ok, false);
    assert.match(result.error, /HTTP 503/);
  });
  await withServer(204, async (url) => assert.equal((await createProbe(httpSource(url, [200, 204]))()).ok, true));
});

test("an unreachable HTTP endpoint is a failure, not an exception", async () => {
  const result = await createProbe(httpSource("http://127.0.0.1:1/health", [200], 1))();
  assert.equal(result.ok, false);
  assert.match(result.error, /failed/);
});
