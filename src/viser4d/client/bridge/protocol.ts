import type { RuntimeMessage, RuntimeValue } from "../binary";

export type RuntimeConfig = {
  clientId: number | null;
  numSteps: number;
  timelineFps: number;
  speed: number;
  loop: boolean;
  timelineSliderUuid: string | null;
  speedSliderUuid: string | null;
  stepButtonsUuid: string | null;
  playButtonUuid: string | null;
  pauseButtonUuid: string | null;
  speedSyncUuid: string | null;
  playbackStateSyncUuid: string | null;
  timestepSyncUuid: string | null;
};

export type GuiUpdateMessage = {
  type: "GuiUpdateMessage";
  uuid: string;
  updates: { [key: string]: RuntimeValue | undefined };
};

export type ViewerMessage = {
  type: string;
  [key: string]: unknown;
};

type GuiStateLike = {
  guiConfigFromUuid: { [uuid: string]: unknown | undefined };
  updateGuiProps(
    uuid: string,
    updates: { [key: string]: RuntimeValue | undefined },
  ): void;
};

type UseGuiLike = {
  getState(): GuiStateLike;
};

export type ViewerLike = {
  messageSource?: "websocket" | "file_playback" | "embed";
  mutable: {
    current: {
      messageQueue: RuntimeMessage[];
      sendMessage: (message: ViewerMessage) => void;
    };
  };
  useGui: UseGuiLike;
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
    "useGui" in value &&
    "useSceneTree" in value
  );
}

function getReactRoot(): ReactFiberNode {
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
  return reactRoot as ReactFiberNode;
}

function findFiber(
  predicate: (fiber: ReactFiberNode) => boolean,
): ReactFiberNode | null {
  const seen = new Set<unknown>();
  const stack: ReactFiberNode[] = [getReactRoot()];
  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || seen.has(fiber)) {
      continue;
    }
    seen.add(fiber);
    if (predicate(fiber)) {
      return fiber;
    }
    if (fiber.child) {
      stack.push(fiber.child);
    }
    if (fiber.sibling) {
      stack.push(fiber.sibling);
    }
  }
  return null;
}

export function getWindow(): TimelineRuntimeWindow {
  return window as TimelineRuntimeWindow;
}

export function findViewer(): ViewerLike {
  const fiber = findFiber((candidate) => isViewerLike(candidate.memoizedProps?.value));
  if (fiber) {
    return fiber.memoizedProps!.value as ViewerLike;
  }
  throw new Error("[viser4d] Could not locate the viewer in the React fiber tree.");
}

export function findPlaybackTimeSlider(): Element | null {
  return document.querySelector("[role='slider'][aria-valuenow]");
}
