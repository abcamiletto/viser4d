(function() {
  var AudioContextCtor = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextCtor) {
    window.__viser4d_audio = {
      tracks: {},
      addTrack: function() {},
      removeTrack: function() {},
      setVolume: function() {},
      setTransport: function() {},
      destroy: function() {}
    };
    return;
  }

  var SOURCE_MIN_RATE = 1e-4;
  var SOURCE_MAX_RATE = 16.0;
  var SYNC_INTERVAL_MS = 20;
  var HARD_SYNC_THRESHOLD_SEC = 0.09;
  var SOFT_SYNC_GAIN = 0.35;
  var DRIFT_RATE_GAIN = 0.5;
  var MAX_RATE_CORRECTION = 0.04;
  var MIN_RESYNC_INTERVAL_SEC = 0.25;

  var old = window.__viser4d_audio;
  if (old && typeof old.destroy === "function") {
    old.destroy();
  }

  var audioCtx = new AudioContextCtor();

  function base64ToArrayBuffer(base64) {
    var binary = atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  function clamp(v, minV, maxV) {
    return Math.max(minV, Math.min(maxV, v));
  }

  function toFiniteNumber(value, fallback) {
    var n = Number(value);
    if (!isFinite(n)) return fallback;
    return n;
  }

  function positive(value, fallback) {
    var n = toFiniteNumber(value, fallback);
    if (n <= 0) return fallback;
    return n;
  }

  var mgr = {
    tracks: {},
    _destroyed: false,
    _playing: false,
    _transportSeq: -1,
    _transportStep: 0,
    _timelineFps: 30.0,
    _playbackFps: 30.0,
    _anchorMediaTime: 0.0,
    _anchorCtxTime: 0.0,
    _lastTransportCtxTime: 0.0,
    _syncTimer: null,

    addTrack: function(name, base64Wav, startStep, volume) {
      mgr.removeTrack(name);

      var gain = audioCtx.createGain();
      gain.gain.value = toFiniteNumber(volume, 1.0);
      gain.connect(audioCtx.destination);

      var track = {
        name: name,
        startStep: toFiniteNumber(startStep, 0),
        gain: gain,
        buffer: null,
        decodeToken: 0,
        source: null,
        sourceStartCtxTime: 0.0,
        sourceStartOffset: 0.0,
        sourceRate: 1.0,
        lastHardSyncCtxTime: -Infinity
      };
      mgr.tracks[name] = track;

      var decodeToken = ++track.decodeToken;
      var bufferData = base64ToArrayBuffer(base64Wav);
      audioCtx.decodeAudioData(bufferData.slice(0)).then(function(buffer) {
        if (mgr._destroyed) return;
        var current = mgr.tracks[name];
        if (!current || current !== track || current.decodeToken !== decodeToken) return;

        current.buffer = buffer;
        mgr._syncTrack(current, true);
      }).catch(function() {});
    },

    removeTrack: function(name) {
      var track = mgr.tracks[name];
      if (!track) return;

      mgr._stopSource(track);
      try { track.gain.disconnect(); } catch (_e) {}
      delete mgr.tracks[name];
    },

    setVolume: function(name, volume) {
      var track = mgr.tracks[name];
      if (!track) return;
      track.gain.gain.value = toFiniteNumber(volume, track.gain.gain.value);
    },

    setTransport: function(payload) {
      if (!payload || typeof payload !== "object") return;

      var seq = toFiniteNumber(payload.seq, NaN);
      if (!isFinite(seq)) return;
      if (seq <= mgr._transportSeq) return;
      mgr._transportSeq = seq;

      var nextStep = toFiniteNumber(payload.step, mgr._transportStep);
      var nextTimelineFps = positive(payload.timeline_fps, mgr._timelineFps);
      var nextPlaybackFps = positive(payload.playback_fps, mgr._playbackFps);
      var nextPlaying = !!payload.playing;
      var hardSync = !!payload.hard_sync;

      var nowCtx = audioCtx.currentTime;
      var predictedMediaTime = mgr._mediaTimeAt(nowCtx);
      var desiredMediaTime = nextStep / nextTimelineFps;
      var phaseError = desiredMediaTime - predictedMediaTime;

      if (mgr._playing !== nextPlaying) hardSync = true;
      if (!hardSync && Math.abs(phaseError) > HARD_SYNC_THRESHOLD_SEC) {
        hardSync = true;
      }

      mgr._transportStep = nextStep;
      mgr._timelineFps = nextTimelineFps;
      mgr._playbackFps = nextPlaybackFps;
      mgr._playing = nextPlaying;

      if (hardSync || !nextPlaying) {
        mgr._anchorMediaTime = desiredMediaTime;
      } else {
        mgr._anchorMediaTime = predictedMediaTime + phaseError * SOFT_SYNC_GAIN;
      }
      mgr._anchorCtxTime = nowCtx;
      mgr._lastTransportCtxTime = nowCtx;

      if (mgr._playing) mgr._resumeContext();
      mgr._syncAllTracks(hardSync);
    },

    _resumeContext: function() {
      if (audioCtx.state === "suspended") {
        audioCtx.resume().catch(function() {});
      }
    },

    _transportRate: function() {
      var timelineFps = positive(mgr._timelineFps, 1.0);
      var playbackFps = positive(mgr._playbackFps, timelineFps);
      return clamp(playbackFps / timelineFps, SOURCE_MIN_RATE, SOURCE_MAX_RATE);
    },

    _maxPredictionWindow: function() {
      var rate = mgr._transportRate();
      if (rate <= 0) return Infinity;
      return HARD_SYNC_THRESHOLD_SEC / rate;
    },

    _isTransportStale: function(ctxTime) {
      if (!mgr._playing) return false;
      return (ctxTime - mgr._lastTransportCtxTime) > mgr._maxPredictionWindow();
    },

    _mediaTimeAt: function(ctxTime) {
      if (!mgr._playing) return mgr._anchorMediaTime;
      var elapsed = Math.max(0, ctxTime - mgr._anchorCtxTime);
      var maxElapsed = Math.max(
        0,
        (mgr._lastTransportCtxTime + mgr._maxPredictionWindow()) - mgr._anchorCtxTime
      );
      elapsed = Math.min(elapsed, maxElapsed);
      return mgr._anchorMediaTime + elapsed * mgr._transportRate();
    },

    _expectedOffset: function(track, ctxTime) {
      if (!track.buffer) return Infinity;
      var timelineFps = positive(mgr._timelineFps, 1.0);
      return mgr._mediaTimeAt(ctxTime) - track.startStep / timelineFps;
    },

    _isPlayableOffset: function(track, offset) {
      return !!track.buffer && offset >= 0 && offset < track.buffer.duration;
    },

    _stopSource: function(track) {
      var source = track.source;
      if (!source) return;

      var nowCtx = audioCtx.currentTime;
      track.sourceStartOffset = mgr._getSourceOffset(track, nowCtx);
      track.sourceStartCtxTime = nowCtx;

      track.source = null;
      source.onended = null;
      try { source.stop(); } catch (_e) {}
      try { source.disconnect(); } catch (_e) {}
    },

    _startSourceAtOffset: function(track, offset, rate) {
      if (!track.buffer) return;

      var source = audioCtx.createBufferSource();
      source.buffer = track.buffer;

      var appliedRate = clamp(rate, SOURCE_MIN_RATE, SOURCE_MAX_RATE);
      try {
        source.playbackRate.value = appliedRate;
      } catch (_e) {
        appliedRate = 1.0;
        source.playbackRate.value = appliedRate;
      }

      source.connect(track.gain);

      var startOffset = clamp(offset, 0, Math.max(0, track.buffer.duration - 1e-6));
      track.source = source;
      track.sourceStartCtxTime = audioCtx.currentTime;
      track.sourceStartOffset = startOffset;
      track.sourceRate = appliedRate;

      source.onended = function() {
        if (track.source === source) {
          track.source = null;
          track.sourceStartOffset = track.buffer ? track.buffer.duration : track.sourceStartOffset;
        }
      };

      try {
        source.start(0, startOffset);
      } catch (_e) {
        track.source = null;
      }
    },

    _getSourceOffset: function(track, ctxTime) {
      if (!track.source) return track.sourceStartOffset;
      var elapsed = Math.max(0, ctxTime - track.sourceStartCtxTime);
      return track.sourceStartOffset + elapsed * track.sourceRate;
    },

    _syncTrack: function(track, forceHard) {
      if (!track.buffer) return;

      var nowCtx = audioCtx.currentTime;
      var transportStale = mgr._isTransportStale(nowCtx);
      var expectedOffset = mgr._expectedOffset(track, nowCtx);
      var shouldPlay = !transportStale && mgr._playing && mgr._isPlayableOffset(track, expectedOffset);
      if (!shouldPlay) {
        mgr._stopSource(track);
        return;
      }

      var baseRate = mgr._transportRate();

      if (!track.source) {
        mgr._resumeContext();
        mgr._startSourceAtOffset(track, expectedOffset, baseRate);
        track.lastHardSyncCtxTime = nowCtx;
        return;
      }

      var actualOffset = mgr._getSourceOffset(track, nowCtx);
      var sourceDrift = expectedOffset - actualOffset;
      var shouldHardSync = forceHard || (
        Math.abs(sourceDrift) > HARD_SYNC_THRESHOLD_SEC &&
        nowCtx - track.lastHardSyncCtxTime > MIN_RESYNC_INTERVAL_SEC
      );
      if (shouldHardSync) {
        mgr._stopSource(track);
        mgr._resumeContext();
        mgr._startSourceAtOffset(track, expectedOffset, baseRate);
        track.lastHardSyncCtxTime = nowCtx;
        return;
      }

      var correction = clamp(sourceDrift * DRIFT_RATE_GAIN, -MAX_RATE_CORRECTION, MAX_RATE_CORRECTION);
      var targetRate = clamp(baseRate * (1 + correction), SOURCE_MIN_RATE, SOURCE_MAX_RATE);
      if (Math.abs(track.sourceRate - targetRate) > 1e-4) {
        track.sourceStartOffset = actualOffset;
        track.sourceStartCtxTime = nowCtx;
        track.sourceRate = targetRate;
        try {
          track.source.playbackRate.value = targetRate;
        } catch (_e) {}
      }
    },

    _syncAllTracks: function(forceHard) {
      var names = Object.keys(mgr.tracks);
      for (var i = 0; i < names.length; i++) {
        var track = mgr.tracks[names[i]];
        if (track) mgr._syncTrack(track, forceHard);
      }
    },

    destroy: function() {
      if (mgr._destroyed) return;
      mgr._destroyed = true;
      mgr._playing = false;

      if (mgr._syncTimer !== null) {
        clearInterval(mgr._syncTimer);
        mgr._syncTimer = null;
      }

      var names = Object.keys(mgr.tracks);
      for (var i = 0; i < names.length; i++) {
        mgr.removeTrack(names[i]);
      }

      if (audioCtx.state !== "closed") {
        audioCtx.close().catch(function() {});
      }
    }
  };

  mgr._syncTimer = setInterval(function() {
    if (mgr._destroyed) return;
    if (mgr._playing) mgr._resumeContext();
    mgr._syncAllTracks(false);
  }, SYNC_INTERVAL_MS);

  window.__viser4d_audio = mgr;
})();
