import { isAudioMessage } from "../audio/messages";
import type { RuntimeMessage } from "../binary";
import { FilePlaybackAdapter } from "./filePlaybackAdapter";
import { TimelineController } from "./timelineController";
import { ViewerAdapter } from "./viewerAdapter";

export class TimelineRuntime {
  private readonly controller: TimelineController;
  private readonly filePlayback: FilePlaybackAdapter;
  private readonly viewer: ViewerAdapter;

  constructor() {
    const viewer = new ViewerAdapter({
      handleQueuedMessage: (message) => this.handleQueuedMessage(message),
      handleGuiMessage: (message) => this.controller.handleGuiMessage(message),
      handlePlaybackTimeChange: () => this.filePlayback.sync(),
      onReady: () => this.handleViewerReady(),
    });
    this.viewer = viewer;
    this.controller = new TimelineController({
      pushMessages: (messages) => viewer.pushMessages(messages),
      sendRuntimeEvent: (message) => viewer.sendMessage(message),
      updateGuiVisible: (uuid, visible) =>
        viewer.updateGuiVisible(uuid, visible),
    });
    this.filePlayback = new FilePlaybackAdapter(
      () => viewer.getPlaybackTime(),
      (event, payload) => this.controller.debug.push(event, payload),
    );
    viewer.install();
  }

  get debug() {
    return this.controller.debug;
  }

  dispose(): void {
    this.viewer.dispose();
    this.controller.dispose();
    this.filePlayback.dispose();
  }

  private handleQueuedMessage(message: RuntimeMessage): boolean {
    if (this.controller.handleQueuedMessage(message)) {
      return true;
    }
    const isWebsocket = this.viewer.getMessageSource() === "websocket";
    if (!isAudioMessage(message) || isWebsocket) {
      return false;
    }
    this.filePlayback.enqueue(message);
    return true;
  }

  private handleViewerReady(): void {
    const isWebsocket = this.viewer.getMessageSource() === "websocket";
    this.controller.onViewerReady(isWebsocket);
  }
}
