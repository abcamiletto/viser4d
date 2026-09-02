// The only module that touches the viser frontend, and the only place `any` is
// permitted (the viewer is untyped). Browser-side coupling inventory:
//
// - React fiber walk from `#root` / `__reactContainer$*` to the viewer context,
//   duck-typed on `mutable` + `useGuiConfig` + `guiActions` + `useSceneTree`.
// - `viewer.mutable.current.messageQueue.push` is wrapped: the single inbound
//   seam, intercepting `Timeline*` control messages and forwarding the rest.
// - `viewer.mutable.current.sendMessage(message)` for outbound events.
// - `viewer.messageSource` distinguishes websocket from file playback.
// - `filePlayback.ts` additionally scrapes `[role='slider'][aria-valuenow]`,
//   viser's native player position.
//
// Discovery failures retry on animation frames and then log loudly; there is no
// silent degradation.

import type { ScenePayload } from "./binary";

/* eslint-disable @typescript-eslint/no-explicit-any */

type QueueMessage = { type: string };

/** Returns true if the runtime consumed the message (viser must not see it). */
export type InboundHandler = (message: QueueMessage) => boolean;

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object";
}

function isViewer(value: unknown): value is Record<string, any> {
  return (
    isRecord(value) &&
    isRecord(value.mutable) &&
    "useGuiConfig" in value &&
    "guiActions" in value &&
    "useSceneTree" in value
  );
}

function reactRoot(): Record<string, any> | null {
  const root = document.getElementById("root") as unknown;
  if (!isRecord(root)) {
    return null;
  }
  const key = Object.keys(root).find((name) => name.startsWith("__reactContainer$"));
  const container = key ? root[key] : null;
  return isRecord(container) ? (container as Record<string, any>) : null;
}

function findViewer(): Record<string, any> | null {
  const start = reactRoot();
  if (!start) {
    return null;
  }
  const seen = new Set<unknown>();
  const stack: Record<string, any>[] = [start];
  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || seen.has(fiber)) {
      continue;
    }
    seen.add(fiber);
    const value = fiber.memoizedProps?.value;
    if (isViewer(value)) {
      return value;
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

const RETRY_BUDGET_MS = 5000;

export class Viser {
  private viewer: Record<string, any> | null = null;
  private queue: QueueMessage[] | null = null;
  private originalPush: ((...messages: QueueMessage[]) => number) | null = null;
  private disposed = false;
  private deadline = 0;

  constructor(
    private readonly onMessage: InboundHandler,
    private readonly onReady: () => void,
  ) {}

  install(): void {
    this.deadline = performance.now() + RETRY_BUDGET_MS;
    this.tryInstall();
  }

  dispose(): void {
    this.disposed = true;
    if (this.queue && this.originalPush) {
      this.queue.push = this.originalPush;
    }
    this.viewer = null;
    this.queue = null;
    this.originalPush = null;
  }

  get isWebsocket(): boolean {
    return this.viewer?.messageSource === "websocket";
  }

  /** Push scene payloads into viser, bypassing our own interception. */
  pushMessages(messages: ScenePayload[]): void {
    if (!messages.length || !this.originalPush) {
      return;
    }
    this.originalPush(...(messages as QueueMessage[]));
  }

  /** Send an event back to the server through viser's normal path. */
  sendMessage(message: QueueMessage): void {
    this.viewer?.mutable.current.sendMessage(message);
  }

  private tryInstall(): void {
    if (this.disposed || this.originalPush) {
      return;
    }
    const viewer = findViewer();
    if (viewer) {
      this.viewer = viewer;
      this.wrapQueue(viewer);
      this.onReady();
      return;
    }
    if (performance.now() >= this.deadline) {
      console.error(
        "[viser4d] Could not locate the viewer in the React fiber tree after " +
          "5s of retries; the timeline runtime is inactive.",
      );
      return;
    }
    requestAnimationFrame(() => this.tryInstall());
  }

  private wrapQueue(viewer: Record<string, any>): void {
    const queue = viewer.mutable.current.messageQueue as QueueMessage[];
    const original = queue.push.bind(queue) as (...m: QueueMessage[]) => number;
    queue.push = (...messages: QueueMessage[]): number => {
      const forwarded: QueueMessage[] = [];
      for (const message of messages) {
        if (!this.onMessage(message)) {
          forwarded.push(message);
        }
      }
      return forwarded.length ? original(...forwarded) : queue.length;
    };
    this.queue = queue;
    this.originalPush = original;
  }
}
