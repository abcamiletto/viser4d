import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { generateRuntimeMessages } from "./generate-runtime-messages.mjs";

const clientDir = path.dirname(fileURLToPath(import.meta.url));

// Codegen is owned by the Python package. Allow skipping it so the runtime can
// be built while the Python side is rewritten (protocol.gen.ts is then treated
// as a checked-in, read-only input).
const skipCodegen =
  process.env.VISER4D_SKIP_CODEGEN === "1" ||
  process.argv.includes("--no-codegen");

if (!skipCodegen) {
  generateRuntimeMessages();
}

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
