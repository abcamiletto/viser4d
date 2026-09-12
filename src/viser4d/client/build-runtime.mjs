import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { prepareClient } from "./prepare-client.mjs";

const clientDir = path.dirname(fileURLToPath(import.meta.url));
prepareClient();
await build({
  entryPoints: [path.join(clientDir, "index.ts")],
  outfile: path.join(clientDir, "..", "runtime.js"),
  bundle: true,
  preserveSymlinks: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  charset: "ascii",
  logLevel: "info",
});
