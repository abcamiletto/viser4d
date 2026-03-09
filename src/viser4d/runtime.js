"use strict";
(() => {
  // src/viser4d/client/binary.ts
  function decodeBase64Bytes(base64Text) {
    const binary = atob(base64Text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
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
  function decodeAudioWaveform(payload) {
    const flat = samplesToFloat32(decodeAudioArray(payload));
    const channels = [];
    for (let channel = 0; channel < payload.numChannels; channel += 1) {
      const out = new Float32Array(payload.numFrames);
      for (let frame = 0; frame < payload.numFrames; frame += 1) {
        out[frame] = flat[frame * payload.numChannels + channel] ?? 0;
      }
      channels.push(out);
    }
    return channels;
  }

  // src/viser4d/client/bridge/protocol.ts
  function isObjectRecord(value) {
    return !!value && typeof value === "object";
  }
  function isViewerLike(value) {
    return isObjectRecord(value) && isObjectRecord(value.mutable) && "useSceneTree" in value;
  }
  function getReactRoot() {
    const root = document.getElementById("root");
    if (!root) {
      throw new Error("[viser4d] Could not find #root while locating the viewer.");
    }
    const rootRecord = root;
    if (!isObjectRecord(rootRecord)) {
      throw new Error("[viser4d] React root is not an object while locating the viewer.");
    }
    const containerKey = Object.keys(rootRecord).find(
      (key) => key.startsWith("__reactContainer$")
    );
    const reactRoot = containerKey ? rootRecord[containerKey] : null;
    if (!isObjectRecord(reactRoot)) {
      throw new Error("[viser4d] Could not find the React container while locating the viewer.");
    }
    return reactRoot;
  }
  function findFiber(predicate) {
    const seen = /* @__PURE__ */ new Set();
    const stack = [getReactRoot()];
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
  function getWindow() {
    return window;
  }
  function findViewer() {
    const fiber = findFiber((candidate) => isViewerLike(candidate.memoizedProps?.value));
    if (fiber) {
      return fiber.memoizedProps.value;
    }
    throw new Error("[viser4d] Could not locate the viewer in the React fiber tree.");
  }
  function findPlaybackTimeSlider() {
    return document.querySelector("[role='slider'][aria-valuenow]");
  }

  // src/viser4d/client/audio/runtime.ts
  function makeTrackState(step, sampleRate = 44100) {
    return {
      channels: 1,
      sampleRate,
      waveform: [new Float32Array(0)],
      volume: 1,
      startStep: step,
      removed: false
    };
  }
  function getMessageSampleRate(message) {
    return message.type === "AddAudioMessage" ? message.sampleRate : void 0;
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
        channels: override.channels ?? override.waveform.length,
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
      channels: override.channels ?? base.channels,
      sampleRate: override.sampleRate ?? base.sampleRate,
      waveform: override.waveform ?? base.waveform,
      volume: override.volume ?? base.volume,
      startStep: override.startStep ?? base.startStep,
      removed: override.removed ?? base.removed
    };
  }
  function appendWaveforms(head, tail) {
    return head.map((samples, channel) => {
      const merged = new Float32Array(samples.length + (tail[channel]?.length ?? 0));
      merged.set(samples, 0);
      merged.set(tail[channel] ?? new Float32Array(0), samples.length);
      return merged;
    });
  }
  function trackFrameCount(track) {
    return track.waveform[0]?.length ?? 0;
  }
  var AudioRuntime = class {
    constructor(getTransportStep, debugPush) {
      this.getTransportStep = getTransportStep;
      this.debugPush = debugPush;
      this.ctx = null;
      this.timelineTracks = /* @__PURE__ */ new Map();
      this.liveOverrides = /* @__PURE__ */ new Map();
      this.runtimeTracks = /* @__PURE__ */ new Map();
      this.playing = false;
      this.currentStep = 0;
      this.fps = 30;
      this.stepRate = 30;
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
      const timelineTrack = this.timelineTracks.get(name);
      const liveOverride = this.liveOverrides.get(name);
      return mergeTrackState(timelineTrack, liveOverride);
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
    setStepRate(stepRate) {
      this.stepRate = Math.max(1e-6, stepRate || this.stepRate || 30);
    }
    buildBuffer(track, runtimeTrack) {
      const ctx = this.ensureContext();
      if (!ctx) {
        return null;
      }
      if (runtimeTrack.buffer && runtimeTrack.bufferWaveform === track.waveform && runtimeTrack.bufferSampleRate === track.sampleRate) {
        return runtimeTrack.buffer;
      }
      const buffer = ctx.createBuffer(track.channels, trackFrameCount(track), track.sampleRate);
      for (let channel = 0; channel < track.channels; channel += 1) {
        buffer.copyToChannel(
          new Float32Array(track.waveform[channel] ?? new Float32Array(0)),
          channel
        );
      }
      runtimeTrack.buffer = buffer;
      runtimeTrack.bufferWaveform = track.waveform;
      runtimeTrack.bufferSampleRate = track.sampleRate;
      return buffer;
    }
    applyOp(target, step, message, partial) {
      const next = target ?? (partial ? {} : makeTrackState(step, getMessageSampleRate(message)));
      let effect = "none";
      switch (message.type) {
        case "AddAudioMessage":
          next.sampleRate = message.sampleRate;
          next.channels = message.waveform.numChannels;
          next.waveform = decodeAudioWaveform(message.waveform);
          next.volume = message.volume;
          next.startStep = step;
          next.removed = false;
          effect = "reschedule";
          break;
        case "SetAudioVolumeMessage":
          next.volume = message.volume;
          effect = "volume";
          break;
        case "SetAudioWaveformMessage":
          next.channels = message.waveform.numChannels;
          next.waveform = decodeAudioWaveform(message.waveform);
          next.removed = false;
          effect = "reschedule";
          break;
        case "AppendAudioMessage": {
          const tail = decodeAudioWaveform(message.waveform);
          next.channels = message.waveform.numChannels;
          next.waveform = next.waveform ? appendWaveforms(next.waveform, tail) : tail;
          effect = "reschedule";
          break;
        }
        case "RemoveAudioMessage":
          next.removed = true;
          effect = "reschedule";
          break;
      }
      return { track: next, effect };
    }
    applyOps(targetMap, step, messages, eventName, partial) {
      for (const message of messages) {
        const current = targetMap.get(message.name);
        const hasWaveform = current && "waveform" in current && current.waveform;
        const target = partial || hasWaveform ? current || null : makeTrackState(step, getMessageSampleRate(message));
        const result = this.applyOp(target, step, message, partial);
        targetMap.set(message.name, result.track);
        this.debugPush(eventName, {
          name: message.name,
          step,
          type: message.type,
          effect: result.effect
        });
        if (result.effect === "volume") {
          this.updateTrackVolume(message.name);
        } else if (result.effect === "reschedule") {
          this.reconcileTrack(message.name);
        }
      }
    }
    applyTimelineMessages(step, messages) {
      this.applyOps(
        this.timelineTracks,
        step,
        messages,
        "audio.timeline_message",
        false
      );
    }
    applyLiveMessages(step, messages) {
      this.applyOps(
        this.liveOverrides,
        step,
        messages,
        "audio.live_message",
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
        } catch (error) {
          this.debugPush("audio.stop_error", {
            error: error instanceof Error ? error.message : String(error)
          });
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
      return trackFrameCount(track) / track.sampleRate * this.stepRate;
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
      if (!track || track.removed || !trackFrameCount(track)) {
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
      source.playbackRate.value = this.fps / this.stepRate;
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
        if (this.playing && effective && !effective.removed && trackFrameCount(effective) && this.isTrackActiveAtStep(effective, this.getPlaybackStep())) {
          this.reconcileTrack(name);
        }
      };
      source.start(
        ctx.currentTime + Math.max(0, (track.startStep - playbackStep) / this.fps),
        Math.max(0, (playbackStep - track.startStep) / this.stepRate)
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

  // src/viser4d/client/audio/messages.ts
  function isAudioMessage(message) {
    return message.type === "AddAudioMessage" || message.type === "SetAudioVolumeMessage" || message.type === "SetAudioWaveformMessage" || message.type === "AppendAudioMessage" || message.type === "RemoveAudioMessage";
  }

  // src/viser4d/client/bridge/runtime.ts
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
  var TimelineRuntime = class {
    constructor() {
      this.stepMessages = [];
      this.appliedStep = -1;
      this.debug = debugState;
      this.viewer = null;
      this.playbackTimeSlider = null;
      this.config = {
        numSteps: 1,
        fps: 30,
        baseFps: null,
        loop: false,
        timestepSyncUuid: null
      };
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
      this.audio = new AudioRuntime(
        () => this.getTransportStep(),
        (event, payload) => debugState.push(event, payload)
      );
      this.playbackAudio = new AudioRuntime(
        () => this.playbackTime,
        (event, payload) => debugState.push(event, payload)
      );
      this.playbackTime = 0;
      this.playbackPlaying = false;
      this.playbackObserved = false;
      this.playbackLastAppliedMessageTime = -1;
      this.interceptorInstalled = false;
      this.playbackMonitorId = null;
      this.playbackAudio.setStepRate(1);
      this.installWhenReady();
    }
    getViewer() {
      if (!this.viewer) {
        this.viewer = findViewer();
      }
      return this.viewer;
    }
    getPlaybackTimeSlider() {
      if (!this.playbackTimeSlider || !this.playbackTimeSlider.isConnected) {
        this.playbackTimeSlider = findPlaybackTimeSlider();
      }
      return this.playbackTimeSlider;
    }
    installWhenReady() {
      if (this.interceptorInstalled) {
        return;
      }
      try {
        this.installMessageQueueInterceptor();
        if (this.getViewer().messageSource !== "websocket") {
          this.startPlaybackMonitor();
        }
      } catch {
        getWindow().requestAnimationFrame(() => this.installWhenReady());
      }
    }
    installMessageQueueInterceptor() {
      if (this.interceptorInstalled) {
        return;
      }
      const queue = this.getViewer().mutable.current.messageQueue;
      const originalPush = queue.push.bind(queue);
      queue.push = (...messages) => {
        const forwarded = [];
        for (const message of messages) {
          if (this.handleQueuedMessage(message)) {
            continue;
          }
          forwarded.push(message);
        }
        return originalPush(...forwarded);
      };
      this.interceptorInstalled = true;
    }
    handleQueuedMessage(message) {
      if (!isAudioMessage(message)) {
        return false;
      }
      if (this.getViewer().messageSource === "websocket") {
        return false;
      }
      const playbackTime = getPlaybackMessageTime(message);
      if (playbackTime !== null && playbackTime < this.playbackLastAppliedMessageTime) {
        this.resetPlaybackAudio();
      }
      if (playbackTime !== null) {
        this.playbackLastAppliedMessageTime = playbackTime;
        this.playbackAudio.applyLiveMessages(playbackTime, [message]);
      } else {
        this.playbackAudio.applyLiveMessages(this.playbackTime, [message]);
      }
      return true;
    }
    startPlaybackMonitor() {
      if (this.playbackMonitorId !== null) {
        return;
      }
      const tick = () => {
        this.syncPlaybackState();
        this.playbackMonitorId = getWindow().requestAnimationFrame(tick);
      };
      this.playbackMonitorId = getWindow().requestAnimationFrame(tick);
    }
    syncPlaybackState() {
      const nextTime = this.readPlaybackTime();
      if (nextTime === null) {
        return;
      }
      const delta = nextTime - this.playbackTime;
      const jumped = Math.abs(delta) > 0.2;
      const playing = delta > 1e-4;
      if (!this.playbackObserved) {
        this.playbackObserved = true;
        this.playbackTime = nextTime;
        this.playbackAudio.seek(nextTime, 1, false);
        return;
      }
      if (delta < -1e-4) {
        this.resetPlaybackAudio();
      }
      if (jumped) {
        this.playbackAudio.seek(nextTime, 1, playing);
      } else if (playing !== this.playbackPlaying) {
        if (playing) {
          this.playbackAudio.play(nextTime, 1);
        } else {
          this.playbackAudio.pause(nextTime, 1);
        }
      } else if (!playing && Math.abs(delta) > 1e-4) {
        this.playbackAudio.seek(nextTime, 1, false);
      }
      this.playbackTime = nextTime;
      this.playbackPlaying = playing;
    }
    resetPlaybackAudio() {
      this.playbackAudio.resetTimeline();
      this.playbackLastAppliedMessageTime = -1;
    }
    readPlaybackTime() {
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
    pushMessages(messages) {
      this.getViewer().mutable.current.messageQueue.push(...messages);
    }
    sendGuiUpdate(uuid, value) {
      this.getViewer().mutable.current.sendMessage({
        type: "GuiUpdateMessage",
        uuid,
        updates: { value }
      });
    }
    ensureStep(step) {
      const bucket = this.stepMessages[step];
      if (bucket) {
        return bucket;
      }
      const created = [];
      this.stepMessages[step] = created;
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
    applyStepMessages(step, messages) {
      const sceneMessages = [];
      const audioMessages = [];
      for (const message of messages) {
        if (isAudioMessage(message)) {
          audioMessages.push(message);
        } else {
          sceneMessages.push(message);
        }
      }
      if (sceneMessages.length) {
        this.pushMessages(sceneMessages);
      }
      if (audioMessages.length) {
        this.audio.applyTimelineMessages(step, audioMessages);
      }
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
      this.audio.setStepRate(this.config.baseFps);
      while (this.stepMessages.length < this.config.numSteps) {
        this.stepMessages.push([]);
      }
      debugState.push("runtime.configure", this.config);
      this.syncAudioTransport();
    }
    setBaseline(payload) {
      this.baselineByName.set(payload.name, payload.messages);
      this.timelineNodeNames.add(payload.name);
    }
    preloadStep(payload) {
      this.stepMessages[payload.step] = this.ensureStep(payload.step).concat(
        payload.messages
      );
      for (const name of payload.nodeNames || []) {
        this.timelineNodeNames.add(name);
      }
    }
    applyMessageUpdate(message) {
      const name = typeof message.name === "string" ? message.name : null;
      debugState.push("runtime.apply_message_update", {
        type: message.type,
        name,
        step: Math.floor(this.currentStep)
      });
      if (isAudioMessage(message)) {
        this.audio.applyLiveMessages(Math.floor(this.currentStep), [message]);
        return;
      }
      this.pushMessages([message]);
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
        const messages = this.stepMessages[index];
        if (messages?.length) {
          this.applyStepMessages(index, messages);
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
      this.rafId = getWindow().requestAnimationFrame(
        (nextTimestamp) => this.tick(nextTimestamp)
      );
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
      this.rafId = getWindow().requestAnimationFrame(
        (timestamp) => this.tick(timestamp)
      );
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
  function getPlaybackMessageTime(message) {
    const playbackTime = message.__viserPlaybackTime;
    return typeof playbackTime === "number" ? playbackTime : null;
  }

  // src/viser4d/client/index.ts
  var windowRef = window;
  if (!windowRef.__VISER4D__) {
    windowRef.__VISER4D__ = new TimelineRuntime();
  }
})();
