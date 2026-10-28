import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { EngineClient, EngineError } from "../src/client.js";
import { API_KEY, FakeEngineServer } from "./helpers.js";

const server = new FakeEngineServer();
before(() => server.start());
after(() => server.stop());

const report = { source: "nightly", job: "export.sh", error: "boom", exit_code: 2, fix_id: "rerun" };

test("reports a failure with the API key and gets an incident id back", async () => {
  const client = new EngineClient(server.url, API_KEY);

  const id = await client.reportFailure(report);

  assert.equal(typeof id, "number");
  assert.equal(server.requests.at(-1)?.key, API_KEY);
  assert.deepEqual(server.incidents.get(id)?.report, report);
});

test("reads status, and sends a result only for an approved incident", async () => {
  const client = new EngineClient(server.url, API_KEY);
  const id = await client.reportFailure(report);

  assert.deepEqual(await client.getStatus(id), { incidentId: id, status: "AWAITING_APPROVAL", fixId: "rerun" });
  await assert.rejects(
    client.reportResult(id, { agent: "nightly", outcome: "SUCCESS", detail: "ok", attempts: 1 }),
    (error: unknown) => error instanceof EngineError && error.status === 409,
  );

  server.setStatus(id, "APPROVED");
  await client.reportResult(id, { agent: "nightly", outcome: "SUCCESS", detail: "ok", attempts: 1 });
  assert.equal((await client.getStatus(id)).status, "REMEDIATED");
});

test("a wrong key is an EngineError with status 401", async () => {
  const client = new EngineClient(server.url, "wrong-key");

  await assert.rejects(client.reportFailure(report), (error: unknown) => error instanceof EngineError && error.status === 401);
});

test("an unknown incident is a 404", async () => {
  await assert.rejects(
    new EngineClient(server.url, API_KEY).getStatus(999_999),
    (error: unknown) => error instanceof EngineError && error.status === 404,
  );
});

test("an unreachable engine is an EngineError without a status", async () => {
  await assert.rejects(
    new EngineClient("http://127.0.0.1:1", API_KEY, 1000).reportFailure(report),
    (error: unknown) => error instanceof EngineError && error.status === undefined && /cannot reach/.test(error.message),
  );
});

test("health works without a key", async () => {
  await new EngineClient(server.url, "").health();
  assert.equal(server.requests.at(-1)?.key, undefined);
});
