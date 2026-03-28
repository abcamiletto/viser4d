import argparse

import numpy as np
import viser4d

parser = argparse.ArgumentParser()
parser.add_argument(
    "--port", type=int, default=8080, help="Port to bind the server to."
)
args = parser.parse_args()

server = viser4d.Viser4dServer(num_steps=10, fps=10, port=args.port)

server.scene.add_frame("/origin", axes_length=0.25)
server.scene.add_grid("/ground", width=10.0, height=10.0)

point_cloud = None
for i in range(10):
    with server.at(i) as timeline:
        points = np.random.uniform(-1.0, 1.0, size=(200, 3))
        if point_cloud is None:
            point_cloud = timeline.scene.add_point_cloud(
                "/points",
                points=points,
                colors=(255, 200, 0),
            )
        else:
            point_cloud.points = points

# Open the viewer and use the Playback controls in the GUI.
server.sleep_forever()
