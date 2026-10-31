// Removes stale compiled files so a renamed or deleted source file cannot linger in dist and get published.
import { rmSync } from "node:fs";

rmSync(new URL("../dist/", import.meta.url), { recursive: true, force: true });
