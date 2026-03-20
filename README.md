# viser4d

viser4d is a small wrapper around `viser` that adds a time dimension. It records
scene operations across timesteps, supports timeline-synced audio playback, and
can seek or play them back.

## Quickstart

```bash
pip install viser4d
```

```python
import numpy as np
import viser4d

server = viser4d.Viser4dServer(num_steps=10)

with server.at(0):
    points = np.random.uniform(-1.0, 1.0, size=(200, 3))
    point_cloud = server.scene.add_point_cloud(
        "/points",
        points=points,
        colors=(255, 200, 0),
    )

for i in range(1, 10):
    with server.at(i):
        points = np.random.uniform(-1.0, 1.0, size=(200, 3))
        point_cloud.points = points

server.play(fps=10, loop=True)
server.sleep_forever()
```

## Streaming ingest

If data arrives incrementally, initialize components at `t=0` and then record
updates as each new frame arrives:

```python
import numpy as np
import viser4d

num_steps = 180
server = viser4d.Viser4dServer(num_steps=num_steps)

def get_next_points() -> np.ndarray:
    # Replace with your real sensor/network/pipeline frame source.
    return np.random.normal(size=(400, 3)).astype(np.float32)

with server.at(0):
    point_cloud = server.scene.add_point_cloud(
        "/stream/points",
        points=get_next_points(),
    )

for t in range(1, num_steps):
    points = get_next_points()
    with server.at(t):
        point_cloud.points = points
    server.seek(t)  # optional: keep view synced to latest streamed frame

server.play(fps=30, loop=True)
server.sleep_forever()
```

## Shared timestep callbacks

If you have your own visualization logic and just want to use viser4d's timeline
infrastructure (playback controls, seeking, scrubbing), you can register a
callback that fires whenever the shared server timestep changes:

```python
import viser4d

server = viser4d.Viser4dServer(num_steps=100)

def on_timestep(t: int) -> None:
    # Update your custom visualizations here
    update_video_frames(t)
    update_body_meshes(t)
    update_3d_keypoints(t)

server.on_timestep_change(on_timestep)
server.play(fps=30, loop=True)
server.sleep_forever()
```

Callbacks are invoked after viser4d applies its own recorded state, so you can
mix both approaches - record some operations with `at(t)` and handle others via
callbacks.

`server.on_timestep_change(...)` follows the shared server transport driven by
`server.seek(...)` and `server.play(...)`.

## Client-local timestep callbacks

The built-in playback controls shown in each browser tab are client-local. If
you want lazy updates that follow a specific client's Play button, scrubbing, or
step controls, register a client callback instead:

```python
import viser
import viser4d

server = viser4d.Viser4dServer(num_steps=100)

def on_client_timestep(client: viser.ClientHandle, t: int) -> None:
    update_video_frame(client.scene, t)
    update_client_overlays(client.scene, t)

server.on_client_timestep_change(on_client_timestep)
server.sleep_forever()
```

These callbacks fire once per client after that client's timeline runtime
commits a new discrete timestep.

## Serialize `.viser` recordings

To serialize the full viser4d timeline, including audio, use `server.serialize()`:

```python
import viser4d

server = viser4d.Viser4dServer(num_steps=100)
# ... record timeline data ...
blob = server.serialize(start_timestep=0, end_timestep=None)
```

Write the returned bytes to disk yourself if needed.

## Streaming audio append

For audio that arrives incrementally, create a track once inside `at(t)` and
append chunks through the returned handle:

```python
import numpy as np
import viser4d

server = viser4d.Viser4dServer(num_steps=300, fps=30)

with server.at(0):
    audio = server.audio.add_track(
        "/stream/audio",
        data=np.zeros(1600, dtype=np.float32),
        sample_rate=16000,
    )

for _ in range(120):
    chunk = np.random.uniform(-0.05, 0.05, size=(1600,)).astype(np.float32)
    audio.append(chunk)
```

`AudioHandle.append(...)` extends the same track contiguously (same channel
count).

## How it works

Context determines behavior. `server.scene` is viser's normal scene API, but
its websocket target is swapped while you're inside an `at(t)` context:

```
Inside at(t):                          Outside at(t):
─────────────                          ──────────────
scene.add_frame(...)                   scene.add_frame(...)
       │                                      │
       ▼                                      ▼
    records to Timeline                    forwards to live viser scene
```

- **Inside `at(t)`**: Operations are recorded to a timeline, not executed.
- **Outside `at(t)`**: Operations forward directly to viser's live scene.
- **Playback**: `seek(t)` or `play()` applies recorded state to the live scene.
- **Audio**: Add timeline-synced tracks with `server.audio.add_track(...)`.

See `examples/` for more.

## Quality checks

```bash
uvx ruff format .
uvx ruff check .
uvx ty check
pnpm run typecheck:runtime
pnpm run build:runtime
```

## Tests

```bash
pnpm run build:runtime
uv run --group dev pytest -q
```
