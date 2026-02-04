# viser4d

viser4d is a small wrapper around `viser` that adds a time dimension. It records
scene operations across timesteps and can seek or play them back.

## Quickstart

```bash
pip install -e .
```

```python
import numpy as np
import viser4d

server = viser4d.Viser4dServer(num_steps=10)

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

## Timestep callbacks

If you have your own visualization logic and just want to use viser4d's timeline
infrastructure (playback controls, seeking, scrubbing), you can register a
callback that fires whenever the timestep changes:

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

## How it works

- `server.at(t)` sets the active timestep for recording.
- `server.scene.add_*` calls are recorded instead of executed immediately.
- Attribute assignments on returned handles (like `handle.position = ...`) are
  recorded as updates.
- `server.seek(t)` rebuilds the live scene from all recorded ops up to `t`.

See `examples/` for more.

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
