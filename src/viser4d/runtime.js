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
  function isBinaryPayload(value) {
    return typeof value.__viser4d_binary__ === "string";
  }
  function asBinaryBytes(value) {
    if (value instanceof Uint8Array) {
      return value;
    }
    if (value instanceof ArrayBuffer) {
      return new Uint8Array(value);
    }
    if (!ArrayBuffer.isView(value)) {
      return null;
    }
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  function normalizeTransportValue(value) {
    if (Array.isArray(value)) {
      return value.map((item) => normalizeTransportValue(item));
    }
    if (!value || typeof value !== "object") {
      return value;
    }
    const bytes = asBinaryBytes(value);
    if (bytes) {
      return bytes;
    }
    const record = value;
    if (isBinaryPayload(record)) {
      return decodeBase64Bytes(record.__viser4d_binary__);
    }
    const out = {};
    for (const [key, inner] of Object.entries(record)) {
      out[key] = inner === void 0 ? void 0 : normalizeTransportValue(inner);
    }
    return out;
  }
  function normalizeTransportMessage(message) {
    return normalizeTransportValue(message);
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
    return isObjectRecord(value) && isObjectRecord(value.mutable) && "useGuiConfig" in value && "guiActions" in value && "useSceneTree" in value;
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

  // src/viser4d/client/bridge/blockCache.ts
  var BlockCache = class {
    constructor(sendGuiUpdate) {
      this.sendGuiUpdate = sendGuiUpdate;
      this.blockSize = 64;
      this.blockRequestSyncUuid = null;
      this.pendingStep = null;
      this.blocks = /* @__PURE__ */ new Map();
      this.requestedBlocks = /* @__PURE__ */ new Set();
    }
    getBlock(step) {
      return this.blocks.get(Math.floor(step / this.blockSize)) ?? null;
    }
    blockIndexOf(step) {
      return Math.floor(step / this.blockSize);
    }
    blockStartStep(blockIndex) {
      return blockIndex * this.blockSize;
    }
    loadBlock(blockIndex, block) {
      this.requestedBlocks.delete(blockIndex);
      this.blocks.set(blockIndex, block);
    }
    evictBlock(blockIndex, appliedBlock) {
      if (blockIndex === appliedBlock) {
        return;
      }
      this.blocks.delete(blockIndex);
      this.requestedBlocks.delete(blockIndex);
    }
    /**
     * Returns true if the step's block is loaded. If not, records it as pending
     * and sends a request to the server.
     */
    ensureStepLoaded(step) {
      if (this.getBlock(step)) {
        return true;
      }
      this.pendingStep = step;
      const blockIndex = this.blockIndexOf(step);
      if (!this.requestedBlocks.has(blockIndex) && this.blockRequestSyncUuid) {
        this.requestedBlocks.add(blockIndex);
        this.sendGuiUpdate(this.blockRequestSyncUuid, step);
      }
      return false;
    }
  };

  // src/viser4d/client/bridge/sceneApplicator.ts
  var SceneApplicator = class {
    constructor(pushMessages, audio, blocks, renderedTimelineNodes) {
      this.pushMessages = pushMessages;
      this.audio = audio;
      this.blocks = blocks;
      this.renderedTimelineNodes = renderedTimelineNodes;
      this.appliedStep = -1;
      this.appliedBlock = -1;
    }
    applyStepMessages(step, messages) {
      const sceneMessages = [];
      const audioMessages = [];
      for (const message of messages) {
        if (isAudioMessage(message)) {
          audioMessages.push(message);
        } else {
          this.trackTimelineNode(message);
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
    resetState() {
      this.removeRenderedTimelineNodes();
      this.audio.resetTimeline();
      this.appliedStep = -1;
      this.appliedBlock = -1;
    }
    rebuildThrough(step) {
      if (!this.blocks.ensureStepLoaded(step)) {
        return;
      }
      this.resetState();
      this.applyThrough(step);
    }
    applyThrough(step) {
      const blockIndex = this.blocks.blockIndexOf(step);
      if (this.appliedStep >= 0 && (blockIndex !== this.appliedBlock || step < this.appliedStep)) {
        this.rebuildThrough(step);
        return;
      }
      this.advanceThrough(step);
    }
    advanceThrough(step) {
      if (!this.blocks.ensureStepLoaded(step)) {
        return;
      }
      if (this.appliedStep < 0) {
        const block = this.blocks.getBlock(step);
        if (!block) {
          return;
        }
        const blockIndex = this.blocks.blockIndexOf(step);
        this.applyBlockCheckpoint(blockIndex, block);
        this.appliedBlock = blockIndex;
        this.appliedStep = this.blocks.blockStartStep(blockIndex) - 1;
      }
      let nextStep = this.appliedStep + 1;
      while (nextStep <= step) {
        if (!this.blocks.ensureStepLoaded(nextStep)) {
          return;
        }
        const block = this.blocks.getBlock(nextStep);
        if (!block) {
          return;
        }
        const blockIndex = this.blocks.blockIndexOf(nextStep);
        const blockStart = this.blocks.blockStartStep(blockIndex);
        const blockEnd = Math.min(
          step,
          blockStart + block.stepMessages.length - 1
        );
        for (let index = nextStep; index <= blockEnd; index += 1) {
          const messages = block.stepMessages[index - blockStart] ?? [];
          if (messages.length) {
            this.applyStepMessages(index, messages);
          }
        }
        this.appliedBlock = blockIndex;
        this.appliedStep = blockEnd;
        nextStep = blockEnd + 1;
      }
    }
    applyBlockCheckpoint(blockIndex, block) {
      if (block.checkpointMessages.length) {
        this.applyStepMessages(
          this.blocks.blockStartStep(blockIndex),
          block.checkpointMessages
        );
      }
    }
    removeRenderedTimelineNodes() {
      if (!this.renderedTimelineNodes.size) {
        return;
      }
      this.pushMessages(
        Array.from(this.renderedTimelineNodes).map((name) => ({
          type: "RemoveSceneNodeMessage",
          name
        }))
      );
      this.renderedTimelineNodes.clear();
    }
    trackTimelineNode(message) {
      const name = typeof message.name === "string" ? message.name : null;
      if (!name) {
        return;
      }
      if (message.type === "RemoveSceneNodeMessage") {
        const prefix = `${name}/`;
        for (const nodeName of Array.from(this.renderedTimelineNodes)) {
          if (nodeName === name || nodeName.startsWith(prefix)) {
            this.renderedTimelineNodes.delete(nodeName);
          }
        }
        return;
      }
      this.renderedTimelineNodes.add(name);
    }
  };

  // src/viser4d/client/bridge/playbackEngine.ts
  var PlaybackEngine = class {
    constructor(config, scene, blocks, audio, callbacks) {
      this.config = config;
      this.scene = scene;
      this.blocks = blocks;
      this.audio = audio;
      this.callbacks = callbacks;
      this.playing = false;
      this.currentStep = 0;
      this.playStartStep = 0;
      this.playStartPerfTime = 0;
      this.rafId = null;
    }
    updateConfig(config) {
      this.config = config;
    }
    getTransportStep(timestamp = performance.now()) {
      if (!this.playing) {
        return this.currentStep;
      }
      return this.playStartStep + (timestamp - this.playStartPerfTime) / 1e3 * this.getPlaybackFps();
    }
    play(payload) {
      const step = this.getTransportStep();
      this.config.speed = payload.speed;
      this.config.loop = payload.loop;
      this.playing = true;
      this.anchorTransport(step);
      this.audio.play(step, this.getPlaybackFps());
      if (this.rafId !== null) {
        getWindow().cancelAnimationFrame(this.rafId);
      }
      this.callbacks.sendSpeedToServer(payload.speed);
      this.callbacks.sendPlaybackStateToServer(true);
      this.callbacks.syncPlaybackButtons();
      this.rafId = getWindow().requestAnimationFrame((ts) => this.tick(ts));
    }
    pause() {
      const step = this.getTransportStep();
      this.currentStep = step;
      this.playing = false;
      if (this.rafId !== null) {
        getWindow().cancelAnimationFrame(this.rafId);
        this.rafId = null;
      }
      this.audio.pause(step, this.getPlaybackFps());
      this.callbacks.sendPlaybackStateToServer(false);
      this.callbacks.syncTimestepToServer(Math.floor(this.currentStep), true);
      this.callbacks.syncPlaybackButtons();
    }
    seek(payload) {
      const step = Math.max(
        0,
        Math.min(this.config.numSteps - 1, payload.step)
      );
      this.currentStep = step;
      if (this.playing) {
        this.anchorTransport(step);
      }
      if (!this.blocks.ensureStepLoaded(step)) {
        return;
      }
      this.scene.applyThrough(step);
      this.audio.seek(step, this.getPlaybackFps(), this.playing);
      this.callbacks.syncTimestepToServer(step, true);
    }
    refresh() {
      this.scene.rebuildThrough(Math.floor(this.currentStep));
    }
    setSpeed(payload) {
      const step = this.getTransportStep();
      this.config.speed = payload.speed;
      this.config.loop = payload.loop;
      this.anchorTransport(step);
      this.audio.setFps(step, this.getPlaybackFps(), this.playing);
      this.callbacks.sendSpeedToServer(payload.speed);
    }
    syncAudioTransport() {
      this.audio.seek(this.currentStep, this.getPlaybackFps(), this.playing);
    }
    getPlaybackFps() {
      return this.config.timelineFps * this.config.speed;
    }
    anchorTransport(step, timestamp = performance.now()) {
      this.currentStep = step;
      this.playStartStep = step;
      this.playStartPerfTime = timestamp;
    }
    tick(timestamp) {
      if (!this.playing) {
        return;
      }
      const previousStep = this.currentStep;
      const next = this.getTransportStep(timestamp);
      if (next >= this.config.numSteps) {
        if (!this.config.loop) {
          this.currentStep = this.config.numSteps - 1;
          this.playing = false;
          this.audio.pause(this.currentStep, this.getPlaybackFps());
          this.callbacks.syncAdvancedTimesteps(
            previousStep,
            this.currentStep,
            true
          );
          this.callbacks.sendPlaybackStateToServer(false);
          this.callbacks.syncPlaybackButtons();
          return;
        }
        this.anchorTransport(0, timestamp);
        this.scene.rebuildThrough(0);
        this.audio.play(0, this.getPlaybackFps());
        this.callbacks.syncAdvancedTimesteps(previousStep, 0, true);
      } else {
        this.currentStep = next;
        this.scene.advanceThrough(Math.floor(this.currentStep));
        this.callbacks.syncAdvancedTimesteps(previousStep, this.currentStep);
      }
      this.rafId = getWindow().requestAnimationFrame((ts) => this.tick(ts));
    }
  };

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
      this.debug = debugState;
      this.viewer = null;
      this.playbackTimeSlider = null;
      this.config = {
        numSteps: 1,
        blockSize: 64,
        timelineFps: 30,
        speed: 1,
        loop: false,
        blockRequestSyncUuid: null,
        timelineSliderUuid: null,
        speedSliderUuid: null,
        stepButtonsUuid: null,
        playButtonUuid: null,
        pauseButtonUuid: null,
        speedSyncUuid: null,
        playbackStateSyncUuid: null,
        timestepSyncUuid: null
      };
      this.lastLocalSliderStep = -1;
      this.lastSyncedStep = -1;
      this.audio = new AudioRuntime(
        () => this.engine.getTransportStep(),
        (event, payload) => debugState.push(event, payload)
      );
      this.blocks = new BlockCache(
        (uuid, value) => this.sendGuiUpdate(uuid, value)
      );
      this.scene = new SceneApplicator(
        (messages) => this.pushMessages(messages),
        this.audio,
        this.blocks,
        /* @__PURE__ */ new Set()
      );
      this.engine = new PlaybackEngine(
        this.config,
        this.scene,
        this.blocks,
        this.audio,
        {
          syncAdvancedTimesteps: (prev, next, force) => this.syncAdvancedTimesteps(prev, next, force),
          syncTimestepToServer: (step, force) => this.syncTimestepToServer(step, force),
          syncPlaybackButtons: () => this.syncPlaybackButtons(),
          sendPlaybackStateToServer: (p) => this.sendPlaybackStateToServer(p),
          sendSpeedToServer: (s) => this.sendSpeedToServer(s)
        }
      );
      // File/embed playback audio
      this.playbackAudio = new AudioRuntime(
        () => this.playbackTime,
        (event, payload) => debugState.push(event, payload)
      );
      this.playbackTime = 0;
      this.playbackPlaying = false;
      this.playbackObserved = false;
      this.playbackLastAppliedMessageTime = -1;
      // Browser integration state
      this.queueIngressConfigured = false;
      this.guiMessageInterceptorInstalled = false;
      this.queuePush = null;
      this.playbackMonitor = null;
      this.playbackAudio.setStepRate(1);
      this.installWhenReady();
    }
    get appliedStep() {
      return this.scene.appliedStep;
    }
    // --- Public API (called from Python runtime messages) ---
    configure(config) {
      this.config = { ...this.config, ...config };
      this.blocks.blockSize = this.config.blockSize;
      this.blocks.blockRequestSyncUuid = this.config.blockRequestSyncUuid;
      this.engine.updateConfig(this.config);
      this.audio.setStepRate(this.config.timelineFps);
      debugState.push("runtime.configure", this.config);
      this.engine.syncAudioTransport();
      this.syncPlaybackButtons();
    }
    loadBlock(payload) {
      const block = {
        checkpointMessages: payload.checkpointMessages.map(
          (m) => normalizeTransportMessage(m)
        ),
        stepMessages: payload.stepMessages.map(
          (messages) => messages.map((m) => normalizeTransportMessage(m))
        )
      };
      this.blocks.loadBlock(payload.block, block);
      const activeBlock = this.blocks.blockIndexOf(
        Math.floor(this.engine.currentStep)
      );
      if (payload.block === activeBlock && this.scene.appliedBlock === activeBlock) {
        this.scene.rebuildThrough(Math.floor(this.engine.currentStep));
        return;
      }
      if (this.blocks.pendingStep !== null && this.blocks.getBlock(this.blocks.pendingStep)) {
        const step = this.blocks.pendingStep;
        this.blocks.pendingStep = null;
        this.engine.seek({ step });
      }
    }
    evictBlock(payload) {
      this.blocks.evictBlock(payload.block, this.scene.appliedBlock);
    }
    seek(payload) {
      this.engine.seek(payload);
    }
    refresh() {
      this.engine.refresh();
    }
    play(payload) {
      this.engine.play(payload);
    }
    pause() {
      this.engine.pause();
    }
    setSpeed(payload) {
      this.engine.setSpeed(payload);
    }
    applyMessageUpdate(rawMessage) {
      const message = normalizeTransportMessage(rawMessage);
      const name = typeof message.name === "string" ? message.name : null;
      debugState.push("runtime.apply_message_update", {
        type: message.type,
        name,
        step: Math.floor(this.engine.currentStep)
      });
      if (isAudioMessage(message)) {
        this.audio.applyLiveMessages(Math.floor(this.engine.currentStep), [
          message
        ]);
        return;
      }
      this.pushMessages([message]);
    }
    // --- Browser integration ---
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
      try {
        this.configureQueueIngress();
        this.installGuiMessageInterceptor();
        if (this.getViewer().messageSource !== "websocket") {
          this.installPlaybackMonitor();
        }
      } catch {
        getWindow().requestAnimationFrame(() => this.installWhenReady());
      }
    }
    configureQueueIngress() {
      if (this.queueIngressConfigured) {
        return;
      }
      const queue = this.getViewer().mutable.current.messageQueue;
      const originalPush = queue.push.bind(queue);
      this.queuePush = originalPush;
      queue.push = (...messages) => {
        const forwarded = [];
        for (const message of messages) {
          const normalized = normalizeTransportMessage(message);
          if (this.handleQueuedMessage(normalized)) {
            continue;
          }
          forwarded.push(normalized);
        }
        return originalPush(...forwarded);
      };
      this.queueIngressConfigured = true;
    }
    installPlaybackMonitor() {
      if (this.playbackMonitor) {
        return;
      }
      const slider = this.getPlaybackTimeSlider();
      if (!slider) {
        throw new Error("[viser4d] Could not find the playback slider.");
      }
      this.playbackMonitor = new MutationObserver(() => {
        this.syncPlaybackState();
      });
      this.playbackMonitor.observe(slider, {
        attributes: true,
        attributeFilter: ["aria-valuenow"]
      });
      this.syncPlaybackState();
    }
    installGuiMessageInterceptor() {
      if (this.guiMessageInterceptorInstalled) {
        return;
      }
      const mutable = this.getViewer().mutable.current;
      let rawSendMessage = mutable.sendMessage;
      const wrappedSendMessage = (message) => {
        if (message.type === "GuiUpdateMessage" && this.handleLocalPlaybackGuiMessage(message)) {
          return;
        }
        rawSendMessage(message);
      };
      Object.defineProperty(mutable, "sendMessage", {
        configurable: true,
        enumerable: true,
        get: () => wrappedSendMessage,
        set: (value) => {
          rawSendMessage = value;
        }
      });
      this.guiMessageInterceptorInstalled = true;
    }
    handleLocalPlaybackGuiMessage(message) {
      const value = message.updates.value;
      if (message.uuid === this.config.timelineSliderUuid) {
        const step = Number(value);
        if (!Number.isFinite(step)) {
          return true;
        }
        this.engine.seek({ step });
        return true;
      }
      if (message.uuid === this.config.speedSliderUuid) {
        const speed = Number(value);
        if (!Number.isFinite(speed)) {
          return true;
        }
        this.engine.setSpeed({ speed, loop: this.config.loop });
        return true;
      }
      if (message.uuid === this.config.stepButtonsUuid) {
        if (value === "Prev") {
          this.engine.seek({ step: Math.floor(this.engine.currentStep) - 1 });
        } else if (value === "Next") {
          this.engine.seek({ step: Math.floor(this.engine.currentStep) + 1 });
        }
        return true;
      }
      if (message.uuid === this.config.playButtonUuid) {
        this.engine.play({ speed: this.config.speed, loop: this.config.loop });
        return true;
      }
      if (message.uuid === this.config.pauseButtonUuid) {
        this.engine.pause();
        return true;
      }
      return false;
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
    // --- File/embed playback sync ---
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
    // --- Server sync helpers ---
    pushMessages(messages) {
      if (this.queuePush) {
        this.queuePush(...messages);
        return;
      }
      this.getViewer().mutable.current.messageQueue.push(...messages);
    }
    sendGuiUpdate(uuid, value) {
      this.getViewer().mutable.current.sendMessage({
        type: "GuiUpdateMessage",
        uuid,
        updates: { value }
      });
    }
    syncPlaybackButtons() {
      const viewer = this.getViewer();
      const sync = (uuid, visible) => {
        if (!uuid || viewer.useGuiConfig.get(uuid) === void 0) {
          return;
        }
        viewer.guiActions.updateGuiProps(uuid, { visible });
      };
      sync(this.config.playButtonUuid, !this.engine.playing);
      sync(this.config.pauseButtonUuid, this.engine.playing);
    }
    syncTimelineSlider(step, force = false) {
      const clampedStep = Math.max(0, Math.min(this.config.numSteps - 1, step));
      if (this.config.timelineSliderUuid && (force || clampedStep !== this.lastLocalSliderStep)) {
        this.lastLocalSliderStep = clampedStep;
        this.pushMessages([
          {
            type: "GuiUpdateMessage",
            uuid: this.config.timelineSliderUuid,
            updates: { value: clampedStep }
          }
        ]);
      }
    }
    sendTimestepToServer(step, force = false) {
      const clampedStep = Math.max(0, Math.min(this.config.numSteps - 1, step));
      if (!this.config.timestepSyncUuid) {
        return;
      }
      if (!force && clampedStep === this.lastSyncedStep) {
        return;
      }
      this.lastSyncedStep = clampedStep;
      this.sendGuiUpdate(this.config.timestepSyncUuid, clampedStep);
    }
    syncTimestepToServer(step, force = false) {
      this.syncTimelineSlider(step, force);
      this.sendTimestepToServer(step, force);
    }
    syncAdvancedTimesteps(previousStep, nextStep, forceFinal = false) {
      const previousDiscrete = Math.floor(previousStep);
      const nextDiscrete = Math.floor(nextStep);
      this.syncTimelineSlider(nextDiscrete, forceFinal);
      if (nextDiscrete === previousDiscrete) {
        if (forceFinal) {
          this.sendTimestepToServer(nextDiscrete, true);
        }
        return;
      }
      if (nextDiscrete > previousDiscrete) {
        for (let step = previousDiscrete + 1; step <= nextDiscrete; step += 1) {
          this.sendTimestepToServer(step);
        }
        return;
      }
      for (let step = previousDiscrete + 1; step < this.config.numSteps; step += 1) {
        this.sendTimestepToServer(step);
      }
      for (let step = 0; step <= nextDiscrete; step += 1) {
        this.sendTimestepToServer(step);
      }
    }
    sendSpeedToServer(speed) {
      if (this.config.speedSyncUuid) {
        this.sendGuiUpdate(this.config.speedSyncUuid, speed);
      }
    }
    sendPlaybackStateToServer(isPlaying) {
      if (this.config.playbackStateSyncUuid) {
        this.sendGuiUpdate(this.config.playbackStateSyncUuid, isPlaying);
      }
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
