import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const clientDir = path.dirname(fileURLToPath(import.meta.url));

await build({
  entryPoints: [path.join(clientDir, "index.ts")],
  outfile: path.join(clientDir, "..", "runtime.js"),
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  charset: "ascii",
  logLevel: "info",
});
