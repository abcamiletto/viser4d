import { TimelineRuntime } from "./bridge/runtime";

const windowRef = window as Window & { __VISER4D__?: TimelineRuntime };

// The page can outlive a websocket session, so each injected bundle gets a
// fresh runtime tied to the current connection.
windowRef.__VISER4D__?.dispose();
windowRef.__VISER4D__ = new TimelineRuntime();
