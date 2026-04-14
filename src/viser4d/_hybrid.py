from __future__ import annotations

from typing import Any, cast

import numpy as np

from ._types import (
    RuntimePayload,
    RuntimeValue,
    StoredMessage,
    StoredPayload,
)

_BINARY_INDEX_KEY = "__binary_index"
_DTYPE_KEY = "dtype"


def _byte_view(value: memoryview | bytes | bytearray) -> memoryview:
    view = value if isinstance(value, memoryview) else memoryview(value)
    if not view.c_contiguous:
        view = memoryview(view.tobytes())
    if view.format != "B" or view.itemsize != 1:
        view = view.cast("B")
    return view


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


def stored_message_as_serializable_dict(
    message: StoredMessage,
    *,
    binary_buffers: list[memoryview],
) -> StoredPayload:
    buffer_offset = len(binary_buffers)
    binary_buffers.extend(_byte_view(buffer) for buffer in message.buffers)
    return cast(
        StoredPayload,
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
) -> RuntimeValue:
    if isinstance(value, dict):
        record = cast(StoredPayload, value)
        idx = record.get(_BINARY_INDEX_KEY)
        dtype = record.get(_DTYPE_KEY)
        if isinstance(idx, int) and isinstance(dtype, str):
            if not 0 <= idx < len(buffers):
                raise ValueError(f"Binary buffer index {idx} is out of range.")
            return cast(
                RuntimeValue,
                np.frombuffer(buffers[idx], dtype=np.dtype(dtype)),
            )
        return {
            str(key): _inflate_placeholders(inner, buffers=buffers)
            for key, inner in record.items()
        }
    if isinstance(value, list):
        return [_inflate_placeholders(item, buffers=buffers) for item in value]
    return cast(RuntimeValue, value)


def inflate_stored_message(message: StoredMessage) -> RuntimePayload:
    return cast(
        RuntimePayload,
        _inflate_placeholders(message.payload, buffers=message.buffers),
    )


def inflate_stored_messages(messages: list[StoredMessage]) -> list[RuntimePayload]:
    return [inflate_stored_message(message) for message in messages]
