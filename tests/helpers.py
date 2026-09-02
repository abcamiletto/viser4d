"""Helpers shared by the viser4d test modules."""

from typing import cast

import msgspec
import zstandard


def deserialize_recording(blob: bytes) -> dict[str, object]:
    """Decode a ``server.serialize()`` blob into viser's recording dict."""
    inner_size = int.from_bytes(blob[:8], "little")
    inner = zstandard.ZstdDecompressor().decompress(
        blob[8:], max_output_size=inner_size
    )
    assert len(inner) == inner_size
    msgpack_size = int.from_bytes(inner[:8], "little")
    return cast(dict[str, object], msgspec.msgpack.decode(inner[8 : 8 + msgpack_size]))
