// A stand-in for a real automation: it fails until .healed exists, like a job blocked by stale state.
import { existsSync } from "node:fs";

if (existsSync(new URL("./.healed", import.meta.url))) {
  console.log("nightly export finished: 1,284 rows written");
} else {
  console.error("ExportError: target partition is locked by a stale run (lock file older than 6h)");
  process.exit(2);
}
