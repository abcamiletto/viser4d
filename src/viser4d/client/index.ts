// Bootstrap: dispose any prior instance, install the runtime, and expose a
// small handle on window.__VISER4D__. A page can outlive a websocket session,
// so each injected bundle gets a fresh runtime tied to the current connection.

import { isTimelineControlMessage } from "./protocol.gen";
import { Controller } from "./controller";
import { decodeRecording } from "./binary";
import { Viser } from "./viser";

class Runtime {
  private readonly viser: Viser;
  private readonly controller: Controller;

  constructor() {
    this.viser = new Viser(
      (message) => this.route(message),
      () => this.onReady(),
    );
    this.controller = new Controller({
      pushMessages: (messages) => this.viser.pushMessages(messages),
      sendEvent: (message) => {
        if (this.viser.isWebsocket) this.viser.sendMessage(message);
      },
      isWebsocket: () => this.viser.isWebsocket,
    });
    this.viser.install();
  }

  get debug(): unknown {
    return this.controller.debug();
  }

  dispose(): void {
    this.viser.dispose();
    this.controller.dispose();
  }

  private route(message: { type: string }): boolean {
    if (isTimelineControlMessage(message)) {
      this.controller.handleControl(message);
      return true;
    }
    return false;
  }

  loadRecording(payload: string): void {
    this.controller.loadRecording(decodeRecording(payload));
  }

  private onReady(): void {
    this.controller.start();
  }
}

type RuntimeHandle = { dispose(): void; debug: unknown; loadRecording(payload: string): void };
const win = window as Window & { __VISER4D__?: RuntimeHandle };
win.__VISER4D__?.dispose();
win.__VISER4D__ = new Runtime();
