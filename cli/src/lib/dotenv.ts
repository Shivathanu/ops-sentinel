import { existsSync, readFileSync } from "node:fs";

/** Parses KEY=VALUE lines. Blank lines and `#` comments are skipped; matching single or double quotes are removed. */
export function parseDotenv(text: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line === "" || line.startsWith("#")) continue;
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!match) continue;
    const key = match[1] as string;
    let value = (match[2] as string).trim();
    const quote = value[0];
    if ((quote === '"' || quote === "'") && value.endsWith(quote) && value.length >= 2) {
      value = value.slice(1, -1);
    } else {
      // Unquoted values may carry a trailing comment.
      value = value.replace(/\s+#.*$/, "");
    }
    values[key] = value;
  }
  return values;
}

/** Loads a `.env` file into a copy of `env`. Variables already set in `env` win, so CI and shell settings override the file. */
export function withDotenv(file: string, env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  if (!existsSync(file)) return env;
  const merged: NodeJS.ProcessEnv = { ...parseDotenv(readFileSync(file, "utf8")) };
  for (const [key, value] of Object.entries(env)) {
    if (value !== undefined && value !== "") merged[key] = value;
  }
  return merged;
}
