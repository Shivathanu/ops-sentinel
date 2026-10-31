// The pre-approved fix for flaky-job.mjs: clear the stale lock. Safe to run more than once.
import { writeFileSync } from "node:fs";

writeFileSync(new URL("./.healed", import.meta.url), "cleared\n");
console.log("stale lock cleared");
