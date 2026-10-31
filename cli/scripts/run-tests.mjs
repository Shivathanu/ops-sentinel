// `node --test <directory>` changed meaning between Node 20 and 22, so list the compiled test files explicitly.
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";

const files = readdirSync(new URL("../dist/test/", import.meta.url))
  .filter((name) => name.endsWith(".test.js"))
  .map((name) => `dist/test/${name}`);
const result = spawnSync(process.execPath, ["--test", ...files], { stdio: "inherit" });
process.exit(result.status ?? 1);
