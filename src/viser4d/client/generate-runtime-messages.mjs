import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const clientDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(clientDir, "..", "..", "..");
const moduleName = "viser4d._codegen";

export function generateRuntimeMessages() {
  const env = {
    ...process.env,
    PYTHONPATH: [path.join(repoRoot, "src"), process.env.PYTHONPATH]
      .filter(Boolean)
      .join(path.delimiter),
  };

  // The Python autobuild always sets VISER4D_PYTHON; a bare `npm run build`
  // falls back to the project's uv environment.
  const [executable, args] = process.env.VISER4D_PYTHON
    ? [process.env.VISER4D_PYTHON, ["-m", moduleName]]
    : ["uv", ["run", "python", "-m", moduleName]];

  const result = spawnSync(executable, args, {
    cwd: repoRoot,
    env,
    stdio: "inherit",
  });

  if (result.status !== 0) {
    throw new Error("Failed to generate protocol.gen.ts.");
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  generateRuntimeMessages();
}
