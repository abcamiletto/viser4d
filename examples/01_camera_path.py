import argparse

import numpy as np
import viser4d
from viser import transforms as tf

parser = argparse.ArgumentParser()
parser.add_argument(
    "--port", type=int, default=8080, help="Port to bind the server to."
)
args = parser.parse_args()

server = viser4d.Viser4dServer(num_steps=60, port=args.port)
server.scene.add_frame("/origin", axes_length=0.25)

frustum = None

for i in range(60):
    angle = i * (2.0 * np.pi / 60.0)
    position = np.array([2.0 * np.cos(angle), 2.0 * np.sin(angle), 1.0])
    wxyz = tf.SO3.from_z_radians(angle).wxyz
    with server.at(i) as timeline:
        if frustum is None:
            frustum = timeline.scene.add_camera_frustum(
                "/camera",
                fov=60.0,
                aspect=16 / 9,
                scale=0.25,
            )
        frustum.position = position
        frustum.wxyz = wxyz

# Open the viewer and use the Playback controls in the GUI.
server.sleep_forever()
