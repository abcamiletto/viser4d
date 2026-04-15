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

Chunking is controlled by `viser4d.ChunkStreamingConfig`. By default,
`Viser4dServer` populates it from `VISER4D_BLOCK_SIZE` and
`VISER4D_CLIENT_CHUNK_CACHE_SIZE`, falling back to `32` steps per block and a
`1GB` per-client preload budget. You can also pass
`chunk_streaming=viser4d.ChunkStreamingConfig(...)` for one server instance.

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
