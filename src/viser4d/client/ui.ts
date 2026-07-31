// The runtime-owned playback bar: a self-contained DOM overlay (no external
// CSS) shown only in websocket mode. It reports intent through callbacks and
// reflects transport state each tick without fighting an active slider drag.

export type UiCallbacks = {
  play(): void;
  pause(): void;
  prev(): void;
  next(): void;
  seek(step: number): void;
  setSpeed(speed: number): void;
  setLoop(loop: boolean): void;
};

export type UiState = {
  playing: boolean;
  step: number;
  total: number;
  speed: number;
  loop: boolean;
};

const SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4];

const ICON = {
  play: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M4 3.2v9.6l7.5-4.8z"/></svg>',
  pause:
    '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="3.5" y="3" width="3" height="10" rx="1"/><rect x="9.5" y="3" width="3" height="10" rx="1"/></svg>',
  prev: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M5 3v10H3.5V3zm8 0v10l-7-5z"/></svg>',
  next: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M11 3v10h1.5V3zM3 3v10l7-5z"/></svg>',
  loop: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 6a4 4 0 0 1 4-4h4l-1.5-1.5M13 10a4 4 0 0 1-4 4H5l1.5 1.5"/></svg>',
};

function button(html: string): HTMLButtonElement {
  const el = document.createElement("button");
  el.innerHTML = html;
  el.style.cssText =
    "display:flex;align-items:center;justify-content:center;width:30px;height:30px;" +
    "padding:0;border:none;border-radius:7px;background:transparent;color:#e8e8ef;" +
    "cursor:pointer;transition:background .12s;";
  el.addEventListener("pointerenter", () => {
    el.style.background = "rgba(255,255,255,0.12)";
  });
  el.addEventListener("pointerleave", () => {
    el.style.background = "transparent";
  });
  return el;
}

export class PlaybackBar {
  private readonly root = document.createElement("div");
  private readonly playBtn = button(ICON.play);
  private readonly prevBtn = button(ICON.prev);
  private readonly nextBtn = button(ICON.next);
  private readonly loopBtn = button(ICON.loop);
  private readonly slider = document.createElement("input");
  private readonly label = document.createElement("span");
  private readonly speed = document.createElement("select");
  private dragging = false;
  private state: UiState = { playing: false, step: 0, total: 1, speed: 1, loop: false };

  constructor(private readonly callbacks: UiCallbacks) {
    this.build();
  }

  mount(): void {
    document.body.appendChild(this.root);
  }

  dispose(): void {
    this.root.remove();
  }

  setState(state: UiState): void {
    this.state = state;
    this.playBtn.innerHTML = state.playing ? ICON.pause : ICON.play;
    this.slider.max = String(Math.max(0, state.total - 1));
    if (!this.dragging) {
      this.slider.value = String(state.step);
    }
    this.label.textContent = `${state.step + 1} / ${state.total}`;
    this.speed.value = String(state.speed);
    this.loopBtn.style.color = state.loop ? "#7aa2ff" : "#e8e8ef";
  }

  private build(): void {
    this.root.tabIndex = 0;
    this.root.style.cssText =
      "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483000;" +
      "display:flex;align-items:center;gap:8px;padding:8px 14px;box-sizing:border-box;" +
      "max-width:min(720px,calc(100vw - 32px));width:560px;" +
      "background:rgba(22,24,30,0.86);backdrop-filter:blur(12px);" +
      "-webkit-backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.08);" +
      "border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,0.4);" +
      "font:13px/1 ui-sans-serif,system-ui,sans-serif;color:#e8e8ef;pointer-events:auto;outline:none;";

    this.playBtn.addEventListener("click", () => {
      if (this.state.playing) {
        this.callbacks.pause();
      } else {
        this.callbacks.play();
      }
    });
    this.prevBtn.addEventListener("click", () => this.callbacks.prev());
    this.nextBtn.addEventListener("click", () => this.callbacks.next());
    this.loopBtn.addEventListener("click", () => this.callbacks.setLoop(!this.state.loop));

    this.slider.type = "range";
    this.slider.min = "0";
    this.slider.max = "0";
    this.slider.step = "1";
    this.slider.value = "0";
    this.slider.style.cssText =
      "flex:1;min-width:80px;height:4px;cursor:pointer;accent-color:#7aa2ff;";
    const stopDrag = (): void => {
      this.dragging = false;
    };
    this.slider.addEventListener("pointerdown", () => {
      this.dragging = true;
    });
    this.slider.addEventListener("pointerup", stopDrag);
    this.slider.addEventListener("pointercancel", stopDrag);
    this.slider.addEventListener("input", () => {
      this.dragging = true;
      this.callbacks.seek(Number(this.slider.value));
    });
    this.slider.addEventListener("change", stopDrag);

    this.label.style.cssText =
      "min-width:64px;text-align:center;font-variant-numeric:tabular-nums;color:#c8c8d4;";

    this.speed.style.cssText =
      "height:28px;padding:0 6px;border:1px solid rgba(255,255,255,0.1);border-radius:7px;" +
      "background:rgba(255,255,255,0.06);color:#e8e8ef;font:12px/1 inherit;cursor:pointer;";
    for (const value of SPEEDS) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = `${value}x`;
      this.speed.appendChild(option);
    }
    this.speed.addEventListener("change", () =>
      this.callbacks.setSpeed(Number(this.speed.value)),
    );

    this.root.addEventListener("keydown", (event) => {
      if (event.code === "Space") {
        event.preventDefault();
        if (this.state.playing) {
          this.callbacks.pause();
        } else {
          this.callbacks.play();
        }
      }
    });

    this.root.append(
      this.playBtn,
      this.prevBtn,
      this.nextBtn,
      this.slider,
      this.label,
      this.speed,
      this.loopBtn,
    );
  }
}
