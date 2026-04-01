from __future__ import annotations

from typing import Any, cast

import msgspec
import numpy as np
import zstandard

from ._types import StoredMessage

_BINARY_INDEX_KEY = "__binary_index"
_BINARY_LENGTHS_KEY = "binaryBufferLengths"
_DTYPE_KEY = "dtype"


def _byte_view(value: memoryview | bytes | bytearray) -> memoryview:
    view = value if isinstance(value, memoryview) else memoryview(value)
    if not view.c_contiguous:
        view = memoryview(view.tobytes())
    if view.format != "B" or view.itemsize != 1:
        view = view.cast("B")
    return view


def _append_placeholder(
    buffers: list[memoryview],
    data: memoryview | bytes | bytearray,
    *,
    dtype: str,
) -> dict[str, object]:
    byte_view = _byte_view(data)
    buffers.append(byte_view)
    return {
        _BINARY_INDEX_KEY: len(buffers) - 1,
        _DTYPE_KEY: dtype,
    }


def _remap_placeholders(
    value: Any,
    *,
    buffer_offset: int,
    buffer_count: int,
) -> Any:
    if isinstance(value, dict):
        idx = value.get(_BINARY_INDEX_KEY)
        dtype = value.get(_DTYPE_KEY)
        if isinstance(idx, int) and isinstance(dtype, str):
            if not 0 <= idx < buffer_count:
                raise ValueError(f"Binary buffer index {idx} is out of range.")
            return {
                _BINARY_INDEX_KEY: buffer_offset + idx,
                _DTYPE_KEY: dtype,
            }
        return {
            str(key): _remap_placeholders(
                inner,
                buffer_offset=buffer_offset,
                buffer_count=buffer_count,
            )
            for key, inner in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _remap_placeholders(
                item,
                buffer_offset=buffer_offset,
                buffer_count=buffer_count,
            )
            for item in value
        ]
    return value


def _append_stored_message(
    buffers: list[memoryview],
    message: StoredMessage,
) -> dict[str, object]:
    buffer_offset = len(buffers)
    buffers.extend(_byte_view(buffer) for buffer in message.buffers)
    return cast(
        dict[str, object],
        _remap_placeholders(
            message.payload,
            buffer_offset=buffer_offset,
            buffer_count=len(message.buffers),
        ),
    )


def _inflate_placeholders(
    value: object,
    *,
    buffers: tuple[bytes, ...],
) -> object:
    if isinstance(value, dict):
        record = cast(dict[str, object], value)
        idx = record.get(_BINARY_INDEX_KEY)
        dtype = record.get(_DTYPE_KEY)
        if isinstance(idx, int) and isinstance(dtype, str):
            if not 0 <= idx < len(buffers):
                raise ValueError(f"Binary buffer index {idx} is out of range.")
            return np.frombuffer(buffers[idx], dtype=np.dtype(dtype))
        return {
            str(key): _inflate_placeholders(inner, buffers=buffers)
            for key, inner in record.items()
        }
    if isinstance(value, list):
        return [_inflate_placeholders(item, buffers=buffers) for item in value]
    if isinstance(value, tuple):
        return tuple(_inflate_placeholders(item, buffers=buffers) for item in value)
    return value


def inflate_stored_message(message: StoredMessage) -> dict[str, object]:
    return cast(
        dict[str, object],
        _inflate_placeholders(message.payload, buffers=message.buffers),
    )


def inflate_stored_messages(messages: list[StoredMessage]) -> list[dict[str, object]]:
    return [inflate_stored_message(message) for message in messages]


def _to_hybrid_value(value: Any, buffers: list[memoryview]) -> Any:
    if isinstance(value, StoredMessage):
        return _append_stored_message(buffers, value)
    if isinstance(value, dict):
        return {
            str(key): _to_hybrid_value(inner, buffers) for key, inner in value.items()
        }
    if isinstance(value, np.ndarray):
        data = value.data if value.data.c_contiguous else value.copy().data
        return _append_placeholder(
            buffers, cast(memoryview, data), dtype=value.dtype.str
        )
    if isinstance(value, (memoryview, bytes, bytearray)):
        return _append_placeholder(buffers, value, dtype="|u1")
    if isinstance(value, (tuple, list)):
        return [_to_hybrid_value(item, buffers) for item in value]
    return value


def encode_hybrid_document(document: object) -> bytes:
    """Encode a JSON-like value using viser's hybrid placeholder/binary layout."""
    buffers: list[memoryview] = []
    payload = _to_hybrid_value(document, buffers)
    if not isinstance(payload, dict):
        raise TypeError("Hybrid document root must encode to a mapping.")
    if _BINARY_LENGTHS_KEY in payload:
        raise ValueError(f"Reserved key {_BINARY_LENGTHS_KEY!r} is not allowed.")
    if buffers:
        payload = {
            **payload,
            _BINARY_LENGTHS_KEY: [len(buffer) for buffer in buffers],
        }

    packed = msgspec.msgpack.encode(payload)
    out = bytearray()
    out.extend(len(packed).to_bytes(8, "little"))
    out.extend(packed)
    for buffer in buffers:
        out.extend(b"\x00" * (-len(out) % 8))
        out.extend(buffer)
    return bytes(out)


def serialize_zstd_hybrid_document(
    document: object,
    *,
    level: int,
) -> bytes:
    hybrid = encode_hybrid_document(document)
    compressed = zstandard.ZstdCompressor(level=level).compress(hybrid)
    return len(hybrid).to_bytes(8, "little") + compressed
