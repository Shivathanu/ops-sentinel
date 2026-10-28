#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { main } from "./cli.js";

function readVersion(): string {
  const file = fileURLToPath(new URL("../../package.json", import.meta.url));
  return (JSON.parse(readFileSync(file, "utf8")) as { version: string }).version;
}

const controller = new AbortController();
let interrupted = false;
for (const name of ["SIGINT", "SIGTERM"] as const) {
  process.on(name, () => {
    // A second signal means the first graceful stop is taking too long.
    if (interrupted) process.exit(130);
    interrupted = true;
    process.stderr.write(`\nReceived ${name}; stopping. Send it again to force quit.\n`);
    controller.abort();
  });
}

main(
  process.argv.slice(2),
  {
    out: (line) => process.stdout.write(`${line}\n`),
    err: (line) => process.stderr.write(`${line}\n`),
    env: process.env,
    cwd: process.cwd(),
    version: readVersion(),
  },
  controller.signal,
).then(
  (code) => process.exit(code),
  (error: unknown) => {
    process.stderr.write(`fatal: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n`);
    process.exit(1);
  },
);
