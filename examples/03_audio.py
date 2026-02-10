import argparse

import numpy as np
import viser4d

parser = argparse.ArgumentParser()
parser.add_argument(
    "--port", type=int, default=8080, help="Port to bind the server to."
)
args = parser.parse_args()

server = viser4d.Viser4dServer(num_steps=60, port=args.port)
server.scene.add_grid("/ground", width=4.0, height=4.0)

sample_rate = 16_000
beacon = None
for t in range(server.num_steps):
    with server.at(t):
        angle = 2.0 * np.pi * t / server.num_steps
        if beacon is None:
            beacon = server.scene.add_frame("/beacon", axes_length=0.2)
        beacon.position = (float(np.cos(angle)), float(np.sin(angle)), 0.3)

        if t % 15 == 0:
            random_audio = (0.1 * np.random.randn(sample_rate // 4)).astype(np.float32)
            server.scene.add_audio(random_audio, sample_rate=sample_rate)

server.play(fps=10, loop=True)
server.sleep_forever()
