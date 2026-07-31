# viser4d architecture

viser4d extends [viser](https://github.com/nerfstudio-project/viser) with a recorded
time dimension: scenes are recorded per discrete timestep on the server, and each
browser tab plays the recording back locally (play / pause / scrub / speed / loop),
streaming timeline data in blocks. Timelines can carry synced audio and be exported
as `.viser` recordings or standalone HTML.

viser has no extension API, so viser4d necessarily reaches into viser internals on
both sides (Python server and browser client). The design goal is to make that
coupling *narrow and explicit*, and to make everything else pure and boring.

## The one data model

Everything — recording, storage, streaming, playback, export — is built on a single
canonical model: **keyed scene state**.

Every scene-mutating viser message reduces to a `(key, message)` *put* or a *node
delete*. The scene at any timestep is a map `key -> entry`.

**Keys** (computed in `_state.py`, mirrored nowhere — clients receive `name`
explicitly and never parse keys):

| message shape                    | key                          |
|----------------------------------|------------------------------|
| has `props` (node creation)      | `create:{name}`              |
| `SceneNodeUpdateMessage`         | one put per prop: `update:{name}:{prop}` (message rewritten to carry only that prop) |
| other message with `name`        | `{type}:{name}` (+`:{bone_index}` when the payload has one) |
| message without `name`           | `{type}` (global state)      |

**Entries** are `SceneEntry {key, rev, name, message}`. `rev` is a global
monotonically increasing counter stamped at record time. Two entries are equal iff
their revs are equal — nobody ever deep-compares payloads.

**Fold rules** (`fold(state, ...)`, the only place scene semantics live):

- *delete node `n`*: drop every key whose `name` is `n` or a descendant (`n/...`).
- *put create for `n`*: if `create:{n}` exists, first drop `n`'s **own** non-create
  keys (re-creating a node resets its properties; descendants are preserved, which
  matches viser's client-side upsert). Then store the entry.
- *any other put*: store the entry (last write wins per key).

**A node exists** iff its `create:{name}` key is present.

**Materialize ordering** (turning a state map or delta into a message list that
viser can apply):

1. `RemoveSceneNodeMessage` for deleted nodes, topmost ancestors only.
2. Global (nameless) entries.
3. Nodes sorted parent-before-child (by path depth, then name): create message
   first, then that node's property entries.

**StepDelta** — what one recorded timestep stores:

```
StepDelta { puts: SceneEntry[], deleteNodes: string[], audio: AudioEvent[] }
```

Recording twice into the same step folds into the existing delta (deletes collapse
subtrees and drop shadowed puts; puts replace same-key puts and take a fresh rev).

**Audio** is an ordered event stream (`AudioEvent {rev, message}`), folded into
per-track snapshots at checkpoints:

```
AudioTrack { name, rev, sampleRate, startStep, volume, waveform: float32 (frames, channels) }
```

- `AddAudioMessage` at step `t` creates the track with `startStep = t`.
- `AppendAudioMessage` extends the waveform (startStep unchanged).
- `SetAudioWaveformMessage` replaces the waveform (startStep unchanged).
- `SetAudioVolumeMessage` / `RemoveAudioMessage` do the obvious.
- A track occupies steps `[startStep, startStep + frames / sampleRate * fps)`;
  at transport step `s` the clip offset is `(s - startStep) / fps` seconds and the
  playback rate is the playback speed. Waveforms travel as float32 numpy arrays /
  typed arrays (viser's msgpack handles binary), never base64.

**Blocks & checkpoints.** The timeline is split into blocks of `block_size` steps.
A block's wire payload is:

```
Block { index, checkpointScene: SceneEntry[], checkpointAudio: AudioTrack[], deltas: StepDelta[] }
```

The checkpoint is **exclusive**: the folded state of all deltas of earlier
blocks, i.e. the state *before* this block's `deltas[0]`. State at block offset
`o` = fold(checkpoint, `deltas[0..o]` inclusive, override overlay).

**Overrides.** Writes to timeline handles *outside* `server.at(t)` form a keyed
overlay applied on top of every step (the "tweak a property across the whole
timeline" feature). An override for node `n` applies only where `n` exists.
Overrides are ordinary `SceneEntry`s kept in their own state map; an override
`RemoveSceneNodeMessage` persists as a tombstone that prunes the overlay and
keeps the node deleted wherever it exists. Creating nodes outside `at(t)` is an
error, and so are audio edits outside `at(t)` — scene overrides are the only
out-of-session write path.

**Client diff.** The browser keeps `applied: Map<key, rev>` of what it has pushed
into viser. Forward playback applies deltas directly (a delta *is* the diff).
Any other transition (backward seek, cross-block seek, block reload, override
change) computes the target state map and diffs by rev:

- nodes in `applied` but not target → `RemoveSceneNodeMessage` (topmost only);
- nodes with any stale applied key (key absent from target, or create rev changed)
  → re-push create + all target entries for that node (viser upsert resets props,
  no remove/re-add churn);
- otherwise → push only entries whose rev changed;
- global keys: push on rev change (a vanished global key cannot be unset — known,
  accepted limitation).

When a block changes during live recording the server resends the whole block
(debounced, only to clients holding it); the client diff makes reapplication
minimal and flicker-free.

## Wire protocol

Defined once as viser `Message` dataclasses in `_protocol.py`
(`include_in_scene_serialization=False`), code-generated into
`client/protocol.gen.ts` by `_codegen.py`. Control messages ride viser's existing
websocket and are intercepted client-side before viser processes them; events go
back through viser's normal client→server message path with handlers registered on
the infra server.

Server → client (`TimelineControlMessage`):

| message | fields |
|---|---|
| `TimelineConfigureMessage` | numSteps, blockSize, timelineFps, speed, loop, cacheBytes, manifests |
| `TimelineManifestsMessage` | manifests |
| `TimelineBlockMessage` | index, checkpointScene, checkpointAudio, deltas |
| `TimelineOverrideMessage` | key, rev, name, message |
| `TimelineSeekMessage` | step |
| `TimelinePlayMessage` | speed, loop |
| `TimelinePauseMessage` | — |
| `TimelineSetSpeedMessage` | speed, loop |
| `TimelineClearMessage` | — |
| `TimelineRefreshMessage` | — |

Client → server (`TimelineEventMessage`):

| message | fields |
|---|---|
| `TimelineReadyMessage` | — |
| `TimelineBlockRequestMessage` | index |
| `TimelineBlockDiscardMessage` | index |
| `TimelineTimestepMessage` | step |
| `TimelinePlaybackStateMessage` | isPlaying |
| `TimelineSpeedMessage` | speed |

Manifests: `{index, stepStart, stepStop, byteSize | null}` — the client preload
planner's input. Control messages are buffered server-side per client until
`TimelineReadyMessage` arrives.

## Python package (`src/viser4d/`, flat)

| module | responsibility |
|---|---|
| `__init__.py` | public exports: `Viser4dServer`, `StreamingConfig`, `AudioHandle` |
| `_server.py` | `Viser4dServer(viser.ViserServer)`: config, client session registry, server-wide playback controls, callbacks, export entry points |
| `_recorder.py` | the recording seam: a shadow `SceneApi` whose transport routes messages into the active `at(t)` session or the override path; `TimelineContext`; live-scene name-collision validation; debounced block resend to clients |
| `_state.py` | the canonical model: `StoredMessage` capture/inflate, keys, `SceneState`/`AudioState`, `StepDelta`, fold, materialize ordering |
| `_timeline.py` | `Timeline`: per-step deltas in blocks, zstd+msgpack disk spill with a single-worker flush executor, in-memory LRU checkpoint cache invalidated by rev, block payloads, manifests, in-place resize/clear |
| `_playback.py` | `ClientSession`: per-client protocol bridge (block serving, event dispatch, ready-buffering) and the public playback handle (play/pause/seek/speed/state) |
| `_protocol.py` | wire message dataclasses + payload TypedDicts (single source of truth) |
| `_audio.py` | `AudioApi` / `AudioHandle`, waveform normalization |
| `_export.py` | `.viser`/HTML export via viser's `StateSerializer` |
| `_viser.py` | **the only module that touches viser internals** (see inventory below) |
| `_config.py` | `StreamingConfig` (+ `VISER4D_BLOCK_SIZE`, `VISER4D_CLIENT_CHUNK_CACHE_SIZE` env parsing), validators |
| `_build.py` | client bundle autobuild (nodeenv-sandboxed esbuild; wheels ship `runtime.js`) |
| `_codegen.py` | dataclass → TypeScript interface generation for `protocol.gen.ts` |

Checkpoints are **memory-only** (no checkpoint files, no on-disk revision
validation): a checkpoint is a map of references into stored entries, cached LRU
(default 4), rebuilt from the nearest cached predecessor when stale. Staleness is
one rule: a checkpoint for block `k` built at rev `r` is valid iff no block `< k`
has been written after `r`.

Trade-off, accepted deliberately: a fully cold seek to block `k` re-folds blocks
`0..k-1` (~1.6 s at the far end of a 512-step / 200-object timeline). Sequential
access — the client's preload pattern — extends the previous checkpoint and is
O(1) per block, and every fold along the way populates the cache. In exchange,
the entire on-disk checkpoint subsystem and its stale-file validation are gone.

Threading: user thread records; viser's websocket/event-loop threads serve blocks
and dispatch events. One `RLock` on `Timeline`, one lock per `ClientSession`, one
lock on the session registry. Block flushes go through a dedicated
single-worker executor, so writes per block are naturally ordered.

## Browser runtime (`src/viser4d/client/`)

Injected into viser's stock client via `RunJavascriptMessage` at server start;
bundled by esbuild into `runtime.js` (IIFE, checked in, shipped in wheels).

| module | responsibility |
|---|---|
| `index.ts` | bootstrap; installs on `window.__VISER4D__`, disposes any prior instance |
| `viser.ts` | **all viser-frontend coupling** (see inventory below) |
| `controller.ts` | protocol dispatch and orchestration |
| `state.ts` | mirror of the data model: block decode, fold, rev-diff, materialize ordering, `applied` map |
| `cache.ts` | block cache + preload planner (current+previous required; forward speculative fill under the byte budget; evict the rest; one speculative request in flight) |
| `player.ts` | wall-clock transport: play/pause/seek/speed/loop, rAF tick, crossed-step notification |
| `ui.ts` | **runtime-owned playback bar** (DOM overlay: play/pause, prev/next, scrub slider, step counter, speed, loop) |
| `audio.ts` | Web Audio engine driven by an injected transport clock (shared between live playback and file playback) |
| `filePlayback.ts` | export-mode adapter: syncs audio to viser's native player position (the one remaining DOM scrape, isolated here) |
| `binary.ts` | typed-array decoding |
| `protocol.gen.ts` | generated wire types |

In websocket mode the controller drives everything and shows the playback bar. In
file-playback mode (exported HTML) viser's native player replays the scene; only
`filePlayback.ts` + `audio.ts` run, keeping audio in sync.

## Viser coupling inventory

Python (`_viser.py`) — everything else imports only this module:

- `viser._messages`: `Message`, `RunJavascriptMessage`, `_CreateSceneNodeMessage`
  (isinstance), `Message.as_serializable_dict(binary_buffers=...)`
- `viser._scene_api.SceneApi(owner, thread_executor=, event_loop=)`,
  `SceneApi._owner`, `SceneApi._handle_from_node_name`
- `server._websock_server`: `queue_message`, `register_handler`,
  `unregister_handler`; `client._websock_connection.queue_message`
- `viser.infra.WebsockMessageHandler` as the shadow-transport base
- `StateSerializer._messages/_binary_buffers/_time` (no public equivalent for
  appending pre-serialized messages at chosen timestamps)

Browser (`viser.ts`):

- React fiber walk from `#root` / `__reactContainer$*` to find the viewer context
  (duck-typed on `mutable` + `useGuiConfig` + `guiActions` + `useSceneTree`)
- wrap `viewer.mutable.current.messageQueue.push` — the single inbound seam:
  intercepts `Timeline*` control messages, forwards everything else
- `viewer.mutable.current.sendMessage(message)` — outbound events
- `viewer.messageSource` (`"websocket"` vs file playback)
- `filePlayback.ts` only: `[role='slider'][aria-valuenow]` scrape of viser's
  native player

Discovery failures retry on animation frames and log loudly; there is no silent
degradation.

Target viser: `>=1.0.30,<1.1`. All `Message` subclasses must declare
`include_in_scene_serialization=False`.

## Public API

```python
server = viser4d.Viser4dServer(num_steps=100, fps=30.0,
                               streaming=viser4d.StreamingConfig(...),  # or env vars
                               loop=False, playback_speed=1.0, **viser_kwargs)

with server.at(t) as timeline:       # record into timestep t (non-nestable)
    handle = timeline.scene.add_*(...)   # full viser scene API
    track = timeline.audio.add_track(name, data=..., sample_rate=...)

handle.position = ...                # outside at(t): timeline-wide override
server.scene.add_*(...)              # plain live viser scene, unaffected

server.play() / pause() / refresh()
server.set_playback_speed(s) / set_loop(b) / set_steps(n) / clear()
server.on_timestep_change(cb)        # (client, step), every committed step
server.on_playback_change(cb)        # (client, is_playing)
server.get_client_playback(id) / get_client_playbacks()
server.serialize(start_timestep=..., end_timestep=...) -> bytes   # .viser
server.as_html(dark_mode=..., ...) -> str
server.sleep_forever(); server.stop()
```

## Export

`server.serialize()` seeds viser's `StateSerializer` from the live broadcast
buffer (which includes the injected runtime JS — exported HTML gets audio sync for
free), then walks steps: materialize state-at-step messages (timeline delta +
applicable overrides) and append them with `insert_sleep(1/fps)` between steps.
