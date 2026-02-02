import numpy as np
import viser4d

server = viser4d.ViserServer(num_steps=4)

for t in range(4):
    with server.at(t):
        server.scene.add_point_cloud(
            f"/batch/{t}",
            points=np.random.uniform(-1.0, 1.0, size=(100, 3)),
            colors=(0, 200, 255),
        )

server.play(fps=1, loop=True)
server.sleep_forever()
