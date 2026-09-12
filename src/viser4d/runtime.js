"use strict";
(() => {
  var __defProp = Object.defineProperty;
  var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
  var __publicField = (obj, key, value) => __defNormalProp(obj, typeof key !== "symbol" ? key + "" : key, value);

  // protocol.gen.ts
  var _TimelineControlMessageTypes = /* @__PURE__ */ new Set(["TimelineBlockBytesMessage", "TimelineBlockMessage", "TimelineClearMessage", "TimelineConfigureMessage", "TimelineOverrideMessage", "TimelinePauseMessage", "TimelinePlayMessage", "TimelineRefreshMessage", "TimelineSeekMessage", "TimelineSetSpeedMessage"]);
  function isTimelineControlMessage(message) {
    return _TimelineControlMessageTypes.has(message.type);
  }

  // node_modules/viser-audio/audio.ts
  var DRIFT_SECONDS = 0.08;
  var AudioEngine = class {
    constructor(host) {
      this.host = host;
      __publicField(this, "ctx", null);
      __publicField(this, "clips", /* @__PURE__ */ new Map());
      __publicField(this, "transport", null);
      __publicField(this, "frame", null);
      __publicField(this, "unlockButton", null);
      __publicField(this, "resumePending", false);
      __publicField(this, "disposed", false);
      __publicField(this, "unlock", () => {
        const ctx = this.ctx;
        if (!ctx || ctx.state === "running" || this.resumePending) return;
        this.resumePending = true;
        void ctx.resume().then(() => {
          if (this.disposed) return;
          this.hideUnlock();
          this.syncTimeline();
        }).catch((error) => {
          if (!this.disposed) console.error("[viser-audio] Audio context could not resume", error);
        }).finally(() => {
          this.resumePending = false;
        });
      });
      __publicField(this, "tick", () => {
        this.frame = null;
        this.sync(false);
      });
    }
    handle(message) {
      this.checkAlive();
      if (message.type === "AudioAddMessage") {
        this.add(message);
      } else if (message.type === "AudioRemoveMessage") {
        this.remove(message.name);
      } else {
        const clip = this.clips.get(message.name);
        if (!clip) throw new Error(`Unknown audio clip: ${message.name}`);
        switch (message.type) {
          case "AudioSamplesMessage": {
            const buffer = this.buildBuffer(message.samples, message.num_channels, clip.sampleRate);
            this.stop(clip);
            clip.numChannels = message.num_channels;
            clip.buffer = buffer;
            clip.progress = 0;
            this.startLive(clip);
            break;
          }
          case "AudioAppendMessage": {
            this.append(clip, message.samples);
            break;
          }
          case "AudioUpdateMessage": {
            const u = message.updates;
            if (u.volume !== void 0) {
              clip.volume = u.volume;
              clip.gain.gain.value = u.volume;
            }
            if (u.loop !== void 0) {
              clip.progress = this.playhead(clip);
              clip.startedAt = this.context().currentTime;
              clip.loop = u.loop;
              if (clip.source) clip.source.loop = u.loop;
            }
            if (u.positional !== void 0) {
              clip.positional = u.positional;
              this.connectOutput(clip);
            }
            if (u.playback_rate !== void 0) {
              positiveRate(u.playback_rate);
              clip.progress = this.playhead(clip);
              this.stop(clip);
              clip.playbackRate = u.playback_rate;
              this.startLive(clip);
            }
            break;
          }
          case "AudioPlaybackMessage": {
            if (clip.startTime !== null)
              throw new Error("Use the transport to play timeline tracks.");
            this.stop(clip);
            clip.progress = message.offset;
            clip.playRequested = message.playing;
            this.startLive(clip);
            break;
          }
        }
      }
      this.syncTimeline();
      this.scheduleFrame();
    }
    /** Bind a client-local clock. Call sync() after seeks for immediate response. */
    setTransport(transport) {
      this.checkAlive();
      this.transport = transport;
      for (const clip of this.clips.values()) {
        if (clip.startTime !== null) this.stop(clip);
      }
      this.sync();
    }
    /** Restore the tracks folded at a recording checkpoint, then apply its events. */
    loadCheckpoint(tracks) {
      this.reset();
      for (const track of tracks) this.add(track);
      this.sync();
    }
    sync(force = true) {
      this.checkAlive();
      this.syncTimeline(force);
      this.updateSpatialization();
      this.scheduleFrame();
    }
    reset() {
      this.checkAlive();
      for (const name of this.clips.keys()) this.remove(name);
      if (this.frame !== null) cancelAnimationFrame(this.frame);
      this.frame = null;
    }
    debug() {
      return [...this.clips.values()].map((clip) => ({
        name: clip.name,
        numChannels: clip.numChannels,
        sampleRate: clip.sampleRate,
        numFrames: clip.buffer?.length ?? 0,
        duration: clip.buffer?.duration ?? 0,
        playing: clip.source !== null,
        position: Math.max(0, this.playhead(clip)),
        volume: clip.volume,
        loop: clip.loop,
        positional: clip.positional,
        playbackRate: clip.playbackRate,
        startTime: clip.startTime,
        contextState: this.ctx?.state ?? "none"
      }));
    }
    dispose() {
      if (this.disposed) return;
      this.reset();
      this.disposed = true;
      document.removeEventListener("pointerdown", this.unlock);
      document.removeEventListener("keydown", this.unlock);
      this.hideUnlock();
      void this.ctx?.close();
      this.ctx = null;
    }
    add(message) {
      positiveRate(message.playback_rate);
      const buffer = this.buildBuffer(message.samples, message.num_channels, message.sample_rate);
      this.remove(message.name);
      const clip = {
        name: message.name,
        sampleRate: message.sample_rate,
        numChannels: message.num_channels,
        volume: message.volume,
        loop: message.loop,
        positional: message.positional,
        playbackRate: message.playback_rate,
        startTime: message.start_time,
        buffer,
        source: null,
        gain: this.context().createGain(),
        panner: null,
        playRequested: false,
        progress: 0,
        startedAt: 0,
        sourceRate: message.playback_rate
      };
      clip.gain.gain.value = clip.volume;
      this.clips.set(clip.name, clip);
      this.connectOutput(clip);
    }
    context() {
      if (!this.ctx) {
        this.ctx = new AudioContext();
        document.addEventListener("pointerdown", this.unlock);
        document.addEventListener("keydown", this.unlock);
      }
      return this.ctx;
    }
    requestUnlock() {
      if (!this.unlockButton) {
        const button2 = document.createElement("button");
        button2.type = "button";
        button2.textContent = "Enable audio";
        button2.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:10000;padding:8px 16px;cursor:pointer";
        button2.addEventListener("click", this.unlock);
        document.body.append(button2);
        this.unlockButton = button2;
      }
      this.unlock();
    }
    hideUnlock() {
      this.unlockButton?.remove();
      this.unlockButton = null;
    }
    buildBuffer(samples, channels, rate) {
      const flat = sampleFloats(samples);
      if (!Number.isInteger(channels) || channels < 1 || channels > 32 || flat.length % channels) {
        throw new Error("Invalid interleaved audio channel count or sample length.");
      }
      const frames = flat.length / channels;
      if (!frames) return null;
      const buffer = this.context().createBuffer(channels, frames, rate);
      for (let channel = 0; channel < channels; channel++) {
        const data = buffer.getChannelData(channel);
        for (let frame = 0; frame < frames; frame++) data[frame] = flat[frame * channels + channel];
      }
      return buffer;
    }
    connectOutput(clip) {
      const ctx = this.context();
      clip.gain.disconnect();
      clip.panner?.disconnect();
      if (clip.positional) {
        if (!this.host) throw new Error("Positional audio requires an AudioHost.");
        clip.panner ?? (clip.panner = new PannerNode(ctx, { panningModel: "HRTF", distanceModel: "inverse" }));
        clip.gain.connect(clip.panner);
        clip.panner.connect(ctx.destination);
      } else {
        clip.gain.connect(ctx.destination);
      }
    }
    playhead(clip) {
      if (!clip.source) return clip.progress;
      let position = clip.progress;
      position += (this.context().currentTime - clip.startedAt) * clip.sourceRate;
      const duration = clip.buffer?.duration ?? 0;
      if (clip.loop && duration && position >= 0) return position % duration;
      return Math.min(position, duration);
    }
    start(clip, position, rate) {
      const buffer = clip.buffer;
      if (!buffer || !clip.loop && position >= buffer.duration) return;
      const ctx = this.context();
      if (ctx.state !== "running") this.requestUnlock();
      const offset = clip.loop && position >= 0 ? position % buffer.duration : Math.max(0, position);
      const when = ctx.currentTime + Math.max(0, -position / rate);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.loop = clip.loop;
      source.playbackRate.value = rate;
      source.connect(clip.gain);
      source.onended = () => {
        source.disconnect();
        if (clip.source !== source) return;
        clip.source = null;
        clip.progress = buffer.duration;
      };
      source.start(when, offset);
      clip.source = source;
      clip.progress = offset;
      clip.startedAt = when;
      clip.sourceRate = rate;
    }
    stop(clip) {
      if (!clip.source) return;
      clip.source.onended = null;
      clip.source.stop();
      clip.source.disconnect();
      clip.source = null;
    }
    startLive(clip) {
      if (clip.startTime === null && clip.playRequested) {
        this.start(clip, clip.progress, clip.playbackRate);
      }
    }
    append(clip, samples) {
      const chunk = this.buildBuffer(samples, clip.numChannels, clip.sampleRate);
      if (!chunk) return;
      const old = clip.buffer;
      const oldLength = old?.length ?? 0;
      const buffer = this.context().createBuffer(
        clip.numChannels,
        oldLength + chunk.length,
        clip.sampleRate
      );
      for (let channel = 0; channel < clip.numChannels; channel++) {
        const data = buffer.getChannelData(channel);
        if (old) data.set(old.getChannelData(channel));
        data.set(chunk.getChannelData(channel), oldLength);
      }
      clip.progress = this.playhead(clip);
      this.stop(clip);
      clip.buffer = buffer;
      this.startLive(clip);
    }
    remove(name) {
      const clip = this.clips.get(name);
      if (!clip) return;
      this.stop(clip);
      clip.gain.disconnect();
      clip.panner?.disconnect();
      this.clips.delete(name);
    }
    syncTimeline(force = false) {
      if (!this.transport) return;
      const state = this.transport();
      positiveRate(state.rate);
      if (!Number.isFinite(state.position)) throw new Error("Timeline position must be finite.");
      for (const clip of this.clips.values()) {
        if (clip.startTime === null) continue;
        const duration = clip.buffer?.duration ?? 0;
        const position = (state.position - clip.startTime) * clip.playbackRate;
        const rate = state.rate * clip.playbackRate;
        if (!state.playing || !duration || !clip.loop && position >= duration) {
          this.stop(clip);
          clip.progress = Math.max(0, Math.min(position, duration));
          continue;
        }
        if (this.context().state !== "running") {
          this.requestUnlock();
          continue;
        }
        const desired = clip.loop && position >= 0 ? position % duration : position;
        const drift = Math.abs(this.playhead(clip) - desired);
        if (!force && clip.source && clip.sourceRate === rate && drift < DRIFT_SECONDS) continue;
        this.stop(clip);
        this.start(clip, position, rate);
      }
    }
    scheduleFrame() {
      const wanted = [...this.clips.values()].some(
        (clip) => clip.positional || clip.startTime !== null && this.transport
      );
      if (wanted && this.frame === null) this.frame = requestAnimationFrame(this.tick);
      if (!wanted && this.frame !== null) {
        cancelAnimationFrame(this.frame);
        this.frame = null;
      }
    }
    checkAlive() {
      if (this.disposed) throw new Error("AudioEngine has been disposed.");
    }
    updateSpatialization() {
      const ctx = this.ctx;
      if (!ctx || ![...this.clips.values()].some((clip) => clip.positional)) return;
      const camera = this.host?.cameraMatrix();
      if (camera) {
        setListenerPose(
          ctx.listener,
          translation(camera),
          // A camera looks down its own -Z.
          normalize([-camera[8], -camera[9], -camera[10]]),
          normalize([camera[4], camera[5], camera[6]])
        );
      }
      for (const clip of this.clips.values()) {
        if (!clip.positional || !clip.panner) continue;
        const matrix = this.host?.nodeMatrix(clip.name);
        if (!matrix) continue;
        setPannerPose(
          clip.panner,
          translation(matrix),
          normalize([matrix[8], matrix[9], matrix[10]])
        );
      }
    }
  };
  function translation(matrix) {
    return [matrix[12], matrix[13], matrix[14]];
  }
  function sampleFloats(samples) {
    if (samples instanceof Float32Array) return samples;
    const bytes = samples instanceof ArrayBuffer ? new Uint8Array(samples) : samples;
    if (bytes.byteLength % 4) throw new Error("Audio samples must contain complete float32 values.");
    const aligned = bytes.byteOffset % 4 === 0 ? bytes : bytes.slice();
    return new Float32Array(aligned.buffer, aligned.byteOffset, aligned.byteLength / 4);
  }
  function positiveRate(rate) {
    if (!Number.isFinite(rate) || rate <= 0)
      throw new Error("Playback rate must be finite and positive.");
  }
  function normalize(v) {
    const length = Math.hypot(v[0], v[1], v[2]);
    return length > 0 ? [v[0] / length, v[1] / length, v[2] / length] : [0, 0, -1];
  }
  function setPannerPose(panner, position, forward) {
    panner.positionX.value = position[0];
    panner.positionY.value = position[1];
    panner.positionZ.value = position[2];
    panner.orientationX.value = forward[0];
    panner.orientationY.value = forward[1];
    panner.orientationZ.value = forward[2];
  }
  function setListenerPose(listener, position, forward, up) {
    listener.positionX.value = position[0];
    listener.positionY.value = position[1];
    listener.positionZ.value = position[2];
    listener.forwardX.value = forward[0];
    listener.forwardY.value = forward[1];
    listener.forwardZ.value = forward[2];
    listener.upX.value = up[0];
    listener.upY.value = up[1];
    listener.upZ.value = up[2];
  }

  // cache.ts
  function planPreload(focusBlock, blockBytes, budgetBytes, loaded) {
    const count = blockBytes.length;
    if (count === 0) {
      return { required: [], speculative: [], evictions: [] };
    }
    const focus = Math.max(0, Math.min(count - 1, focusBlock));
    const required = [focus];
    let used = blockBytes[focus] ?? 0;
    if (focus > 0) {
      required.push(focus - 1);
      used += blockBytes[focus - 1] ?? 0;
    }
    const desired = new Set(required);
    const speculative = [];
    for (let offset = 1; offset < count; offset += 1) {
      if (used >= budgetBytes) {
        break;
      }
      const index = (focus + offset) % count;
      if (desired.has(index)) {
        continue;
      }
      const size = blockBytes[index];
      if (size === null) {
        speculative.push(index);
        desired.add(index);
        break;
      }
      if (used + size > budgetBytes) {
        break;
      }
      speculative.push(index);
      desired.add(index);
      used += size;
    }
    const evictions = [];
    for (const index of loaded.keys()) {
      if (!desired.has(index)) {
        evictions.push(index);
      }
    }
    evictions.sort((a, b) => a - b);
    return { required, speculative, evictions };
  }
  var BlockCache = class {
    constructor(io) {
      this.io = io;
      this.blockSize = 1;
      this.budgetBytes = 0;
      this.blockBytes = [];
      this.blocks = /* @__PURE__ */ new Map();
      this.pending = /* @__PURE__ */ new Set();
    }
    blockIndexOf(step) {
      return Math.floor(step / this.blockSize);
    }
    blockStartStep(index) {
      return index * this.blockSize;
    }
    getBlock(step) {
      return this.blocks.get(this.blockIndexOf(step)) ?? null;
    }
    setBlockBytes(blockBytes) {
      this.blockBytes = blockBytes;
    }
    setBudgetBytes(bytes) {
      this.budgetBytes = bytes;
    }
    loadBlock(block) {
      this.pending.delete(block.index);
      this.blocks.set(block.index, block);
    }
    reset() {
      this.blocks.clear();
      this.pending.clear();
    }
    /** Request the block holding `step` unless it is already resident. */
    ensureStepLoaded(step) {
      const index = this.blockIndexOf(step);
      if (!this.blocks.has(index)) {
        this.issue(index);
      }
    }
    /** Re-plan the resident set around `focusBlock`, pinning the applied block. */
    syncFocus(focusBlock, appliedBlock) {
      if (this.blockBytes.length === 0 || this.budgetBytes <= 0) {
        return;
      }
      const plan = planPreload(focusBlock, this.blockBytes, this.budgetBytes, this.blocks);
      const desired = /* @__PURE__ */ new Set([...plan.required, ...plan.speculative]);
      for (const index of [...this.pending]) {
        if (!desired.has(index)) {
          this.pending.delete(index);
          this.io.discardBlock(index);
        }
      }
      for (const index of plan.evictions) {
        if (index === appliedBlock) {
          continue;
        }
        this.blocks.delete(index);
        this.pending.delete(index);
        this.io.discardBlock(index);
      }
      let requiredInFlight = false;
      for (const index of plan.required) {
        if (this.blocks.has(index)) {
          continue;
        }
        requiredInFlight = true;
        this.issue(index);
      }
      if (requiredInFlight) {
        return;
      }
      for (const index of plan.speculative) {
        if (!this.blocks.has(index)) {
          this.issue(index);
          return;
        }
      }
    }
    issue(index) {
      if (this.pending.has(index)) {
        return;
      }
      this.pending.add(index);
      this.io.requestBlock(index);
    }
  };

  // player.ts
  var Player = class {
    constructor(listener) {
      this.listener = listener;
      this.numSteps = 1;
      this.fps = 30;
      this._speed = 1;
      this._loop = false;
      this._playing = false;
      this.position = 0;
      // fractional current step
      this.anchorStep = 0;
      this.anchorTime = 0;
      this.emitted = -1;
      // last integer step handed to the listener
      this.rafId = null;
    }
    get playing() {
      return this._playing;
    }
    get speed() {
      return this._speed;
    }
    get loop() {
      return this._loop;
    }
    get currentStep() {
      return Math.min(this.numSteps - 1, Math.max(0, Math.floor(this.position)));
    }
    configure(numSteps, fps, speed, loop) {
      this.numSteps = Math.max(1, numSteps);
      this.fps = fps;
      this._speed = speed;
      this._loop = loop;
      this.position = Math.min(this.position, this.numSteps - 1);
      if (this._playing) {
        this.anchor(this.position);
      }
    }
    getTransportStep(now = performance.now()) {
      if (!this._playing) {
        return this.position;
      }
      return this.anchorStep + (now - this.anchorTime) / 1e3 * this.fps * this._speed;
    }
    play(speed, loop) {
      if (speed !== void 0) {
        this._speed = speed;
      }
      if (loop !== void 0) {
        this._loop = loop;
      }
      if (!this._loop && this.currentStep >= this.numSteps - 1) {
        this.seek(0);
      }
      this._playing = true;
      this.anchor(this.position);
      this.listener.transport();
      this.startRaf();
    }
    pause() {
      this.position = this.clamp(this.getTransportStep());
      this._playing = false;
      this.stopRaf();
      this.emitForward(Math.floor(this.position));
      this.listener.transport();
    }
    seek(step) {
      this.position = this.clamp(step);
      if (this._playing) {
        this.anchor(this.position);
      }
      this.emitted = Math.floor(this.position);
      this.listener.step(this.emitted, false);
    }
    setSpeed(speed, loop) {
      const step = this.getTransportStep();
      this._speed = speed;
      this._loop = loop;
      if (this._playing) {
        this.anchor(this.clamp(step));
      }
      this.listener.transport();
    }
    refresh() {
      this.listener.step(this.currentStep, false);
    }
    dispose() {
      this.stopRaf();
      this._playing = false;
      this.position = 0;
      this.emitted = -1;
    }
    clamp(step) {
      return Math.min(this.numSteps - 1, Math.max(0, step));
    }
    anchor(step, now = performance.now()) {
      this.position = step;
      this.anchorStep = step;
      this.anchorTime = now;
    }
    startRaf() {
      this.stopRaf();
      this.rafId = requestAnimationFrame((ts) => this.tick(ts));
    }
    stopRaf() {
      if (this.rafId !== null) {
        cancelAnimationFrame(this.rafId);
        this.rafId = null;
      }
    }
    emitForward(through) {
      for (let step = this.emitted + 1; step <= through; step += 1) {
        this.emitted = step;
        this.listener.step(step, true);
      }
    }
    tick(timestamp) {
      if (!this._playing) {
        return;
      }
      const next = this.getTransportStep(timestamp);
      if (next >= this.numSteps) {
        this.emitForward(this.numSteps - 1);
        if (this._loop) {
          this.anchor(0, timestamp);
          this.emitted = 0;
          this.listener.step(0, false);
          this.startRaf();
        } else {
          this.position = this.numSteps - 1;
          this._playing = false;
          this.stopRaf();
          this.listener.transport();
        }
        return;
      }
      this.position = next;
      this.emitForward(Math.floor(next));
      this.startRaf();
    }
  };

  // state.ts
  var CREATE_PREFIX = "create:";
  function isCreateKey(key) {
    return key.startsWith(CREATE_PREFIX);
  }
  function isDescendant(name, ancestor) {
    return name === ancestor || name !== null && name.startsWith(ancestor + "/");
  }
  function hasAncestorInSet(name, names) {
    let slash = name.lastIndexOf("/");
    while (slash > 0) {
      const parent = name.slice(0, slash);
      if (names.has(parent)) {
        return true;
      }
      slash = parent.lastIndexOf("/");
    }
    return false;
  }
  function topmost(names) {
    const set = new Set(names);
    return names.filter((name) => !hasAncestorInSet(name, set));
  }
  function compareNodeNames(left, right) {
    const depth = left.split("/").length - right.split("/").length;
    return depth !== 0 ? depth : left.localeCompare(right);
  }
  function orderEntries(entries) {
    const globals = [];
    const byNode = /* @__PURE__ */ new Map();
    for (const entry of entries) {
      if (entry.name === null) {
        globals.push(entry.message);
        continue;
      }
      const bucket = byNode.get(entry.name);
      if (bucket) {
        bucket.push(entry);
      } else {
        byNode.set(entry.name, [entry]);
      }
    }
    const out = [...globals];
    for (const name of [...byNode.keys()].sort(compareNodeNames)) {
      const bucket = byNode.get(name);
      for (const entry of bucket) {
        if (isCreateKey(entry.key)) {
          out.push(entry.message);
        }
      }
      for (const entry of bucket) {
        if (!isCreateKey(entry.key)) {
          out.push(entry.message);
        }
      }
    }
    return out;
  }
  function decodeBlock(message) {
    const checkpointScene = /* @__PURE__ */ new Map();
    for (const entry of message.checkpointScene) {
      checkpointScene.set(entry.key, entry);
    }
    return {
      index: message.index,
      checkpointScene,
      checkpointAudio: message.checkpointAudio,
      deltas: message.deltas
    };
  }
  function deleteSubtree(state, name) {
    for (const [key, entry] of state) {
      if (isDescendant(entry.name, name)) {
        state.delete(key);
      }
    }
  }
  function putEntry(state, entry) {
    if (isCreateKey(entry.key) && entry.name !== null) {
      for (const [key, existing] of state) {
        if (key !== entry.key && existing.name === entry.name) {
          state.delete(key);
        }
      }
    }
    state.set(entry.key, entry);
  }
  function applyDelta(state, delta) {
    for (const name of delta.deleteNodes) {
      deleteSubtree(state, name);
    }
    for (const put of delta.puts) {
      putEntry(state, put);
    }
  }
  function isTombstone(entry) {
    return entry.message.type === "RemoveSceneNodeMessage" && entry.name !== null;
  }
  function applyOverrideEntry(overlay, entry) {
    if (isTombstone(entry)) {
      for (const [key, existing] of overlay) {
        if (isDescendant(existing.name, entry.name)) {
          overlay.delete(key);
        }
      }
    }
    overlay.set(entry.key, entry);
  }
  function applyOverlay(state, overlay) {
    for (const entry of overlay.values()) {
      if (isTombstone(entry)) {
        deleteSubtree(state, entry.name);
        continue;
      }
      if (entry.name === null || state.has(CREATE_PREFIX + entry.name)) {
        state.set(entry.key, entry);
      }
    }
  }
  function foldTarget(block, offset, overlay) {
    const state = new Map(block.checkpointScene);
    for (let i = 0; i <= offset; i += 1) {
      const delta = block.deltas[i];
      if (delta) {
        applyDelta(state, delta);
      }
    }
    applyOverlay(state, overlay);
    return state;
  }
  var removeMessage = (name) => ({
    type: "RemoveSceneNodeMessage",
    name,
    owner: ""
  });
  var SceneMirror = class {
    constructor() {
      this.applied = /* @__PURE__ */ new Map();
    }
    reset() {
      this.applied.clear();
    }
    existingNodes() {
      const names = /* @__PURE__ */ new Set();
      for (const [key, entry] of this.applied) {
        if (isCreateKey(key) && entry.name !== null) {
          names.add(entry.name);
        }
      }
      return names;
    }
    /** Fast path: apply one forward delta directly (the delta *is* the diff). */
    advance(delta) {
      const doomed = delta.deleteNodes.filter(
        (name) => this.applied.has(CREATE_PREFIX + name)
      );
      const out = topmost(doomed).map(removeMessage);
      for (const name of doomed) {
        deleteSubtree(this.applied, name);
      }
      out.push(...orderEntries(delta.puts));
      for (const put of delta.puts) {
        putEntry(this.applied, put);
      }
      return out;
    }
    /**
     * Re-apply the override overlay after a forward advance. Tombstones emit a
     * remove only while the applied scene still holds the node; puts emit only
     * when their rev differs from what is applied. Nearly free in steady state.
     */
    reapplyOverrides(overlay) {
      const removes = [];
      const pushes = [];
      for (const entry of overlay.values()) {
        if (isTombstone(entry)) {
          removes.push(...this.removeNode(entry.name));
          continue;
        }
        if (entry.name !== null && !this.applied.has(CREATE_PREFIX + entry.name)) {
          continue;
        }
        const applied = this.applied.get(entry.key);
        if (!applied || applied.rev !== entry.rev) {
          pushes.push(entry);
          this.applied.set(entry.key, entry);
        }
      }
      return [...removes, ...orderEntries(pushes)];
    }
    /** An override RemoveSceneNodeMessage: delete the node from the applied scene. */
    removeNode(name) {
      let held = false;
      for (const entry of this.applied.values()) {
        if (isDescendant(entry.name, name)) {
          held = true;
          break;
        }
      }
      deleteSubtree(this.applied, name);
      return held ? [removeMessage(name)] : [];
    }
    /** Full rev-diff from the applied state to `target`. */
    rebuild(target) {
      const targetNodes = /* @__PURE__ */ new Set();
      const targetByNode = /* @__PURE__ */ new Map();
      const targetGlobals = [];
      for (const [key, entry] of target) {
        if (entry.name === null) {
          targetGlobals.push(entry);
          continue;
        }
        if (isCreateKey(key)) {
          targetNodes.add(entry.name);
        }
        const bucket = targetByNode.get(entry.name);
        if (bucket) {
          bucket.push(entry);
        } else {
          targetByNode.set(entry.name, [entry]);
        }
      }
      const appliedByNode = /* @__PURE__ */ new Map();
      for (const [key, entry] of this.applied) {
        if (entry.name === null) {
          continue;
        }
        const bucket = appliedByNode.get(entry.name);
        if (bucket) {
          bucket.push(key);
        } else {
          appliedByNode.set(entry.name, [key]);
        }
      }
      const appliedNodes = this.existingNodes();
      const vanished = [...appliedNodes].filter((name) => !targetNodes.has(name));
      const removes = topmost(vanished).map(removeMessage);
      const pushes = [];
      for (const [name, entries] of targetByNode) {
        const isNew = !appliedNodes.has(name);
        let stale = false;
        if (!isNew) {
          const targetCreate = target.get(CREATE_PREFIX + name);
          const appliedCreate = this.applied.get(CREATE_PREFIX + name);
          if (!targetCreate || !appliedCreate || appliedCreate.rev !== targetCreate.rev) {
            stale = true;
          } else {
            for (const key of appliedByNode.get(name) ?? []) {
              if (!target.has(key)) {
                stale = true;
                break;
              }
            }
          }
        }
        if (isNew || stale) {
          pushes.push(...entries);
        } else {
          for (const entry of entries) {
            const applied = this.applied.get(entry.key);
            if (!applied || applied.rev !== entry.rev) {
              pushes.push(entry);
            }
          }
        }
      }
      for (const entry of targetGlobals) {
        const applied = this.applied.get(entry.key);
        if (!applied || applied.rev !== entry.rev) {
          pushes.push(entry);
        }
      }
      const next = new Map(target);
      for (const [key, entry] of this.applied) {
        if (entry.name === null && !target.has(key)) {
          next.set(key, entry);
        }
      }
      this.applied = next;
      return [...removes, ...orderEntries(pushes)];
    }
  };

  // ui.ts
  var SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4];
  var ICON = {
    play: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M4 3.2v9.6l7.5-4.8z"/></svg>',
    pause: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="3.5" y="3" width="3" height="10" rx="1"/><rect x="9.5" y="3" width="3" height="10" rx="1"/></svg>',
    prev: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M5 3v10H3.5V3zm8 0v10l-7-5z"/></svg>',
    next: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M11 3v10h1.5V3zM3 3v10l7-5z"/></svg>',
    loop: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 6a4 4 0 0 1 4-4h4l-1.5-1.5M13 10a4 4 0 0 1-4 4H5l1.5 1.5"/></svg>'
  };
  function button(html) {
    const el = document.createElement("button");
    el.innerHTML = html;
    el.style.cssText = "display:flex;align-items:center;justify-content:center;width:30px;height:30px;padding:0;border:none;border-radius:7px;background:transparent;color:#e8e8ef;cursor:pointer;transition:background .12s;";
    el.addEventListener("pointerenter", () => {
      el.style.background = "rgba(255,255,255,0.12)";
    });
    el.addEventListener("pointerleave", () => {
      el.style.background = "transparent";
    });
    return el;
  }
  var PlaybackBar = class {
    constructor(callbacks) {
      this.callbacks = callbacks;
      this.root = document.createElement("div");
      this.playBtn = button(ICON.play);
      this.prevBtn = button(ICON.prev);
      this.nextBtn = button(ICON.next);
      this.loopBtn = button(ICON.loop);
      this.slider = document.createElement("input");
      this.label = document.createElement("span");
      this.speed = document.createElement("select");
      this.dragging = false;
      this.state = { playing: false, step: 0, total: 1, speed: 1, loop: false };
      this.build();
    }
    mount() {
      document.body.appendChild(this.root);
    }
    dispose() {
      this.root.remove();
    }
    setState(state) {
      this.state = state;
      this.playBtn.innerHTML = state.playing ? ICON.pause : ICON.play;
      this.playBtn.setAttribute("aria-label", state.playing ? "Pause" : "Play");
      this.loopBtn.setAttribute("aria-pressed", String(state.loop));
      this.slider.max = String(Math.max(0, state.total - 1));
      if (!this.dragging) {
        this.slider.value = String(state.step);
      }
      this.label.textContent = `${state.step + 1} / ${state.total}`;
      this.speed.value = String(state.speed);
      this.loopBtn.style.color = state.loop ? "#7aa2ff" : "#e8e8ef";
    }
    build() {
      this.root.tabIndex = 0;
      this.root.dataset.viser4dPlayback = "";
      this.root.setAttribute("role", "group");
      this.root.setAttribute("aria-label", "Timeline playback");
      this.prevBtn.setAttribute("aria-label", "Previous step");
      this.nextBtn.setAttribute("aria-label", "Next step");
      this.loopBtn.setAttribute("aria-label", "Loop");
      this.slider.setAttribute("aria-label", "Timeline step");
      this.speed.setAttribute("aria-label", "Playback speed");
      this.root.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483000;display:flex;align-items:center;gap:8px;padding:8px 14px;box-sizing:border-box;max-width:min(720px,calc(100vw - 32px));width:560px;background:rgba(22,24,30,0.86);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.08);border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,0.4);font:13px/1 ui-sans-serif,system-ui,sans-serif;color:#e8e8ef;pointer-events:auto;outline:none;";
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
      this.slider.style.cssText = "flex:1;min-width:80px;height:4px;cursor:pointer;accent-color:#7aa2ff;";
      const stopDrag = () => {
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
      this.label.style.cssText = "min-width:64px;text-align:center;font-variant-numeric:tabular-nums;color:#c8c8d4;";
      this.speed.style.cssText = "height:28px;padding:0 6px;border:1px solid rgba(255,255,255,0.1);border-radius:7px;background:rgba(255,255,255,0.06);color:#e8e8ef;font:12px/1 inherit;cursor:pointer;";
      for (const value of SPEEDS) {
        const option = document.createElement("option");
        option.value = String(value);
        option.textContent = `${value}x`;
        this.speed.appendChild(option);
      }
      this.speed.addEventListener("change", () => this.callbacks.setSpeed(Number(this.speed.value)));
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
        this.loopBtn
      );
    }
  };

  // controller.ts
  var Controller = class {
    // step whose block we are waiting for
    constructor(io) {
      this.io = io;
      this.scene = new SceneMirror();
      this.overlay = /* @__PURE__ */ new Map();
      this.ui = null;
      this.numSteps = 1;
      this.timelineFps = 30;
      this.appliedStep = -1;
      this.appliedBlock = -1;
      this.reportedStep = -1;
      this.focusBlock = -1;
      this.pendingStep = null;
      this.cache = new BlockCache({
        requestBlock: (index) => io.sendEvent({ type: "TimelineBlockRequestMessage", index }),
        discardBlock: (index) => io.sendEvent({ type: "TimelineBlockDiscardMessage", index })
      });
      this.player = new Player({
        step: (step, continuous) => this.applyStep(step, continuous),
        transport: () => this.onTransport()
      });
      this.audio = new AudioEngine();
      this.audio.setTransport(() => ({
        position: this.player.getTransportStep() / this.timelineFps,
        playing: this.player.playing && this.pendingStep === null,
        rate: this.player.speed
      }));
    }
    /** Called once the viewer is located, in websocket mode. */
    start() {
      this.ui = new PlaybackBar({
        play: () => this.player.play(),
        pause: () => this.player.pause(),
        prev: () => this.player.seek(this.player.currentStep - 1),
        next: () => this.player.seek(this.player.currentStep + 1),
        seek: (step) => this.player.seek(step),
        setSpeed: (speed) => this.player.setSpeed(speed, this.player.loop),
        setLoop: (loop) => this.player.setSpeed(this.player.speed, loop)
      });
      this.ui.mount();
      this.updateUi();
      if (this.io.isWebsocket()) this.io.sendEvent({ type: "TimelineReadyMessage" });
    }
    dispose() {
      this.player.dispose();
      this.audio.dispose();
      this.ui?.dispose();
      this.ui = null;
    }
    debug() {
      return {
        numSteps: this.numSteps,
        currentStep: this.player.currentStep,
        appliedStep: this.appliedStep,
        appliedBlock: this.appliedBlock,
        playing: this.player.playing,
        audio: this.audio.debug()
      };
    }
    handleControl(message) {
      switch (message.type) {
        case "TimelineConfigureMessage":
          return this.configure(message);
        case "TimelineBlockBytesMessage":
          this.cache.setBlockBytes(message.blockBytes);
          return this.refocusPreload(this.player.currentStep, true);
        case "TimelineBlockMessage":
          return this.loadBlock(message);
        case "TimelineOverrideMessage":
          return this.applyOverride(message);
        case "TimelineSeekMessage":
          return this.player.seek(message.step);
        case "TimelinePlayMessage":
          return this.player.play(message.speed, message.loop);
        case "TimelinePauseMessage":
          return this.player.pause();
        case "TimelineSetSpeedMessage":
          return this.player.setSpeed(message.speed, message.loop);
        case "TimelineClearMessage":
          return this.clear();
        case "TimelineRefreshMessage":
          return this.player.refresh();
      }
    }
    loadRecording(recording) {
      this.configure({
        type: "TimelineConfigureMessage",
        numSteps: recording.numSteps,
        blockSize: recording.numSteps,
        timelineFps: recording.fps,
        speed: 1,
        loop: true,
        cacheBytes: Number.MAX_SAFE_INTEGER,
        blockBytes: [0]
      });
      for (const entry of recording.overrides) applyOverrideEntry(this.overlay, entry);
      this.loadBlock(recording.block);
      this.player.play();
    }
    configure(message) {
      this.numSteps = message.numSteps;
      this.timelineFps = message.timelineFps;
      this.cache.blockSize = message.blockSize;
      this.cache.setBudgetBytes(message.cacheBytes);
      this.cache.setBlockBytes(message.blockBytes);
      this.player.configure(message.numSteps, message.timelineFps, message.speed, message.loop);
      this.updateUi();
      this.refocusPreload(this.player.currentStep, true);
      this.applyStep(this.player.currentStep, false);
    }
    loadBlock(message) {
      this.cache.loadBlock(decodeBlock(message));
      const step = this.player.currentStep;
      const target = this.cache.blockIndexOf(step) === message.index ? step : this.pendingStep;
      if (target !== null && this.cache.blockIndexOf(target) === message.index) {
        this.pendingStep = null;
        this.applyStep(target, false);
      }
      this.refocusPreload(step, true);
    }
    applyOverride(message) {
      applyOverrideEntry(this.overlay, message.entry);
      this.io.pushMessages(this.scene.reapplyOverrides(this.overlay));
    }
    clear() {
      this.io.pushMessages(this.scene.rebuild(/* @__PURE__ */ new Map()));
      this.player.dispose();
      this.cache.reset();
      this.scene.reset();
      this.audio.reset();
      this.overlay.clear();
      this.appliedStep = -1;
      this.appliedBlock = -1;
      this.reportedStep = -1;
      this.focusBlock = -1;
      this.pendingStep = null;
      this.updateUi();
    }
    applyStep(step, continuous) {
      const block = this.cache.getBlock(step);
      if (!block) {
        this.pendingStep = step;
        this.audio.sync();
        this.cache.ensureStepLoaded(step);
        return;
      }
      const offset = step - this.cache.blockStartStep(block.index);
      const delta = block.deltas[offset];
      const forward = continuous && this.appliedStep >= 0 && step === this.appliedStep + 1 && !!delta && (block.index === this.appliedBlock || offset === 0 && block.index === this.appliedBlock + 1);
      if (forward) {
        const messages = this.scene.advance(delta);
        messages.push(...this.scene.reapplyOverrides(this.overlay));
        this.io.pushMessages(messages);
        for (const event of delta.audio) this.audio.handle(event);
      } else {
        this.io.pushMessages(this.scene.rebuild(foldTarget(block, offset, this.overlay)));
        this.loadAudioThrough(block, offset);
        this.audio.sync();
      }
      this.appliedStep = step;
      this.appliedBlock = block.index;
      this.reportTimestep(step);
      this.updateUi();
      this.refocusPreload(step, false);
    }
    loadAudioThrough(block, offset) {
      this.audio.loadCheckpoint(block.checkpointAudio);
      for (let i = 0; i <= offset; i += 1) {
        const delta = block.deltas[i];
        if (delta) {
          for (const event of delta.audio) this.audio.handle(event);
        }
      }
    }
    onTransport() {
      this.audio.sync();
      this.updateUi();
      if (this.io.isWebsocket()) {
        this.io.sendEvent({ type: "TimelinePlaybackStateMessage", isPlaying: this.player.playing });
        this.io.sendEvent({ type: "TimelineSpeedMessage", speed: this.player.speed });
        this.reportTimestep(this.player.currentStep);
      }
    }
    reportTimestep(step) {
      if (step === this.reportedStep || !this.io.isWebsocket()) {
        return;
      }
      this.reportedStep = step;
      this.io.sendEvent({ type: "TimelineTimestepMessage", step });
    }
    refocusPreload(step, force) {
      const block = this.cache.blockIndexOf(step);
      if (!force && block === this.focusBlock) {
        return;
      }
      this.focusBlock = block;
      this.cache.syncFocus(block, this.appliedBlock);
    }
    updateUi() {
      this.ui?.setState({
        playing: this.player.playing,
        step: this.player.currentStep,
        total: this.numSteps,
        speed: this.player.speed,
        loop: this.player.loop
      });
    }
  };

  // binary.ts
  var ARRAY_TYPES = {
    "|b1": Uint8Array,
    "|u1": Uint8Array,
    "|i1": Int8Array,
    "<u2": Uint16Array,
    "<i2": Int16Array,
    "<u4": Uint32Array,
    "<i4": Int32Array,
    "<u8": BigUint64Array,
    "<i8": BigInt64Array,
    "<f2": Uint16Array,
    "<f4": Float32Array,
    "<f8": Float64Array
  };
  function decodeRecording(payload) {
    return JSON.parse(payload, (_key, value) => {
      if (!value || typeof value !== "object" || !("__typed_array" in value)) return value;
      const Constructor = ARRAY_TYPES[value.__typed_array];
      if (!Constructor) throw new Error(`Unsupported recording array dtype: ${value.__typed_array}`);
      const raw = atob(value.base64);
      const bytes = Uint8Array.from(raw, (char) => char.charCodeAt(0));
      return new Constructor(bytes.buffer);
    });
  }

  // viser.ts
  function isRecord(value) {
    return !!value && typeof value === "object";
  }
  function isViewer(value) {
    return isRecord(value) && isRecord(value.mutable) && "useGuiConfig" in value && "guiActions" in value && "useSceneTree" in value;
  }
  function reactRoot() {
    const root = document.getElementById("root");
    if (!isRecord(root)) {
      return null;
    }
    const key = Object.keys(root).find((name) => name.startsWith("__reactContainer$"));
    const container = key ? root[key] : null;
    return isRecord(container) ? container : null;
  }
  function findViewer() {
    const start = reactRoot();
    if (!start) {
      return null;
    }
    const seen = /* @__PURE__ */ new Set();
    const stack = [start];
    while (stack.length) {
      const fiber = stack.pop();
      if (!fiber || seen.has(fiber)) {
        continue;
      }
      seen.add(fiber);
      const value = fiber.memoizedProps?.value;
      if (isViewer(value)) {
        return value;
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
  var RETRY_BUDGET_MS = 5e3;
  var Viser = class {
    constructor(onMessage, onReady) {
      this.onMessage = onMessage;
      this.onReady = onReady;
      this.viewer = null;
      this.queue = null;
      this.originalPush = null;
      this.disposed = false;
      this.deadline = 0;
    }
    install() {
      this.deadline = performance.now() + RETRY_BUDGET_MS;
      this.tryInstall();
    }
    dispose() {
      this.disposed = true;
      if (this.queue && this.originalPush) {
        this.queue.push = this.originalPush;
      }
      this.viewer = null;
      this.queue = null;
      this.originalPush = null;
    }
    get isWebsocket() {
      return this.viewer?.messageSource === "websocket";
    }
    /** Push scene payloads into viser, bypassing our own interception. */
    pushMessages(messages) {
      if (!messages.length || !this.originalPush) {
        return;
      }
      this.originalPush(...messages);
    }
    /** Send an event back to the server through viser's normal path. */
    sendMessage(message) {
      this.viewer?.mutable.current.sendMessage(message);
    }
    tryInstall() {
      if (this.disposed || this.originalPush) {
        return;
      }
      const viewer = findViewer();
      if (viewer) {
        this.viewer = viewer;
        this.wrapQueue(viewer);
        this.onReady();
        return;
      }
      if (performance.now() >= this.deadline) {
        console.error(
          "[viser4d] Could not locate the viewer in the React fiber tree after 5s of retries; the timeline runtime is inactive."
        );
        return;
      }
      requestAnimationFrame(() => this.tryInstall());
    }
    wrapQueue(viewer) {
      const queue = viewer.mutable.current.messageQueue;
      const original = queue.push.bind(queue);
      queue.push = (...messages) => {
        const forwarded = [];
        for (const message of messages) {
          if (!this.onMessage(message)) {
            forwarded.push(message);
          }
        }
        return forwarded.length ? original(...forwarded) : queue.length;
      };
      this.queue = queue;
      this.originalPush = original;
    }
  };

  // index.ts
  var Runtime = class {
    constructor() {
      this.viser = new Viser(
        (message) => this.route(message),
        () => this.onReady()
      );
      this.controller = new Controller({
        pushMessages: (messages) => this.viser.pushMessages(messages),
        sendEvent: (message) => {
          if (this.viser.isWebsocket) this.viser.sendMessage(message);
        },
        isWebsocket: () => this.viser.isWebsocket
      });
      this.viser.install();
    }
    get debug() {
      return this.controller.debug();
    }
    dispose() {
      this.viser.dispose();
      this.controller.dispose();
    }
    route(message) {
      if (isTimelineControlMessage(message)) {
        this.controller.handleControl(message);
        return true;
      }
      return false;
    }
    loadRecording(payload) {
      this.controller.loadRecording(decodeRecording(payload));
    }
    onReady() {
      this.controller.start();
    }
  };
  var win = window;
  win.__VISER4D__?.dispose();
  win.__VISER4D__ = new Runtime();
})();
