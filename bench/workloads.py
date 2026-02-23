from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile

import numpy as np

import viser4d


@dataclass(frozen=True)
class SceneProfile:
    name: str
    num_objects: int
    num_steps: int
    churn_count: int
    seed: int = 7


@dataclass
class BuiltScene:
    server: viser4d.Viser4dServer
    profile: SceneProfile


HEAVY = SceneProfile(
    name="heavy",
    num_objects=300,
    num_steps=180,
    churn_count=30,
)

VERY_HEAVY = SceneProfile(
    name="very_heavy",
    num_objects=600,
    num_steps=240,
    churn_count=40,
)


def create_server(num_steps: int) -> viser4d.Viser4dServer:
    return viser4d.Viser4dServer(
        num_steps=num_steps,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
    )


def build_heavy_scene(profile: SceneProfile) -> BuiltScene:
    server = create_server(profile.num_steps)
    rng = np.random.default_rng(profile.seed)
    num_objects = profile.num_objects
    num_steps = profile.num_steps
    churn_count = min(profile.churn_count, num_objects)

    base = rng.uniform(-2.5, 2.5, size=(num_objects, 3)).astype(np.float32)
    phase = rng.uniform(0.0, 2.0 * np.pi, size=num_objects).astype(np.float32)
    speed = rng.uniform(0.4, 1.8, size=num_objects).astype(np.float32)
    amp = rng.uniform(0.05, 0.35, size=num_objects).astype(np.float32)

    handles = []
    with server.at(0):
        for i in range(num_objects):
            handle = server.scene.add_frame(
                f"/heavy/{i}",
                axes_length=0.045,
                axes_radius=0.0025,
                origin_radius=0.003,
            )
            handle.position = base[i]
            handles.append(handle)

    for step in range(num_steps):
        t = (step / num_steps) * (2.0 * np.pi)
        with server.at(step):
            for i, handle in enumerate(handles):
                theta = t * speed[i] + phase[i]
                oscillation = amp[i]
                x = base[i, 0] + oscillation * np.cos(theta)
                y = base[i, 1] + oscillation * np.sin(1.7 * theta)
                z = base[i, 2] + 0.25 * oscillation * np.sin(2.3 * theta)
                handle.position = (x, y, z)
                handle.wxyz = (np.cos(theta * 0.5), 0.0, np.sin(theta * 0.5), 0.0)
                handle.visible = ((step + i) % 13) != 0

            if step % 4 == 0:
                for i in range(churn_count):
                    name = f"/churn/{i}"
                    server.scene.remove_by_name(name)
                    pulse = server.scene.add_frame(
                        name,
                        axes_length=0.07,
                        axes_radius=0.003,
                        origin_radius=0.004,
                    )
                    pulse.position = (
                        3.0 * np.cos(t + i * 0.1),
                        3.0 * np.sin(t + i * 0.1),
                        0.2 * np.sin(2.0 * t + i * 0.2),
                    )

    return BuiltScene(server=server, profile=profile)


def teardown_scene(scene: BuiltScene) -> None:
    scene.server.stop()


def bench_build_only(profile: SceneProfile) -> None:
    scene = build_heavy_scene(profile)
    teardown_scene(scene)


def bench_seek_forward(scene: BuiltScene) -> None:
    for step in range(scene.profile.num_steps):
        scene.server.seek(step, blocking=True)


def bench_seek_scrub(scene: BuiltScene) -> None:
    rng = np.random.default_rng(42)
    seq = rng.integers(0, scene.profile.num_steps, size=200, endpoint=False)
    for step in seq:
        scene.server.seek(int(step), blocking=True)


def bench_serialize(scene: BuiltScene) -> None:
    with tempfile.TemporaryDirectory(prefix="viser4d_bench_") as d:
        path = Path(d) / "out.viser"
        scene.server.serialize(path)

