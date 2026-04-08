import type { RuntimeMessage } from "../binary";
import {
  findPlaybackTimeSlider,
  findViewer,
  getWindow,
  type GuiUpdateMessage,
  type ViewerLike,
  type ViewerMessage,
} from "./protocol";

type ViewerAdapterHandlers = {
  handleQueuedMessage(message: RuntimeMessage): boolean;
  handleGuiMessage(message: GuiUpdateMessage): boolean;
  handlePlaybackTimeChange(): void;
  onReady(): void;
};

export class ViewerAdapter {
  private viewer: ViewerLike | null = null;
  private playbackTimeSlider: Element | null = null;
  private disposed = false;
  private undoQueueIngress: (() => void) | null = null;
  private undoGuiMessageInterceptor: (() => void) | null = null;
  private queuePush: ((...messages: RuntimeMessage[]) => number) | null = null;
  private playbackMonitor: MutationObserver | null = null;

  constructor(private handlers: ViewerAdapterHandlers) {}

  install(): void {
    this.installWhenReady();
  }

  dispose(): void {
    this.disposed = true;
    this.undoQueueIngress?.();
    this.undoQueueIngress = null;
    this.undoGuiMessageInterceptor?.();
    this.undoGuiMessageInterceptor = null;
    this.playbackMonitor?.disconnect();
    this.playbackMonitor = null;
    this.viewer = null;
    this.playbackTimeSlider = null;
    this.queuePush = null;
  }

  getMessageSource(): ViewerLike["messageSource"] {
    return this.getViewer().messageSource;
  }

  pushMessages(messages: RuntimeMessage[]): void {
    if (this.queuePush) {
      this.queuePush(...messages);
      return;
    }
    this.getViewer().mutable.current.messageQueue.push(...messages);
  }

  sendMessage(message: ViewerMessage): void {
    this.getViewer().mutable.current.sendMessage(message);
  }

  updateGuiVisible(uuid: string, visible: boolean): void {
    const viewer = this.getViewer();
    const hasGuiControl = viewer.useGuiConfig.get(uuid) !== undefined;
    if (!hasGuiControl) {
      return;
    }
    viewer.guiActions.updateGuiProps(uuid, { visible });
  }

  getPlaybackTime(): number | null {
    const slider = this.getPlaybackTimeSlider();
    if (!slider) {
      return null;
    }
    const value = slider.getAttribute("aria-valuenow");
    if (value === null) {
      return null;
    }
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  private installWhenReady(): void {
    if (this.disposed) {
      return;
    }
    try {
      this.configureQueueIngress();
      this.installGuiMessageInterceptor();
      const messageSource = this.getMessageSource();
      if (messageSource !== "websocket") {
        this.installPlaybackMonitor();
      }
      this.handlers.onReady();
    } catch {
      if (!this.disposed) {
        getWindow().requestAnimationFrame(() => this.installWhenReady());
      }
    }
  }

  private getViewer(): ViewerLike {
    if (!this.viewer) {
      this.viewer = findViewer();
    }
    return this.viewer;
  }

  private getPlaybackTimeSlider(): Element | null {
    if (!this.playbackTimeSlider || !this.playbackTimeSlider.isConnected) {
      this.playbackTimeSlider = findPlaybackTimeSlider();
    }
    return this.playbackTimeSlider;
  }

  private configureQueueIngress(): void {
    if (this.undoQueueIngress) {
      return;
    }
    const queue = this.getViewer().mutable.current.messageQueue;
    const originalPush = queue.push.bind(queue);
    const wrappedPush = (...messages: RuntimeMessage[]): number => {
      const forwarded: RuntimeMessage[] = [];
      for (const message of messages) {
        if (this.handlers.handleQueuedMessage(message)) {
          continue;
        }
        forwarded.push(message);
      }
      return originalPush(...forwarded);
    };
    this.queuePush = originalPush;
    queue.push = wrappedPush;
    this.undoQueueIngress = () => {
      if (queue.push === wrappedPush) {
        queue.push = originalPush;
      }
    };
  }

  private installGuiMessageInterceptor(): void {
    if (this.undoGuiMessageInterceptor) {
      return;
    }
    const mutable = this.getViewer().mutable.current;
    let rawSendMessage = mutable.sendMessage;
    const wrappedSendMessage = (message: ViewerMessage): void => {
      const isGuiUpdate = message.type === "GuiUpdateMessage";
      if (isGuiUpdate) {
        const guiMessage = message as GuiUpdateMessage;
        if (this.handlers.handleGuiMessage(guiMessage)) {
          return;
        }
      }
      rawSendMessage(message);
    };
    Object.defineProperty(mutable, "sendMessage", {
      configurable: true,
      enumerable: true,
      get: () => wrappedSendMessage,
      set: (value) => {
        rawSendMessage = value;
      },
    });
    this.undoGuiMessageInterceptor = () => {
      Object.defineProperty(mutable, "sendMessage", {
        configurable: true,
        enumerable: true,
        writable: true,
        value: rawSendMessage,
      });
    };
  }

  private installPlaybackMonitor(): void {
    if (this.playbackMonitor) {
      return;
    }
    const slider = this.getPlaybackTimeSlider();
    if (!slider) {
      throw new Error("[viser4d] Could not find the playback slider.");
    }
    this.playbackMonitor = new MutationObserver(() => {
      this.handlers.handlePlaybackTimeChange();
    });
    this.playbackMonitor.observe(slider, {
      attributes: true,
      attributeFilter: ["aria-valuenow"],
    });
    this.handlers.handlePlaybackTimeChange();
  }
}
