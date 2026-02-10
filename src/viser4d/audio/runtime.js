(function() {
  // Clean up any previous instance (e.g., after server restart while
  // the browser tab stays open).
  var old = window.__viser4d_audio;
  if (old) {
    old.pause();
    for (var k in old.tracks) old.removeTrack(k);
  }
  var needsResume = true;
  var mgr = {
    tracks: {},
    _playing: false,
    _currentStep: 0,
    _fps: 30,

    addTrack: function(name, base64Wav, startStep, volume) {
      var binary = atob(base64Wav);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      var blob = new Blob([bytes], {type: "audio/wav"});
      var audio = new Audio(URL.createObjectURL(blob));
      audio.preload = "auto";
      audio.volume = volume;
      mgr.tracks[name] = {audio: audio, startStep: startStep};
    },

    removeTrack: function(name) {
      var t = mgr.tracks[name];
      if (!t) return;
      t.audio.pause();
      URL.revokeObjectURL(t.audio.src);
      delete mgr.tracks[name];
    },

    setVolume: function(name, vol) {
      var t = mgr.tracks[name];
      if (t) t.audio.volume = vol;
    },

    // Called by the server when a track's start_step is reached during playback.
    startTrack: function(name) {
      var t = mgr.tracks[name];
      if (!t || !mgr._playing) return;
      t.audio.currentTime = 0;
      t.audio.play().then(function() {
        needsResume = false;
      }).catch(function() {});
    },

    play: function(currentStep, fps) {
      mgr._playing = true;
      mgr._currentStep = currentStep;
      mgr._fps = fps;
      mgr._startAll();
    },

    pause: function() {
      mgr._playing = false;
      for (var name in mgr.tracks) {
        mgr.tracks[name].audio.pause();
      }
    },

    seek: function(step, fps) {
      mgr._playing = false;
      mgr._currentStep = step;
      mgr._fps = fps;
      for (var name in mgr.tracks) {
        var t = mgr.tracks[name];
        var offset = (step - t.startStep) / fps;
        t.audio.pause();
        if (offset >= 0 && offset < t.audio.duration) {
          t.audio.currentTime = offset;
        }
      }
    },

    // Start tracks that should already be playing at the current step.
    // Tracks whose start_step is in the future are left paused — the server
    // will call startTrack() when their time comes.
    _startAll: function() {
      for (var name in mgr.tracks) {
        var t = mgr.tracks[name];
        var offset = (mgr._currentStep - t.startStep) / mgr._fps;
        if (offset < 0 || offset >= t.audio.duration) {
          t.audio.pause();
          continue;
        }
        t.audio.currentTime = offset;
        t.audio.play().then(function() {
          needsResume = false;
        }).catch(function() {});
      }
    }
  };

  // Browsers block audio.play() until a user gesture. On the first
  // interaction, retry playback if it was requested.
  document.addEventListener("pointerdown", function() {
    if (needsResume && mgr._playing) mgr._startAll();
  });

  window.__viser4d_audio = mgr;
})();
