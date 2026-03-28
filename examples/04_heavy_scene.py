"""Heavy scene stress example with many constantly changing objects."""

import argparse

import numpy as np
import viser4d

parser = argparse.ArgumentParser()
parser.add_argument(
    "--port", type=int, default=8080, help="Port to bind the server to."
)
parser.add_argument(
    "--num-objects",
    type=int,
    default=300,
    help="Number of animated scene objects.",
)
parser.add_argument(
    "--num-steps",
    type=int,
    default=180,
    help="Number of timesteps in the recorded timeline.",
)
parser.add_argument("--fps", type=float, default=30.0, help="Timeline FPS.")
parser.add_argument(
    "--seed",
    type=int,
    default=7,
    help="Random seed for deterministic motion.",
)
args = parser.parse_args()

rng = np.random.default_rng(args.seed)
num_objects = max(1, args.num_objects)
num_steps = max(2, args.num_steps)

server = viser4d.Viser4dServer(num_steps=num_steps, fps=args.fps, port=args.port)
server.scene.add_grid(
    "/ground", width=12.0, height=12.0, cell_size=0.5, cell_thickness=1
)
server.scene.add_frame("/origin", axes_length=0.2)

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

churn_count = min(30, num_objects)

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

        # Add/remove churn so scene topology also changes while scrubbing.
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

# Open the viewer and use the Playback controls in the GUI.
server.sleep_forever()
