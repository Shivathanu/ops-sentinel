import assert from "node:assert/strict";
import { test } from "node:test";
import { compose, type Spawn } from "../src/lib/docker.js";

test("compose passes arguments as an array and reports success, failure and a missing docker", () => {
  const calls: string[][] = [];
  const ok: Spawn = (command, args) => {
    calls.push([command, ...args]);
    return { status: 0, stdout: "out", error: undefined as never };
  };
  assert.deepEqual(compose("/p", ["up", "-d"], ok), { ok: true, output: "out", missing: false });
  assert.deepEqual(calls, [["docker", "compose", "up", "-d"]]);

  const failing: Spawn = () => ({ status: 1, stdout: "", error: undefined as never });
  assert.equal(compose("/p", ["down"], failing).ok, false);

  const missing: Spawn = () => ({ status: null, stdout: "", error: Object.assign(new Error("x"), { code: "ENOENT" }) });
  assert.equal(compose("/p", ["down"], missing).missing, true);
});
