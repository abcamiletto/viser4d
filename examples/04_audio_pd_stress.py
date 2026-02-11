"""Stress-test example for audio sync with controlled Python pipeline jitter.

This script generates a synthetic click track and a matching visual pulse.
You can inject periodic callback delays to emulate a slow/uneven Python
pipeline and listen for how smoothly audio stays aligned.

Run:
    uv run examples/04_audio_pd_stress.py --port 8080
    uv run examples/04_audio_pd_stress.py --port 8080 --jitter-ms 0
"""

import argparse
import time

import numpy as np
import viser4d


def make_click_track(
    duration_sec: float,
    sample_rate: int,
    click_period_sec: float,
    click_width_sec: float,
    click_freq_hz: float,
) -> np.ndarray:
    """Build a mono float32 click track."""
    total_samples = int(duration_sec * sample_rate)
    period_samples = max(1, int(click_period_sec * sample_rate))
    width_samples = max(1, int(click_width_sec * sample_rate))

    click_t = np.arange(width_samples, dtype=np.float32) / float(sample_rate)
    click = 0.9 * np.sin(2 * np.pi * click_freq_hz * click_t)
    click *= np.hanning(width_samples).astype(np.float32)

    audio = np.zeros(total_samples, dtype=np.float32)
    for start in range(0, total_samples - width_samples, period_samples):
        audio[start : start + width_samples] += click
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8080)
parser.add_argument("--fps", type=float, default=30.0)
parser.add_argument("--duration-sec", type=float, default=24.0)
parser.add_argument("--jitter-every-steps", type=int, default=20)
parser.add_argument("--jitter-ms", type=float, default=120.0)
args = parser.parse_args()

SAMPLE_RATE = 16000
CLICK_PERIOD_SEC = 0.5
CLICK_WIDTH_SEC = 0.03
CLICK_FREQ_HZ = 1200.0

num_steps = int(args.duration_sec * args.fps)
samples = make_click_track(
    duration_sec=args.duration_sec,
    sample_rate=SAMPLE_RATE,
    click_period_sec=CLICK_PERIOD_SEC,
    click_width_sec=CLICK_WIDTH_SEC,
    click_freq_hz=CLICK_FREQ_HZ,
)

server = viser4d.Viser4dServer(num_steps=num_steps, port=args.port, fps=args.fps)

with server.at(0):
    server.scene.add_audio("/clicks", data=samples, sample_rate=SAMPLE_RATE)


def _inject_pipeline_jitter(step: int) -> None:
    if args.jitter_ms <= 0 or args.jitter_every_steps <= 0:
        return
    if step > 0 and step % args.jitter_every_steps == 0:
        time.sleep(args.jitter_ms / 1000.0)


server.on_timestep_change(_inject_pipeline_jitter)

for step in range(num_steps):
    t = step / args.fps
    phase = (t % CLICK_PERIOD_SEC) / CLICK_PERIOD_SEC
    pulse = max(0.0, 1.0 - phase / 0.25)

    radius = 0.2 + 0.45 * pulse
    n_points = 180
    theta = np.linspace(0.0, 2.0 * np.pi, n_points, endpoint=False)
    x = radius * np.cos(theta)
    y = radius * np.sin(theta)
    z = np.full_like(theta, 0.0)
    points = np.stack([x, y, z], axis=-1).astype(np.float32)

    with server.at(step):
        server.scene.add_point_cloud(
            "/pulse",
            points=points,
            colors=(int(255 * pulse), 40, int(255 * (1.0 - pulse))),
            point_size=0.04,
        )

print(f"Open: http://localhost:{args.port}")
print("Suggested A/B:")
print("  1) --jitter-ms 0")
print(f"  2) --jitter-ms {args.jitter_ms} --jitter-every-steps {args.jitter_every_steps}")

server.play(fps=args.fps, loop=True)
server.sleep_forever()
