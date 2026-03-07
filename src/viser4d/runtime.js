"use strict";
(() => {
  // src/viser4d/runtime-src/binary.ts
  function decodeBase64Bytes(base64Text) {
    const binary = atob(base64Text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }
  function isBinaryPayload(value) {
    return typeof value.__viser4d_binary__ === "string";
  }
  function revive(value) {
    if (Array.isArray(value)) {
      return value.map((item) => revive(item));
    }
    if (!value || typeof value !== "object") {
      return value;
    }
    const record = value;
    if (isBinaryPayload(record)) {
      return decodeBase64Bytes(record.__viser4d_binary__);
    }
    const out = {};
    for (const [key, inner] of Object.entries(record)) {
      out[key] = inner === void 0 ? void 0 : revive(inner);
    }
    return out;
  }
  function reviveMessage(message) {
    return revive(message);
  }
  function decodeAudioArray(payload) {
    const buffer = decodeBase64Bytes(payload.data).buffer;
    switch (payload.dtype) {
      case "int16":
        return new Int16Array(buffer);
      case "int32":
        return new Int32Array(buffer);
      case "float64":
        return new Float64Array(buffer);
      case "float32":
        return new Float32Array(buffer);
      case "uint8":
        return new Uint8Array(buffer);
      default:
        return new Int16Array(buffer);
    }
  }
  function samplesToFloat32(samples) {
    if (samples instanceof Float32Array) {
      return samples;
    }
    if (samples instanceof Float64Array) {
      return Float32Array.from(samples);
    }
    if (samples instanceof Int16Array) {
      const out = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i += 1) {
        out[i] = (samples[i] ?? 0) / 32768;
      }
      return out;
    }
    if (samples instanceof Int32Array) {
      const out = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i += 1) {
        out[i] = (samples[i] ?? 0) / 2147483648;
      }
      return out;
    }
    if (samples instanceof Uint8Array) {
      const out = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i += 1) {
        out[i] = ((samples[i] ?? 128) - 128) / 128;
      }
      return out;
    }
    return Float32Array.from(samples);
  }

  // src/viser4d/runtime-src/runtime.ts
  var debugState = {
    enabled: false,
    logs: [],
    maxLogs: 400,
    push(event, payload) {
      this.logs.push({
        time: Number(performance.now().toFixed(1)),
        event,
        payload
      });
      if (this.logs.length > this.maxLogs) {
        this.logs.shift();
      }
      if (this.enabled) {
        console.debug("[viser4d]", event, payload);
      }
    },
    clear() {
      this.logs.length = 0;
    },
    setEnabled(enabled) {
      this.enabled = !!enabled;
    }
  };
  function getWindow() {
    return window;
  }
  function isObjectRecord(value) {
    return !!value && typeof value === "object";
  }
  function isViewerLike(value) {
    return isObjectRecord(value) && isObjectRecord(value.mutable) && "useSceneTree" in value;
  }
  function findViewer() {
    const root = document.getElementById("root");
    if (!root) {
      return null;
    }
    const rootRecord = root;
    if (!isObjectRecord(rootRecord)) {
      return null;
    }
    const containerKey = Object.keys(rootRecord).find(
      (key) => key.startsWith("__reactContainer$")
    );
    const reactRoot = containerKey ? rootRecord[containerKey] : null;
    if (!isObjectRecord(reactRoot)) {
      return null;
    }
    const seen = /* @__PURE__ */ new Set();
    const stack = [reactRoot];
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
    return null;
  }
  function makeTrackState(step, sampleRate = 44100) {
    return {
      sampleRate,
      waveform: new Float32Array(0),
      volume: 1,
      startStep: step,
      removed: false
    };
  }
  function getOpSampleRate(op) {
    return op.op === "add" ? op.sampleRate : void 0;
  }
  function mergeTrackState(base, override) {
    if (!base && !override) {
      return null;
    }
    if (!base) {
      if (!override?.waveform || override.sampleRate == null) {
        return null;
      }
      return {
        sampleRate: override.sampleRate,
        waveform: override.waveform,
        volume: override.volume ?? 1,
        startStep: override.startStep ?? 0,
        removed: override.removed ?? false
      };
    }
    if (!override) {
      return base;
    }
    return {
      sampleRate: override.sampleRate ?? base.sampleRate,
      waveform: override.waveform ?? base.waveform,
      volume: override.volume ?? base.volume,
      startStep: override.startStep ?? base.startStep,
      removed: override.removed ?? base.removed
    };
  }
  var AudioRuntime = class {
    constructor(getTransportStep) {
      this.getTransportStep = getTransportStep;
      this.ctx = null;
      this.timelineTracks = /* @__PURE__ */ new Map();
      this.liveOverrides = /* @__PURE__ */ new Map();
      this.runtimeTracks = /* @__PURE__ */ new Map();
      this.playing = false;
      this.currentStep = 0;
      this.fps = 30;
      this.baseFps = 30;
      this.nextSourceToken = 1;
    }
    ensureContext() {
      if (!this.ctx) {
        const AudioContextClass = getWindow().AudioContext || getWindow().webkitAudioContext;
        this.ctx = AudioContextClass ? new AudioContextClass() : null;
      }
      return this.ctx;
    }
    getPlaybackStep() {
      return this.playing ? this.getTransportStep() : this.currentStep;
    }
    getTrackNames() {
      return /* @__PURE__ */ new Set([
        ...this.timelineTracks.keys(),
        ...this.liveOverrides.keys(),
        ...this.runtimeTracks.keys()
      ]);
    }
    getEffectiveTrack(name) {
      return mergeTrackState(this.timelineTracks.get(name), this.liveOverrides.get(name));
    }
    getRuntimeTrack(name) {
      const runtimeTrack = this.runtimeTracks.get(name);
      if (runtimeTrack) {
        return runtimeTrack;
      }
      const created = {
        source: null,
        gain: null,
        buffer: null,
        bufferWaveform: null,
        bufferSampleRate: null,
        token: 0
      };
      this.runtimeTracks.set(name, created);
      return created;
    }
    setBaseFps(baseFps) {
      this.baseFps = Math.max(1e-6, baseFps || this.baseFps || 30);
    }
    buildBuffer(track, runtimeTrack) {
      const ctx = this.ensureContext();
      if (!ctx) {
        return null;
      }
      if (runtimeTrack.buffer && runtimeTrack.bufferWaveform === track.waveform && runtimeTrack.bufferSampleRate === track.sampleRate) {
        return runtimeTrack.buffer;
      }
      const buffer = ctx.createBuffer(1, track.waveform.length, track.sampleRate);
      buffer.copyToChannel(track.waveform, 0);
      runtimeTrack.buffer = buffer;
      runtimeTrack.bufferWaveform = track.waveform;
      runtimeTrack.bufferSampleRate = track.sampleRate;
      return buffer;
    }
    applyOp(target, step, op, partial) {
      const next = target || (partial ? {} : makeTrackState(step, getOpSampleRate(op)));
      let effect = "none";
      switch (op.op) {
        case "add":
          next.sampleRate = op.sampleRate;
          next.waveform = samplesToFloat32(decodeAudioArray(op.waveform));
          next.volume = op.volume;
          next.startStep = step;
          next.removed = false;
          effect = "reschedule";
          break;
        case "set_volume":
          next.volume = op.volume;
          effect = "volume";
          break;
        case "set_waveform":
          next.waveform = samplesToFloat32(decodeAudioArray(op.waveform));
          next.removed = false;
          effect = "reschedule";
          break;
        case "append": {
          const head = next.waveform || new Float32Array(0);
          const tail = samplesToFloat32(decodeAudioArray(op.waveform));
          const merged = new Float32Array(head.length + tail.length);
          merged.set(head, 0);
          merged.set(tail, head.length);
          next.waveform = merged;
          effect = "reschedule";
          break;
        }
        case "remove":
          next.removed = true;
          effect = "reschedule";
          break;
      }
      return { track: next, effect };
    }
    applyOps(targetMap, step, ops, eventName, partial) {
      for (const op of ops) {
        const current = targetMap.get(op.name);
        const target = partial || current && "waveform" in current && current.waveform ? current || null : makeTrackState(step, getOpSampleRate(op));
        const result = this.applyOp(target, step, op, partial);
        targetMap.set(op.name, result.track);
        debugState.push(eventName, {
          name: op.name,
          step,
          op: op.op,
          effect: result.effect
        });
        if (result.effect === "volume") {
          this.updateTrackVolume(op.name);
        } else if (result.effect === "reschedule") {
          this.reconcileTrack(op.name);
        }
      }
    }
    applyTimelineOps(step, ops) {
      this.applyOps(
        this.timelineTracks,
        step,
        ops,
        "audio.timeline_op",
        false
      );
    }
    applyLiveOps(step, ops) {
      this.applyOps(
        this.liveOverrides,
        step,
        ops,
        "audio.live_op",
        true
      );
    }
    updateTrackVolume(name) {
      const runtimeTrack = this.runtimeTracks.get(name);
      const effective = this.getEffectiveTrack(name);
      if (runtimeTrack?.gain && effective) {
        runtimeTrack.gain.gain.value = effective.volume;
      }
    }
    stopRuntimeTrack(runtimeTrack) {
      runtimeTrack.token += 1;
      if (runtimeTrack.source) {
        try {
          runtimeTrack.source.stop();
        } catch {
        }
        runtimeTrack.source.disconnect();
        runtimeTrack.source = null;
      }
      if (runtimeTrack.gain) {
        runtimeTrack.gain.disconnect();
        runtimeTrack.gain = null;
      }
    }
    stopAllNodes() {
      for (const runtimeTrack of this.runtimeTracks.values()) {
        this.stopRuntimeTrack(runtimeTrack);
      }
    }
    getClipDurationSteps(track) {
      return track.waveform.length / track.sampleRate * this.baseFps;
    }
    isTrackActiveAtStep(track, playbackStep) {
      return playbackStep >= track.startStep && playbackStep < track.startStep + this.getClipDurationSteps(track);
    }
    reconcileTrack(name) {
      const ctx = this.ensureContext();
      if (!ctx) {
        return;
      }
      const runtimeTrack = this.getRuntimeTrack(name);
      this.stopRuntimeTrack(runtimeTrack);
      if (!this.playing) {
        return;
      }
      if (typeof ctx.resume === "function" && ctx.state === "suspended") {
        ctx.resume().catch(() => {
        });
      }
      const track = this.getEffectiveTrack(name);
      if (!track || track.removed || !track.waveform.length) {
        return;
      }
      const playbackStep = this.getPlaybackStep();
      const endStep = track.startStep + this.getClipDurationSteps(track);
      if (playbackStep >= endStep) {
        return;
      }
      const source = ctx.createBufferSource();
      const gain = ctx.createGain();
      source.buffer = this.buildBuffer(track, runtimeTrack);
      if (!source.buffer) {
        return;
      }
      gain.gain.value = track.volume;
      source.playbackRate.value = this.fps / this.baseFps;
      source.connect(gain);
      gain.connect(ctx.destination);
      const token = ++this.nextSourceToken;
      runtimeTrack.token = token;
      source.onended = () => {
        if (runtimeTrack.token !== token) {
          return;
        }
        runtimeTrack.source = null;
        if (runtimeTrack.gain === gain) {
          runtimeTrack.gain.disconnect();
          runtimeTrack.gain = null;
        }
        const effective = this.getEffectiveTrack(name);
        if (this.playing && effective && !effective.removed && effective.waveform.length && this.isTrackActiveAtStep(effective, this.getPlaybackStep())) {
          this.reconcileTrack(name);
        }
      };
      source.start(
        ctx.currentTime + Math.max(0, (track.startStep - playbackStep) / this.fps),
        Math.max(0, (playbackStep - track.startStep) / this.baseFps)
      );
      runtimeTrack.source = source;
      runtimeTrack.gain = gain;
    }
    rescheduleAll() {
      for (const name of this.getTrackNames()) {
        this.reconcileTrack(name);
      }
    }
    play(step, fps) {
      this.currentStep = step;
      this.fps = fps;
      this.playing = true;
      this.rescheduleAll();
    }
    pause(step, fps) {
      this.currentStep = step;
      this.fps = fps;
      this.playing = false;
      this.stopAllNodes();
    }
    seek(step, fps, playing) {
      this.currentStep = step;
      this.fps = fps;
      this.playing = playing;
      if (playing) {
        this.rescheduleAll();
        return;
      }
      this.stopAllNodes();
    }
    setFps(step, fps, playing) {
      this.currentStep = step;
      this.fps = fps;
      if (playing) {
        this.playing = true;
        this.rescheduleAll();
        return;
      }
      this.playing = false;
      this.stopAllNodes();
    }
    resetTimeline() {
      this.stopAllNodes();
      this.timelineTracks.clear();
      this.currentStep = 0;
    }
  };
  var TimelineRuntime = class {
    constructor() {
      this.sceneSteps = [];
      this.appliedStep = -1;
      this.debug = debugState;
      this.viewer = null;
      this.config = {
        numSteps: 1,
        fps: 30,
        baseFps: null,
        loop: false,
        timestepSyncUuid: null
      };
      this.audioSteps = [];
      this.timelineNodeNames = /* @__PURE__ */ new Set();
      this.baselineByName = /* @__PURE__ */ new Map();
      this.currentStep = 0;
      this.playStartStep = 0;
      this.playStartPerfTime = 0;
      this.playing = false;
      this.rafId = null;
      this.lastSyncedStep = -1;
      this.lastSyncSentAt = 0;
      this.syncIntervalMs = 250;
      this.audio = new AudioRuntime(() => this.getTransportStep());
    }
    getViewer() {
      if (!this.viewer) {
        this.viewer = findViewer();
      }
      return this.viewer;
    }
    pushMessages(messages) {
      this.getViewer()?.mutable.current.messageQueue.push(...messages);
    }
    sendGuiUpdate(uuid, value) {
      this.getViewer()?.mutable.current.sendMessage({
        type: "GuiUpdateMessage",
        uuid,
        updates: { value }
      });
    }
    ensureSceneStep(step) {
      const bucket = this.sceneSteps[step];
      if (bucket) {
        return bucket;
      }
      const created = [];
      this.sceneSteps[step] = created;
      return created;
    }
    ensureAudioStep(step) {
      const bucket = this.audioSteps[step];
      if (bucket) {
        return bucket;
      }
      const created = [];
      this.audioSteps[step] = created;
      return created;
    }
    anchorTransport(step, timestamp = performance.now()) {
      this.currentStep = step;
      this.playStartStep = step;
      this.playStartPerfTime = timestamp;
    }
    syncAudioTransport() {
      this.audio.seek(this.currentStep, this.config.fps, this.playing);
    }
    getTransportStep(timestamp = performance.now()) {
      if (!this.playing) {
        return this.currentStep;
      }
      return this.playStartStep + (timestamp - this.playStartPerfTime) / 1e3 * this.config.fps;
    }
    configure(config) {
      this.config = { ...this.config, ...config };
      if (!this.config.baseFps) {
        this.config.baseFps = this.config.fps;
      }
      this.audio.setBaseFps(this.config.baseFps);
      while (this.sceneSteps.length < this.config.numSteps) {
        this.sceneSteps.push([]);
      }
      while (this.audioSteps.length < this.config.numSteps) {
        this.audioSteps.push([]);
      }
      debugState.push("runtime.configure", this.config);
      this.syncAudioTransport();
    }
    setBaseline(payload) {
      this.baselineByName.set(payload.name, payload.messages.map(reviveMessage));
      this.timelineNodeNames.add(payload.name);
    }
    preloadSceneStep(payload) {
      this.sceneSteps[payload.step] = this.ensureSceneStep(payload.step).concat(
        payload.messages.map(reviveMessage)
      );
      for (const name of payload.nodeNames || []) {
        this.timelineNodeNames.add(name);
      }
    }
    preloadAudioStep(payload) {
      this.audioSteps[payload.step] = this.ensureAudioStep(payload.step).concat(payload.ops);
    }
    applyAudioUpdate(op) {
      debugState.push("runtime.apply_audio_update", {
        op: op.op,
        name: op.name,
        step: Math.floor(this.currentStep)
      });
      this.audio.applyLiveOps(Math.floor(this.currentStep), [op]);
    }
    syncTimestepToServer(step, force = false) {
      if (!this.config.timestepSyncUuid) {
        return;
      }
      const now = performance.now();
      if (!force && step === this.lastSyncedStep) {
        return;
      }
      if (!force && this.playing && now - this.lastSyncSentAt < this.syncIntervalMs) {
        return;
      }
      this.lastSyncedStep = step;
      this.lastSyncSentAt = now;
      this.sendGuiUpdate(this.config.timestepSyncUuid, step);
    }
    resetTimelineState() {
      debugState.push("runtime.reset_timeline_state", {
        currentStep: this.currentStep,
        appliedStep: this.appliedStep,
        playing: this.playing
      });
      this.pushMessages(
        Array.from(this.timelineNodeNames).map((name) => ({
          type: "RemoveSceneNodeMessage",
          name
        }))
      );
      for (const [name, messages] of this.baselineByName.entries()) {
        this.timelineNodeNames.add(name);
        this.pushMessages(messages);
      }
      this.audio.resetTimeline();
      this.appliedStep = -1;
    }
    applyThrough(step) {
      if (step < this.appliedStep) {
        this.resetTimelineState();
      }
      for (let index = this.appliedStep + 1; index <= step; index += 1) {
        const sceneMessages = this.sceneSteps[index];
        if (sceneMessages?.length) {
          this.pushMessages(sceneMessages);
        }
        const audioOps = this.audioSteps[index];
        if (audioOps?.length) {
          this.audio.applyTimelineOps(index, audioOps);
        }
      }
      this.appliedStep = step;
    }
    seek(payload) {
      const step = Math.max(0, Math.min(this.config.numSteps - 1, payload.step));
      this.currentStep = step;
      if (this.playing) {
        this.anchorTransport(step);
      }
      this.applyThrough(step);
      this.audio.seek(step, this.config.fps, this.playing);
      this.syncTimestepToServer(step, true);
    }
    tick(timestamp) {
      if (!this.playing) {
        return;
      }
      const next = this.getTransportStep(timestamp);
      if (next >= this.config.numSteps) {
        if (!this.config.loop) {
          this.currentStep = this.config.numSteps - 1;
          this.playing = false;
          this.audio.pause(this.currentStep, this.config.fps);
          this.syncTimestepToServer(Math.floor(this.currentStep), true);
          return;
        }
        this.anchorTransport(0, timestamp);
        this.resetTimelineState();
        this.applyThrough(0);
        this.audio.play(0, this.config.fps);
      } else {
        this.currentStep = next;
        this.applyThrough(Math.floor(this.currentStep));
      }
      this.syncTimestepToServer(Math.floor(this.currentStep));
      this.rafId = getWindow().requestAnimationFrame((nextTimestamp) => this.tick(nextTimestamp));
    }
    play(payload) {
      const step = this.getTransportStep();
      this.config.fps = payload.fps;
      this.config.loop = payload.loop;
      this.playing = true;
      this.anchorTransport(step);
      this.audio.play(step, this.config.fps);
      if (this.rafId !== null) {
        getWindow().cancelAnimationFrame(this.rafId);
      }
      this.rafId = getWindow().requestAnimationFrame((timestamp) => this.tick(timestamp));
    }
    setFps(payload) {
      const step = this.getTransportStep();
      this.config.fps = payload.fps;
      this.config.loop = payload.loop;
      this.anchorTransport(step);
      this.audio.setFps(step, this.config.fps, this.playing);
    }
    pause() {
      const step = this.getTransportStep();
      this.currentStep = step;
      this.playing = false;
      if (this.rafId !== null) {
        getWindow().cancelAnimationFrame(this.rafId);
        this.rafId = null;
      }
      this.audio.pause(step, this.config.fps);
      this.syncTimestepToServer(Math.floor(this.currentStep), true);
    }
  };

  // src/viser4d/runtime-src/index.ts
  var windowRef = window;
  if (!windowRef.__VISER4D__) {
    windowRef.__VISER4D__ = new TimelineRuntime();
  }
})();
