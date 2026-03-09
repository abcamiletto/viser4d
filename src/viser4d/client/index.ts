import { TimelineRuntime } from "./bridge/runtime";

const windowRef = window as Window & { __VISER4D__?: TimelineRuntime };

if (!windowRef.__VISER4D__) {
  windowRef.__VISER4D__ = new TimelineRuntime();
}
