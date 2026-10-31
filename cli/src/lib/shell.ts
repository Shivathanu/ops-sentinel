import path from "node:path";

/** Quotes one argument so a shell passes it through as a single word. */
export function shellQuote(arg: string): string {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(arg)) return arg;
  if (process.platform === "win32") return `"${arg.replace(/"/g, '\\"')}"`;
  return `'${arg.replace(/'/g, `'\\''`)}'`;
}

/** A source name derived from the first word of a wrapped command, such as `export` for `./export.sh`. */
export function defaultName(first: string): string {
  const base = path.basename(first).replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_.-]/g, "-");
  return base === "" ? "job" : base.slice(0, 100);
}
