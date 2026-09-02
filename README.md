# viser4d

viser4d is a small extension of [`viser`](https://github.com/viser-project/viser)
for recorded 3D scenes with a time dimension.

Features include:

- Record scene updates across discrete timesteps with `with server.at(t):`.
- Play, pause, scrub, and step through the timeline directly in the browser.
- Stream long recordings in chunks and preload nearby timeline blocks for responsive scrubbing.
- Keep playback client-local so different browser tabs can explore the same recording independently.
- Attach timeline-synced audio tracks.
- Export recordings as `.viser` files or standalone HTML.

The goal is to keep viser's live scene API while adding a separate recorded
timeline API for playback and export.

Chunking is controlled by `viser4d.StreamingConfig`. By default,
`Viser4dServer` populates it from two environment variables, both plain
integers: `VISER4D_BLOCK_SIZE` (steps per block, default `32`) and
`VISER4D_CLIENT_CHUNK_CACHE_SIZE` (per-client preload budget in bytes, default
`1000000000`). You can also pass `streaming=viser4d.StreamingConfig(...)` for
one server instance.

Playback controls are drawn by the viser4d runtime as an overlay bar at the
bottom of the viewer (play/pause, scrubbing, stepping, speed, loop); playback
state is client-local, so each browser tab explores the recording
independently.

## Installation

You can install `viser4d` with `pip`:

```bash
pip install viser4d
```

## Quickstart

```python
import numpy as np
import viser4d

server = viser4d.Viser4dServer(num_steps=10, fps=10)

server.scene.add_frame("/origin", axes_length=0.25)
server.scene.add_grid("/ground", width=10.0, height=10.0)

point_cloud = None
for t in range(10):
    with server.at(t) as timeline:
        points = np.random.uniform(-1.0, 1.0, size=(200, 3))
        if point_cloud is None:
            point_cloud = timeline.scene.add_point_cloud(
                "/points",
                points=points,
                colors=(255, 200, 0),
            )
        else:
            point_cloud.points = points

server.sleep_forever()
```

Open the viewer in your browser and use the built-in Playback controls to play,
pause, scrub, and step through the recorded timeline.

Outside `server.at(t)`, `server.scene` behaves like normal `viser`. Inside
`server.at(t)`, writes to `timeline.scene` and `timeline.audio` are recorded
into the timeline.

## More Examples

- [Hello world](examples/00_hello_world.py)
- [Camera path](examples/01_camera_path.py)
- [Multiple timestep batch](examples/02_multiple_timesteps.py)
- [Timeline-synced audio](examples/03_audio.py)
- [Heavy scene stress test](examples/04_heavy_scene.py)

## Export

```python
blob = server.serialize()
html = server.as_html()
```

Use `server.serialize()` to export a `.viser` recording and `server.as_html()`
to export a self-contained HTML viewer.

## Design

Every scene-mutating viser message reduces to a keyed *put* or a *node delete*,
so the scene at any timestep is a map `key -> entry` and each entry carries a
monotonic `rev`. Two entries are identical iff their revs match, so nothing ever
deep-compares payloads.

Each recorded timestep stores only its delta. Deltas are grouped into blocks of
`block_size` steps, spilled to disk as zstd+msgpack, and served to the browser
with a checkpoint: the folded state just before the block's first delta. The
browser folds `checkpoint + deltas[0..offset]`, diffs by rev against what it has
already pushed into viser, and drives playback from its own clock — the server
sends data, not frames.

Writes made outside `server.at(t)` become *overrides*: a keyed overlay applied
on top of every step, where the node exists. Audio is a per-step event stream
folded into per-track waveform snapshots and scheduled against the same
transport clock, in live playback and in exported HTML alike.

Each module's docstring documents its own part in detail; `_state.py` holds the
key derivation table and the fold rules.

## Development

```bash
uv sync --group dev

# Build the browser runtime (only needed for a non-editable checkout).
npm --prefix src/viser4d/client ci
npm --prefix src/viser4d/client run build

uv run --group dev ruff check . && uv run --group dev ruff format --check .
uv run --group dev ty check
uv run --group dev pytest -q
npm --prefix src/viser4d/client run typecheck
```

An editable install rebuilds the runtime automatically when the client sources
or the wire protocol change, using a nodeenv-sandboxed Node. Both
`src/viser4d/runtime.js` and `src/viser4d/client/protocol.gen.ts` are generated
and gitignored; `protocol.gen.ts` comes from `src/viser4d/_protocol.py` via
`uv run python -m viser4d._codegen`.
