# viser4d

viser4d is a small wrapper around `viser` that adds a time dimension. It records
scene operations across timesteps, supports timeline-synced audio playback, and
plays them back client-locally in each browser tab.

## Quickstart

```bash
pip install viser4d
```

```python
import numpy as np
import viser4d

server = viser4d.Viser4dServer(num_steps=10, fps=10)

with server.at(0) as timeline:
    points = np.random.uniform(-1.0, 1.0, size=(200, 3))
    point_cloud = timeline.scene.add_point_cloud(
        "/points",
        points=points,
        colors=(255, 200, 0),
    )

for i in range(1, 10):
    with server.at(i):
        points = np.random.uniform(-1.0, 1.0, size=(200, 3))
        point_cloud.points = points

server.sleep_forever()
```

Open the viewer in your browser and use the built-in Playback controls to play,
scrub, and step through the client-local timeline.

## Timeline model

- The built-in browser controls (`Play`, `Pause`, `Prev`, `Next`, and the
  `Timestep` slider) are client-local. Different tabs can be on different
  timesteps at the same time, and those controls are handled directly in the
  browser rather than round-tripping through Python.
- The `fps=` passed to `Viser4dServer(...)` defines the timeline step rate used
  for audio timing and `.viser` export. Client playback speed is expressed as a
  `speed` factor on top of that base rate.
- `server.on_timestep_change(...)` fires whenever any client commits a new
  discrete timestep and passes `(client, timestep)`. With multiple clients, it
  is an aggregate event stream and may repeat timesteps or arrive out of order.
- `server.on_playback_change(...)` fires whenever a client reports that its
  built-in transport changed between playing and paused, and passes
  `(client, is_playing)`.
- `server.play(...)` and `server.pause()` broadcast playback commands to the
  clients that are connected right now. They do not create a shared server
  clock or a persistent server-side playback speed.

## Streaming ingest

If data arrives incrementally, initialize components at `t=0` and then record
updates as each new frame arrives:

```python
import numpy as np
import viser4d

num_steps = 180
server = viser4d.Viser4dServer(num_steps=num_steps, fps=30)

def get_next_points() -> np.ndarray:
    # Replace with your real sensor/network/pipeline frame source.
    return np.random.normal(size=(400, 3)).astype(np.float32)

with server.at(0) as timeline:
    point_cloud = timeline.scene.add_point_cloud(
        "/stream/points",
        points=get_next_points(),
    )

for t in range(1, num_steps):
    points = get_next_points()
    with server.at(t):
        point_cloud.points = points

server.sleep_forever()
```

## Timestep callbacks

If you have your own visualization logic and just want to use viser4d's timeline
infrastructure, you can register a callback that fires whenever any connected
client commits a new discrete timestep:

```python
import viser
import viser4d

server = viser4d.Viser4dServer(num_steps=100)

def on_timestep(client: viser.ClientHandle, t: int) -> None:
    update_video_frame(client.scene, t)
    update_client_overlays(client.scene, t)

server.on_timestep_change(on_timestep)
server.sleep_forever()
```

With multiple clients, this callback is aggregate: if two tabs both visit
timestep `3`, it will fire twice, once for each client.

## Playback state callbacks

If you need to know when a client starts or stops playback, use the playback
callback and the per-client playback handles. Use
`server.get_client_playback(client_id)` for direct lookup, or
`server.get_client_playbacks()` to snapshot all connected clients:

```python
import viser
import viser4d

server = viser4d.Viser4dServer(num_steps=100)

def on_playback_change(client: viser.ClientHandle, is_playing: bool) -> None:
    print(client.client_id, is_playing)
    playback = server.get_client_playback(client.client_id)
    if playback is not None:
        print(playback.current_timestep, playback.speed)

server.on_playback_change(on_playback_change)

# Snapshot of connected playback handles keyed by client id.
for client_id, playback in server.get_client_playbacks().items():
    print(
        client_id,
        playback.is_playing,
        playback.current_timestep,
        playback.speed,
    )
```

`ClientPlaybackHandle.is_playing` reflects the last play/pause state reported by
that browser tab. `server.play(...)` and `server.pause()` send commands, but the
handle state only changes once the client reports the result back.
`ClientPlaybackHandle.speed` is the tab's current playback-speed factor. If you
need the effective playback FPS, compute `server.fps * playback.speed`.

