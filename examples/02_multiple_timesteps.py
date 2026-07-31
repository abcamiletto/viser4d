import argparse

import numpy as np

import viser4d

parser = argparse.ArgumentParser()
parser.add_argument(
    "--port", type=int, default=8080, help="Port to bind the server to."
)
args = parser.parse_args()

server = viser4d.Viser4dServer(num_steps=4, fps=1, port=args.port)

for t in range(4):
    with server.at(t) as timeline:
        timeline.scene.add_point_cloud(
            f"/batch/{t}",
            points=np.random.uniform(-1.0, 1.0, size=(100, 3)),
            colors=(0, 200, 255),
        )

# Open the viewer and use the Playback controls in the GUI.
server.sleep_forever()
