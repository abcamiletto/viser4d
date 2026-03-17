# viser4d-blender

`viser4d-blender` converts a strict supported subset of `.viser` recordings into
`.blend` files through `bpy`.

It is packaged separately from `viser4d` because it requires `numpy<2` and
`bpy>5.0.0`.

Quick manual check:

```bash
cd packages/viser4d-blender
uv run --python 3.11 viser4d-to-blend \
  tests/assets/blender_showcase.viser \
  /tmp/blender-showcase.blend \
  --overwrite
```
