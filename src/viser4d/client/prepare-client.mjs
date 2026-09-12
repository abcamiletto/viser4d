import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const clientDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(clientDir, "..", "..", "..");

export function prepareClient() {
  const options = { cwd: repoRoot, encoding: "utf8" };
  execFileSync("uv", ["run", "python", "-m", "viser4d._codegen"], { ...options, stdio: "inherit" });
  const source =
    "import pathlib, viser_audio; print(pathlib.Path(viser_audio.__file__).parent / 'client')";
  const audioClient = execFileSync("uv", ["run", "python", "-c", source], options).trim();
  const link = path.join(clientDir, "node_modules", "viser-audio");
  fs.mkdirSync(path.dirname(link), { recursive: true });
  if (fs.lstatSync(link, { throwIfNoEntry: false })) fs.unlinkSync(link);
  fs.symlinkSync(audioClient, link, "junction");
}

if (process.argv[1] === fileURLToPath(import.meta.url)) prepareClient();
