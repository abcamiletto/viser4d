# viser4d

viser4d is a small wrapper around `viser` that adds a time dimension. It records
scene operations across timesteps and can seek or play them back.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      ViserServer                        │
│  - Owns the timeline and playback state                 │
│  - Provides the public API (at, play, pause, seek)      │
└─────────────────────────────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│    Timeline     │ │  SceneRenderer  │ │ PlaybackControls│
│                 │ │                 │ │                 │
│ - Operations by │ │ - Apply ops to  │ │ - GUI widgets   │
│   timestep      │ │   live scene    │ │ - Event handlers│
│ - Store temporal│ │ - Track render  │ │                 │
│   data          │ │   state         │ │                 │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

## Quickstart

```bash
pip install -e .
```

```python
import numpy as np
import viser4d

server = viser4d.ViserServer(num_steps=10)

point_cloud = None
for i in range(10):
    with server.at(i):
        points = np.random.uniform(-1.0, 1.0, size=(200, 3))
        if point_cloud is None:
            point_cloud = server.scene.add_point_cloud(
                "/points",
                points=points,
                colors=(255, 200, 0),
            )
        else:
            point_cloud.points = points

server.play(fps=10, loop=True)
server.sleep_forever()
```

## Quality checks

```bash
uvx ruff format .
uvx ruff check .
uvx ty check
```

## Tests

```bash
uv run --group dev pytest -q
```

## How it works

- `server.at(t)` sets the active timestep for recording.
- `server.scene.add_*` calls are recorded instead of executed immediately.
- Attribute assignments on returned handles (like `handle.position = ...`) are
  recorded as updates.
- `server.seek(t)` rebuilds the live scene from all recorded ops up to `t`.

See `examples/` for more.
