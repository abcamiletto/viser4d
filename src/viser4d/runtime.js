(function () {
  if (window.__VISER4D__) {
    return;
  }

  function revive(value) {
    if (Array.isArray(value)) {
      return value.map(revive);
    }
    if (!value || typeof value !== "object") {
      return value;
    }
    if (value.__viser4d_binary__) {
      return decodeBase64Bytes(value.__viser4d_binary__);
    }
    const out = {};
    for (const [key, inner] of Object.entries(value)) {
      out[key] = revive(inner);
    }
    return out;
  }

  function decodeBase64Bytes(base64Text) {
    const binary = atob(base64Text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  const debugState = {
    enabled: false,
    logs: [],
    maxLogs: 400,
    push(event, payload) {
      const entry = {
        time: Number(performance.now().toFixed(1)),
        event,
        payload,
      };
      this.logs.push(entry);
      if (this.logs.length > this.maxLogs) {
        this.logs.shift();
      }
      if (this.enabled) {
        console.debug("[viser4d]", event, payload);
      }
      return entry;
    },
    clear() {
      this.logs.length = 0;
    },
    setEnabled(enabled) {
      this.enabled = !!enabled;
    },
  };

  class ViewerAdapter {
    constructor() {
      this.viewer = null;
    }

    getViewer() {
      if (!this.viewer) {
        this.viewer = this.findViewer();
      }
      return this.viewer;
    }

    pushMessages(messages) {
      const viewer = this.getViewer();
      if (!viewer) {
        return false;
      }
      viewer.mutable.current.messageQueue.push(...messages);
      return true;
    }

    sendGuiUpdate(uuid, updates) {
      const viewer = this.getViewer();
      if (!viewer) {
        return false;
      }
      viewer.mutable.current.sendMessage({
        type: "GuiUpdateMessage",
        uuid,
        updates,
      });
      return true;
    }

    findViewer() {
      const reactRoot = this.findReactRoot();
      if (!reactRoot) {
        return null;
      }
      const seen = new Set();
      const stack = [reactRoot];
      while (stack.length) {
        const fiber = stack.pop();
        if (!fiber || seen.has(fiber)) {
          continue;
        }
        seen.add(fiber);
        const candidate = fiber.memoizedProps?.value;
        if (candidate?.mutable && candidate?.useSceneTree) {
          return candidate;
        }
        stack.push(fiber.child, fiber.sibling);
      }
      return null;
    }

    findReactRoot() {
      const root = document.getElementById("root");
      if (!root) {
        return null;
      }
      const containerKey = Object.keys(root).find((key) =>
        key.startsWith("__reactContainer$"),
      );
      return containerKey ? root[containerKey] : null;
    }
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
        out[i] = samples[i] / 32768;
      }
      return out;
    }
    if (samples instanceof Int32Array) {
      const out = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i += 1) {
        out[i] = samples[i] / 2147483648;
      }
      return out;
    }
    if (samples instanceof Uint8Array) {
      const out = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i += 1) {
        out[i] = (samples[i] - 128) / 128;
      }
      return out;
    }
    return Float32Array.from(samples);
  }

  function makeTrackState(step, sampleRate) {
    return {
      sampleRate: sampleRate || 44100,
      waveform: new Float32Array(0),
      volume: 1.0,
      startStep: step,
      removed: false,
    };
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
        volume: override.volume ?? 1.0,
        startStep: override.startStep ?? 0,
        removed: !!override.removed,
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
      removed: override.removed ?? base.removed,
    };
  }

  class AudioRuntime {
    constructor(getTransportStep) {
      this.getTransportStep = getTransportStep;
      this.ctx = null;
      this.timelineTracks = new Map();
      this.liveOverrides = new Map();
      this.runtimeTracks = new Map();
      this.playing = false;
      this.currentStep = 0;
      this.fps = 30;
      this.baseFps = 30;
      this.loop = true;
      this.nextSourceToken = 1;
    }

    ensureContext() {
      if (!this.ctx) {
        const AudioContextCls = window.AudioContext || window.webkitAudioContext;
        this.ctx = AudioContextCls ? new AudioContextCls() : null;
      }
      return this.ctx;
    }

    getTrackNames() {
      return new Set([...this.timelineTracks.keys(), ...this.liveOverrides.keys(), ...this.runtimeTracks.keys()]);
    }

    getPlaybackStep() {
      return this.playing ? this.getTransportStep() : this.currentStep;
    }

    setBaseFps(baseFps) {
      this.baseFps = Math.max(1e-6, baseFps || this.baseFps || 30);
    }

    getClipDurationSteps(track) {
      return (track.waveform.length / track.sampleRate) * this.baseFps;
    }

    getClipOffsetSeconds(track, playbackStep) {
      return Math.max(0, (playbackStep - track.startStep) / this.baseFps);
    }

    isTrackActiveAtStep(track, playbackStep) {
      return (
        playbackStep >= track.startStep &&
        playbackStep < track.startStep + this.getClipDurationSteps(track)
      );
    }

    getEffectiveTrack(name) {
      return mergeTrackState(this.timelineTracks.get(name), this.liveOverrides.get(name));
    }

    getRuntimeTrack(name) {
      const existing = this.runtimeTracks.get(name);
      if (existing) {
        return existing;
      }
      const created = {
        source: null,
        gain: null,
        buffer: null,
        bufferWaveform: null,
        bufferSampleRate: null,
        token: 0,
        expectedEndStep: null,
      };
      this.runtimeTracks.set(name, created);
      return created;
    }

    buildBuffer(track, runtimeTrack) {
      const ctx = this.ensureContext();
      if (!ctx) {
        return null;
      }
      if (
        runtimeTrack.buffer &&
        runtimeTrack.bufferWaveform === track.waveform &&
        runtimeTrack.bufferSampleRate === track.sampleRate
      ) {
        return runtimeTrack.buffer;
      }
      const buffer = ctx.createBuffer(1, track.waveform.length, track.sampleRate);
      buffer.copyToChannel(track.waveform, 0);
      runtimeTrack.buffer = buffer;
      runtimeTrack.bufferWaveform = track.waveform;
      runtimeTrack.bufferSampleRate = track.sampleRate;
      return buffer;
    }

    applyOp(target, step, op, partial = false) {
      const next = target || (partial ? {} : makeTrackState(step, op.sampleRate));
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
        default:
          break;
      }
      return { track: next, effect };
    }

    applyOps(targetMap, step, ops, eventName, useOverrideState = false) {
      for (const op of ops) {
        const current = targetMap.get(op.name);
        const target = useOverrideState
          ? current || null
          : current && current.waveform
            ? current
            : makeTrackState(step, op.sampleRate);
        const result = this.applyOp(target, step, op, useOverrideState);
        targetMap.set(op.name, result.track);
        debugState.push(eventName, {
          name: op.name,
          step,
          op: op.op,
          effect: result.effect,
          volume: result.track.volume,
        });
        if (result.effect === "volume") {
          this.updateTrackVolume(op.name);
        } else if (result.effect === "reschedule") {
          this.reconcileTrack(op.name);
        }
      }
    }

    applyTimelineOps(step, ops) {
      this.applyOps(this.timelineTracks, step, ops, "audio.timeline_op");
    }

    applyLiveOps(step, ops) {
      this.applyOps(this.liveOverrides, step, ops, "audio.live_op", true);
    }

    updateTrackVolume(name) {
      const runtimeTrack = this.runtimeTracks.get(name);
      const effective = this.getEffectiveTrack(name);
      if (runtimeTrack?.gain && effective) {
        runtimeTrack.gain.gain.value = effective.volume;
      }
      debugState.push("audio.volume_applied", {
        name,
        runtimeActive: !!runtimeTrack?.gain,
        volume: effective?.volume ?? null,
      });
    }

    stopRuntimeTrack(runtimeTrack) {
      runtimeTrack.token += 1;
      if (runtimeTrack.source) {
        debugState.push("audio.stop_source", {});
        try {
          runtimeTrack.source.stop();
        } catch (_err) {}
        runtimeTrack.source.disconnect();
        runtimeTrack.source = null;
      }
      if (runtimeTrack.gain) {
        runtimeTrack.gain.disconnect();
        runtimeTrack.gain = null;
      }
      runtimeTrack.expectedEndStep = null;
    }

    stopAllNodes() {
      for (const runtimeTrack of this.runtimeTracks.values()) {
        this.stopRuntimeTrack(runtimeTrack);
      }
    }

    getTrackWindow(track) {
      const durationSteps = this.getClipDurationSteps(track);
      return {
        durationSteps,
        endStep: track.startStep + durationSteps,
      };
    }

    shouldRetuneActiveTrack(track, runtimeTrack, playbackStep) {
      if (!track || !runtimeTrack?.source) {
        return false;
      }
      return this.isTrackActiveAtStep(track, playbackStep);
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
        ctx.resume().catch(() => {});
      }
      const track = this.getEffectiveTrack(name);
      if (!track || track.removed || !track.waveform.length) {
        debugState.push("audio.skip_track", {
          name,
          reason: !track ? "missing" : track.removed ? "removed" : "empty_waveform",
        });
        return;
      }
      const playbackStep = this.getPlaybackStep();
      const { durationSteps, endStep } = this.getTrackWindow(track);
      if (playbackStep >= endStep) {
        debugState.push("audio.skip_track", {
          name,
          reason: "clip_finished",
          playbackStep,
          startStep: track.startStep,
          clipDurationSteps: durationSteps,
        });
        return;
      }
      const delaySeconds = Math.max(0, (track.startStep - playbackStep) / this.fps);
      const offsetSeconds = this.getClipOffsetSeconds(track, playbackStep);
      const source = ctx.createBufferSource();
      const gain = ctx.createGain();
      gain.gain.value = track.volume;
      source.buffer = this.buildBuffer(track, runtimeTrack);
      if (!source.buffer) {
        return;
      }
      const playbackRate = this.fps / this.baseFps;
      source.playbackRate.value = playbackRate;
      source.connect(gain);
      gain.connect(ctx.destination);
      const token = ++this.nextSourceToken;
      runtimeTrack.token = token;
      runtimeTrack.expectedEndStep = endStep;
      source.onended = () => {
        if (runtimeTrack.token !== token) {
          return;
        }
        runtimeTrack.source = null;
        if (runtimeTrack.gain === gain) {
          runtimeTrack.gain.disconnect();
          runtimeTrack.gain = null;
        }
        const playbackStepNow = this.getPlaybackStep();
        debugState.push("audio.source_ended", {
          name,
          playing: this.playing,
          currentStep: this.currentStep,
          playbackStep: playbackStepNow,
          expectedEndStep: endStep,
        });
        if (!this.playing) {
          return;
        }
        const effective = this.getEffectiveTrack(name);
        if (!effective || effective.removed || !effective.waveform.length) {
          return;
        }
        if (this.isTrackActiveAtStep(effective, playbackStepNow)) {
          debugState.push("audio.reschedule_after_unexpected_end", {
            name,
            playbackStep: playbackStepNow,
            expectedEndStep: endStep,
          });
          this.reconcileTrack(name);
        }
      };
      source.start(ctx.currentTime + delaySeconds, offsetSeconds);
      runtimeTrack.source = source;
      runtimeTrack.gain = gain;
      debugState.push("audio.start_source", {
        name,
        volume: track.volume,
        startStep: track.startStep,
        fps: this.fps,
        baseFps: this.baseFps,
        playbackStep,
        delaySeconds,
        offsetSeconds,
        playbackRate,
        waveformLength: track.waveform.length,
        sampleRate: track.sampleRate,
      });
    }

    rescheduleAll() {
      for (const name of this.getTrackNames()) {
        this.reconcileTrack(name);
      }
    }

    play({ step, fps, loop }) {
      this.fps = fps;
      this.loop = loop;
      this.currentStep = step;
      this.playing = true;
      debugState.push("audio.play", { step, fps, loop });
      this.rescheduleAll();
    }

    pause({ step, fps, loop }) {
      this.fps = fps;
      this.loop = loop;
      this.currentStep = step;
      this.playing = false;
      debugState.push("audio.pause", { step, fps, loop });
      this.stopAllNodes();
    }

    seek({ step, fps, loop, playing }) {
      this.fps = fps;
      this.loop = loop;
      this.currentStep = step;
      if (playing) {
        this.playing = true;
        debugState.push("audio.seek_playing", { step, fps, loop });
        this.rescheduleAll();
        return;
      }
      this.playing = false;
      debugState.push("audio.seek_paused", { step, fps, loop });
      this.stopAllNodes();
    }

    setFps({ step, fps, loop, playing }) {
      this.fps = fps;
      this.loop = loop;
      this.currentStep = step;
      if (playing) {
        this.playing = true;
        debugState.push("audio.set_fps_playing", { step, fps, loop });
        for (const name of this.getTrackNames()) {
          const track = this.getEffectiveTrack(name);
          const runtimeTrack = this.runtimeTracks.get(name);
          if (this.shouldRetuneActiveTrack(track, runtimeTrack, step)) {
            const playbackRate = this.fps / this.baseFps;
            runtimeTrack.source.playbackRate.value = playbackRate;
            if (runtimeTrack.gain) {
              runtimeTrack.gain.gain.value = track.volume;
            }
            debugState.push("audio.update_playback_rate", {
              name,
              step,
              fps: this.fps,
              baseFps: this.baseFps,
              playbackRate,
            });
            continue;
          }
          this.reconcileTrack(name);
        }
        return;
      }
      this.playing = false;
      debugState.push("audio.set_fps_paused", { step, fps, loop });
      this.stopAllNodes();
    }

    resetTimeline() {
      this.stopAllNodes();
      this.timelineTracks.clear();
      this.currentStep = 0;
      debugState.push("audio.reset_timeline", {});
    }

  }

  class TimelineRuntime {
    constructor() {
      this.adapter = new ViewerAdapter();
      this.config = {
        numSteps: 1,
        fps: 30,
        baseFps: null,
        loop: false,
        timestepSyncUuid: null,
      };
      this.sceneSteps = [];
      this.audioSteps = [];
      this.timelineNodeNames = new Set();
      this.baselineByName = new Map();
      this.currentStep = 0;
      this.playStartStep = 0;
      this.playStartPerfTime = 0;
      this.appliedStep = -1;
      this.playing = false;
      this.rafId = null;
      this.lastSyncedStep = -1;
      this.lastSyncSentAt = 0;
      this.syncIntervalMs = 250;
      this.audio = new AudioRuntime(() => this.getTransportStep());
      this.debug = debugState;
    }

    syncAudioTransport() {
      this.audio.seek({
        step: this.currentStep,
        fps: this.config.fps,
        loop: this.config.loop,
        playing: this.playing,
      });
    }

    getViewer() {
      return this.adapter.getViewer();
    }

    getTransportStep(timestamp = performance.now()) {
      if (!this.playing) {
        return this.currentStep;
      }
      return this.playStartStep + ((timestamp - this.playStartPerfTime) / 1000) * this.config.fps;
    }

    anchorTransport(step, timestamp = performance.now()) {
      this.currentStep = step;
      this.playStartStep = step;
      this.playStartPerfTime = timestamp;
    }

    configure(config) {
      this.config = { ...this.config, ...config };
      if (!this.config.baseFps) {
        this.config.baseFps = this.config.fps;
      }
      debugState.push("runtime.configure", this.config);
      this.audio.setBaseFps(this.config.baseFps);
      while (this.sceneSteps.length < this.config.numSteps) {
        this.sceneSteps.push([]);
      }
      while (this.audioSteps.length < this.config.numSteps) {
        this.audioSteps.push([]);
      }
      this.syncAudioTransport();
    }

    setBaseline(payload) {
      this.baselineByName.set(payload.name, payload.messages.map(revive));
      this.timelineNodeNames.add(payload.name);
    }

    preloadSceneStep(payload) {
      this.sceneSteps[payload.step] = this.sceneSteps[payload.step].concat(
        payload.messages.map(revive),
      );
      for (const name of payload.nodeNames || []) {
        this.timelineNodeNames.add(name);
      }
    }

    preloadAudioStep(payload) {
      this.audioSteps[payload.step] = this.audioSteps[payload.step].concat(payload.ops);
    }

    applyAudioUpdate(op) {
      debugState.push("runtime.apply_audio_update", {
        op: op.op,
        name: op.name,
        step: Math.floor(this.currentStep),
      });
      this.audio.applyLiveOps(Math.floor(this.currentStep), [op]);
    }

    pushMessages(messages) {
      return this.adapter.pushMessages(messages);
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
      this.adapter.sendGuiUpdate(this.config.timestepSyncUuid, { value: step });
    }

    resetTimelineState() {
      debugState.push("runtime.reset_timeline_state", {
        currentStep: this.currentStep,
        appliedStep: this.appliedStep,
        playing: this.playing,
      });
      const removals = Array.from(this.timelineNodeNames).map((name) => ({
        type: "RemoveSceneNodeMessage",
        name,
      }));
      this.pushMessages(removals);
      for (const [name, messages] of this.baselineByName.entries()) {
        this.timelineNodeNames.add(name);
        this.pushMessages(messages);
      }
      this.audio.resetTimeline();
      this.appliedStep = -1;
    }

    applyThrough(step) {
      if (step < this.appliedStep) {
        debugState.push("runtime.apply_through_reset", {
          targetStep: step,
          appliedStep: this.appliedStep,
          currentStep: this.currentStep,
          playing: this.playing,
        });
        this.resetTimelineState();
      }
      for (let index = this.appliedStep + 1; index <= step; index += 1) {
        if (this.sceneSteps[index] && this.sceneSteps[index].length) {
          this.pushMessages(this.sceneSteps[index]);
        }
        if (this.audioSteps[index] && this.audioSteps[index].length) {
          this.audio.applyTimelineOps(index, this.audioSteps[index]);
        }
      }
      this.appliedStep = step;
    }

    seek(payload) {
      const step = Math.max(0, Math.min(this.config.numSteps - 1, payload.step));
      debugState.push("runtime.seek", { from: this.currentStep, to: step, playing: this.playing });
      this.currentStep = step;
      if (this.playing) {
        this.anchorTransport(step);
      }
      this.applyThrough(step);
      this.audio.seek({
        step,
        fps: this.config.fps,
        loop: this.config.loop,
        playing: this.playing,
      });
      this.syncTimestepToServer(step, true);
    }

    tick(timestamp) {
      if (!this.playing) {
        return;
      }
      const next = this.getTransportStep(timestamp);
      if (next >= this.config.numSteps) {
        if (this.config.loop) {
          debugState.push("runtime.loop_reset", {
            next,
            numSteps: this.config.numSteps,
          });
          this.anchorTransport(0, timestamp);
          this.resetTimelineState();
          this.applyThrough(0);
          this.audio.play({
            step: 0,
            fps: this.config.fps,
            loop: this.config.loop,
          });
        } else {
          this.currentStep = this.config.numSteps - 1;
          this.playing = false;
          this.audio.pause({
            step: this.currentStep,
            fps: this.config.fps,
            loop: this.config.loop,
          });
          this.syncTimestepToServer(Math.floor(this.currentStep), true);
          return;
        }
      } else {
        this.currentStep = next;
        this.applyThrough(Math.floor(this.currentStep));
      }
      this.syncTimestepToServer(Math.floor(this.currentStep));
      this.rafId = window.requestAnimationFrame((nextTimestamp) => this.tick(nextTimestamp));
    }

    play(payload) {
      const step = this.getTransportStep();
      this.config.fps = payload.fps;
      this.config.loop = payload.loop;
      this.playing = true;
      this.anchorTransport(step);
      debugState.push("runtime.play", {
        step,
        fps: this.config.fps,
        loop: this.config.loop,
      });
      this.audio.play({
        step,
        fps: this.config.fps,
        loop: this.config.loop,
      });
      if (this.rafId !== null) {
        window.cancelAnimationFrame(this.rafId);
      }
      this.rafId = window.requestAnimationFrame((timestamp) => this.tick(timestamp));
    }

    setFps(payload) {
      const step = this.getTransportStep();
      this.config.fps = payload.fps;
      this.config.loop = payload.loop;
      this.anchorTransport(step);
      debugState.push("runtime.set_fps", {
        step,
        fps: this.config.fps,
        loop: this.config.loop,
        playing: this.playing,
      });
      this.audio.setFps({
        step,
        fps: this.config.fps,
        loop: this.config.loop,
        playing: this.playing,
      });
    }

    pause() {
      const step = this.getTransportStep();
      this.currentStep = step;
      this.playing = false;
      if (this.rafId !== null) {
        window.cancelAnimationFrame(this.rafId);
        this.rafId = null;
      }
      debugState.push("runtime.pause", {
        step,
        fps: this.config.fps,
        loop: this.config.loop,
      });
      this.audio.pause({
        step,
        fps: this.config.fps,
        loop: this.config.loop,
      });
      this.syncTimestepToServer(Math.floor(this.currentStep), true);
    }
  }

  const runtime = new TimelineRuntime();
  window.__VISER4D__ = runtime;
})();
