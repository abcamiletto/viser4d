import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const clientDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(clientDir, "..", "..", "..");
const moduleName = "viser4d._generate_runtime_message_ts";

function hasExecutable(command) {
  const result = spawnSync(command, ["--version"], {
    stdio: "ignore",
  });
  return result.status === 0;
}

export function generateRuntimeMessages() {
  const env = {
    ...process.env,
    PYTHONPATH: [path.join(repoRoot, "src"), process.env.PYTHONPATH]
      .filter(Boolean)
      .join(path.delimiter),
  };

  const command =
    process.env.VISER4D_PYTHON != null
      ? {
          executable: process.env.VISER4D_PYTHON,
          args: ["-m", moduleName],
        }
      : hasExecutable("uv")
        ? {
            executable: "uv",
            args: ["run", "python", "-m", moduleName],
          }
        : {
            executable: process.env.PYTHON ?? "python3",
            args: ["-m", moduleName],
          };

  const result = spawnSync(command.executable, command.args, {
    cwd: repoRoot,
    env,
    stdio: "inherit",
  });

  if (result.status !== 0) {
    throw new Error("Failed to generate generatedRuntimeMessages.ts.");
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  generateRuntimeMessages();
}
