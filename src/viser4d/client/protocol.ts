import type { AudioArrayPayload, RuntimeMessage, RuntimeValue } from "./binary";

export type AudioOp =
  | {
      op: "add";
      name: string;
      sampleRate: number;
      waveform: AudioArrayPayload;
      volume: number;
    }
  | {
      op: "set_volume";
      name: string;
      volume: number;
    }
  | {
      op: "set_waveform";
      name: string;
      waveform: AudioArrayPayload;
    }
  | {
      op: "append";
      name: string;
      waveform: AudioArrayPayload;
    }
  | {
      op: "remove";
      name: string;
    };

export type RuntimeConfig = {
  numSteps: number;
  fps: number;
  baseFps: number | null;
  loop: boolean;
  timestepSyncUuid: string | null;
};

export type ViewerLike = {
  mutable: {
    current: {
      messageQueue: RuntimeMessage[];
      sendMessage(message: {
        type: "GuiUpdateMessage";
        uuid: string;
        updates: { [key: string]: RuntimeValue | undefined };
      }): void;
    };
  };
  useSceneTree: unknown;
};

type TimelineRuntimeWindow = Window & {
  __VISER4D__?: unknown;
  AudioContext?: typeof AudioContext;
  webkitAudioContext?: typeof AudioContext;
};

type ReactFiberNode = {
  memoizedProps?: {
    value?: Partial<ViewerLike> & Record<string, unknown>;
  };
  child?: ReactFiberNode | null;
  sibling?: ReactFiberNode | null;
};

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object";
}

function isViewerLike(value: unknown): value is ViewerLike {
  return (
    isObjectRecord(value) &&
    isObjectRecord(value.mutable) &&
    "useSceneTree" in value
  );
}

export function getWindow(): TimelineRuntimeWindow {
  return window as TimelineRuntimeWindow;
}

export function findViewer(): ViewerLike {
  const root = document.getElementById("root");
  if (!root) {
    throw new Error("[viser4d] Could not find #root while locating the viewer.");
  }
  const rootRecord = root as unknown;
  if (!isObjectRecord(rootRecord)) {
    throw new Error("[viser4d] React root is not an object while locating the viewer.");
  }
  const containerKey = Object.keys(rootRecord).find((key) =>
    key.startsWith("__reactContainer$"),
  );
  const reactRoot = containerKey ? rootRecord[containerKey] : null;
  if (!isObjectRecord(reactRoot)) {
    throw new Error("[viser4d] Could not find the React container while locating the viewer.");
  }
  const seen = new Set<unknown>();
  const stack: ReactFiberNode[] = [reactRoot as ReactFiberNode];
  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || seen.has(fiber)) {
      continue;
    }
    seen.add(fiber);
    const candidate = fiber.memoizedProps?.value;
    if (isViewerLike(candidate)) {
      return candidate;
    }
    if (fiber.child) {
      stack.push(fiber.child);
    }
    if (fiber.sibling) {
      stack.push(fiber.sibling);
    }
  }
  throw new Error("[viser4d] Could not locate the viewer in the React fiber tree.");
}
