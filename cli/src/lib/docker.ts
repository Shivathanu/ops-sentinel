import { spawnSync, type SpawnSyncReturns } from "node:child_process";

export type Spawn = (command: string, args: string[], options: { cwd: string; inherit: boolean }) => Pick<SpawnSyncReturns<string>, "status" | "stdout" | "error">;

export const realSpawn: Spawn = (command, args, options) =>
  spawnSync(command, args, { cwd: options.cwd, encoding: "utf8", stdio: options.inherit ? "inherit" : "pipe" });

/** Runs `docker compose <args>` in `dir`. Arguments are passed as an array, never through a shell. */
export function compose(dir: string, args: string[], spawn: Spawn = realSpawn, inherit = true): { ok: boolean; output: string; missing: boolean } {
  const result = spawn("docker", ["compose", ...args], { cwd: dir, inherit });
  const missing = (result.error as NodeJS.ErrnoException | undefined)?.code === "ENOENT";
  return { ok: result.status === 0 && !result.error, output: result.stdout ?? "", missing };
}

export function dockerAvailable(spawn: Spawn = realSpawn): boolean {
  const result = spawn("docker", ["compose", "version"], { cwd: process.cwd(), inherit: false });
  return result.status === 0 && !result.error;
}
