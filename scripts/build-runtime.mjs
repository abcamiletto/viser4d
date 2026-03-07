import { build } from "esbuild";

await build({
  entryPoints: ["src/viser4d/client/index.ts"],
  outfile: "src/viser4d/runtime.js",
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  charset: "ascii",
  logLevel: "info",
});
