import numpy as np
import viser4d


server = viser4d.ViserServer(num_steps=10)

server.scene.add_frame("/origin", axes_length=0.25)
server.scene.add_grid("/ground", width=10.0, height=10.0)

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