Each handle also exposes per-client control methods that mirror the server-wide
commands but apply only to that one tab:

```python
playback.seek(t)            # jump to a specific timestep
playback.play(speed=2.0)    # start playback at 2× speed
playback.pause()            # pause
playback.set_speed(0.5)     # update speed without starting playback
playback.refresh()          # redraw current timestep from recorded state
```

## Server playback commands

`server.play(speed=..., loop=...)` starts each connected client from that
client's own current timestep. Omitting `speed` preserves each client's current
speed; passing it overrides the connected clients only. `loop=True` enables
looping; omitting it preserves each client's current loop state.
`server.pause()` pauses each connected client wherever it currently is.
`server.set_playback_speed(...)` updates speed without starting playback.
`server.refresh()` redraws the current timestep on all connected clients, which
is useful after updating recorded scene data while paused.
None of these change the base timeline step rate used for audio timing or
export; set that with `fps=` when you construct the server. New clients always
start paused at timestep `0` with speed `1.0`.

## Export recordings

To export a `.viser` recording, use `server.serialize()`:

```python
import viser4d

server = viser4d.Viser4dServer(num_steps=100)
# ... record timeline data ...
blob = server.serialize(start_timestep=0, end_timestep=None)
```

To export a standalone HTML viewer, use `server.as_html()`:

```python
import viser4d

server = viser4d.Viser4dServer(num_steps=100)
# ... record timeline data ...
html = server.as_html(start_timestep=0, end_timestep=None)
```

## Streaming audio append

For audio that arrives incrementally, create a track once inside `at(t)` and
append chunks through the returned handle:

```python
import numpy as np
import viser4d

server = viser4d.Viser4dServer(num_steps=300, fps=30)

with server.at(0) as timeline:
    audio = timeline.audio.add_track(
        "/stream/audio",
        data=np.zeros(1600, dtype=np.float32),
        sample_rate=16000,
    )

for _ in range(120):
    chunk = np.random.uniform(-0.05, 0.05, size=(1600,)).astype(np.float32)
    audio.append(chunk)
```

`AudioHandle.append(...)` extends the same track contiguously (same channel
count). `AudioHandle.volume` is a readable and writable float in `[0, 1]` that
controls playback gain, useful for attaching a GUI slider.

## How it works

**Server-side recording.** `server.scene` is always viser's live/static scene
API. Timeline writes go through the explicit object returned by `server.at(t)`:

```
with server.at(t) as timeline:         Outside at(t):
    timeline.scene.add_frame(...)      server.scene.add_frame(...)
    timeline.audio.add_track(...)             │
           │                                  ▼
           ▼                               updates live viser scene
      records to timeline
```

Recorded messages are grouped into fixed-size blocks in the timeline store.

**Client-side playback.** When a browser tab connects, the server injects a
JavaScript runtime (`TimelineRuntime`) alongside the normal viser viewer. Each
tab manages its own independent transport: play, pause, seek, and speed are
all client-local. The runtime fetches timeline blocks from the server on demand
— only the blocks needed for the current playback position are requested. At
each timestep the runtime replays the recorded viser messages for that step
(and any prior steps in the same block that haven't been applied yet), keeping
the rendered scene in sync with the timeline position.

- **Inside `at(t)`**: Use `timeline.scene` and `timeline.audio` from `with server.at(t) as timeline:`.
- **Outside `at(t)`**: `server.scene` remains viser's live/static scene API.
- **Timeline handles after recording**: mutating a scene handle returned from `timeline.scene.add_*()` outside `at(t)` applies a persistent global override across playback and export. Creating new timeline nodes still requires `at(t)`.
- **Client playback**: Each browser tab owns its own transport and playback state.
- **Block streaming**: Timeline data is fetched block-by-block as the client plays or scrubs.
- **Timestep callbacks**: `on_timestep_change(...)` aggregates committed client steps and passes the source client.
- **Playback callbacks**: `on_playback_change(...)` reports per-client play/pause transitions.
- **Audio**: Add timeline-synced tracks with `timeline.audio.add_track(...)` inside `at(t)`.

See `examples/` for more.

## Quality checks

```bash
uvx ruff format .
uvx ruff check .
uvx ty check
npm --prefix src/viser4d/client run typecheck
npm --prefix src/viser4d/client run build
```

## Tests

```bash
npm --prefix src/viser4d/client run build
uv run --group dev pytest -q
```
