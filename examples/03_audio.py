"""Audio example — Apollo 11 speech synced to an animated point cloud.

Loads a bundled WAV of Neil Armstrong's "one small step" (public domain,
NASA) and attaches it partway through the timeline so it's shorter than
the loop. A volume slider lets you control playback from the GUI.
"""

import argparse
import io
import wave
from pathlib import Path

import numpy as np
import viser4d

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8080)
args = parser.parse_args()

# -- Load audio from bundled wav -----------------------------------------------
wav_path = Path(__file__).parent / "assets" / "apollo11_one_small_step.wav"
with wave.open(io.BytesIO(wav_path.read_bytes()), "rb") as wf:
    sample_rate = wf.getframerate()
    raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)

audio_duration = len(samples) / sample_rate
print(f"Audio: {audio_duration:.1f}s at {sample_rate} Hz")

# -- Timeline parameters ------------------------------------------------------
FPS = 30
TOTAL_DURATION = audio_duration * 2  # loop is 2x longer than clip
NUM_STEPS = int(TOTAL_DURATION * FPS)
AUDIO_START_STEP = 0

# -- Server + audio ------------------------------------------------------------
server = viser4d.Viser4dServer(num_steps=NUM_STEPS, port=args.port)

with server.at(AUDIO_START_STEP):
    audio_handle = server.audio.add_track(
        "/speech", data=samples, sample_rate=sample_rate
    )

# -- Volume slider -------------------------------------------------------------
volume_slider = server.gui.add_slider(
    "Volume", min=0.0, max=1.0, step=0.01, initial_value=1.0
)


@volume_slider.on_update
def _on_volume(event) -> None:
    audio_handle.volume = event.target.value


# -- Animated point cloud ------------------------------------------------------
for step in range(NUM_STEPS):
    frac = step / NUM_STEPS
    angle = frac * 4 * np.pi
    radius = 0.5 + frac * 1.5
    n_points = 200
    theta = np.linspace(0, 2 * np.pi, n_points)
    x = radius * np.cos(theta + angle)
    y = radius * np.sin(theta + angle)
    z = np.full(n_points, frac * 2 - 1)
    points = np.stack([x, y, z], axis=-1).astype(np.float32)

    with server.at(step):
        server.scene.add_point_cloud(
            "/cloud",
            points=points,
            colors=(0, int(200 * (1 - frac)), int(255 * frac)),
            point_size=0.05,
        )

server.play(fps=FPS, loop=True)
server.sleep_forever()
