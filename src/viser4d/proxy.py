from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from .audio import AudioHandle
from .op import CompressionMode, Op, OpKind
from .timeline import Timeline

if TYPE_CHECKING:
    from viser import SceneApi

    from .server import Viser4dServer


class ProxyScene:
    """Context-aware scene proxy.

    Inside an ``at(t)`` context, operations are recorded to the timeline.
    Outside, operations are forwarded to the live viser scene.
    """

    def __init__(
        self,
        server: Viser4dServer,
        timeline: Timeline,
        live_scene: SceneApi,
        lazy_threshold_bytes: int | None,
        compression: CompressionMode,
    ) -> None:
        self._server = server
        self._timeline = timeline
        self._live_scene = live_scene
        self._lazy_threshold_bytes = lazy_threshold_bytes
        self._compression = compression
        self._recording_time: int | None = None

    def add_audio(
        self, name: str, *, data: np.ndarray, sample_rate: int
    ) -> AudioHandle:
        """Add an audio track starting at the current recording timestep."""
        if self._recording_time is None:
            raise RuntimeError("add_audio() must be called inside an at(t) context.")
        return self._server._audio_api.add_track(
            name,
            data,
            sample_rate,
            start_step=self._recording_time,
        )

    def __getattr__(self, name: str) -> Any:
        # Outside recording context - forward to live scene
        if self._recording_time is None:
            return getattr(self._live_scene, name)

        # Inside recording context - record operations
        if name.startswith("add_"):

            def _add(*args: Any, **kwargs: Any) -> ProxyHandle:
                target = self._target_from_add(args, kwargs)
                op = Op.create(
                    kind=OpKind.ADD,
                    target=target,
                    member=name,
                    args=args,
                    kwargs=kwargs,
                    threshold_bytes=self._lazy_threshold_bytes,
                    compression=self._compression,
                )
                self._record(op)
                return ProxyHandle(self, target)

            return _add

        if name == "remove_by_name":

            def _remove(target: str) -> None:
                self._record(
                    Op.create(
                        kind=OpKind.REMOVE,
                        target=target,
                        member=name,
                        threshold_bytes=self._lazy_threshold_bytes,
                        compression=self._compression,
                    )
                )

            return _remove

        # For any other attribute, forward to the live scene even while recording.
        return getattr(self._live_scene, name)

    def _set_time(self, time_step: int | None) -> None:
        self._recording_time = time_step

    def _record(self, op: Op) -> None:
        if self._recording_time is None:
            raise RuntimeError("Cannot record operation when time is not set.")
        t = self._recording_time
        if op.kind is OpKind.ADD:
            self._timeline.record_add(t, op.target, op)
        elif op.kind is OpKind.REMOVE:
            self._timeline.record_remove(t, op.target)
        else:
            self._timeline.record_set(t, op.target, op.member, op.args[0])

    @staticmethod
    def _target_from_add(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        if "name" in kwargs:
            return kwargs["name"]
        if args:
            return args[0]
        raise TypeError(
            "Recorded add_* calls require a node name via the first positional "
            "argument or the 'name' keyword."
        )


class ProxyHandle:
    """Handle proxy that records or forwards attribute access."""

    __slots__ = ("_parent_scene", "_name")

    def __init__(self, parent_scene: ProxyScene, name: str) -> None:
        self._parent_scene = parent_scene
        self._name = name

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return

        if self._parent_scene._recording_time is not None:
            op = Op.create(
                kind=OpKind.SET,
                target=self._name,
                member=name,
                args=(value,),
                threshold_bytes=self._parent_scene._lazy_threshold_bytes,
                compression=self._parent_scene._compression,
            )
            self._parent_scene._record(op)
        else:
            setattr(self._get_live_handle(), name, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get_live_handle(), name)

    def remove(self) -> None:
        if self._parent_scene._recording_time is not None:
            op = Op.create(
                kind=OpKind.REMOVE,
                target=self._name,
                member="remove",
                threshold_bytes=self._parent_scene._lazy_threshold_bytes,
                compression=self._parent_scene._compression,
            )
            self._parent_scene._record(op)
        else:
            self._get_live_handle().remove()

    def _get_live_handle(self) -> Any:
        handle = self._parent_scene._live_scene._handle_from_node_name.get(self._name)
        if handle is None:
            raise RuntimeError(
                f"Handle '{self._name}' not in live scene. "
                "Make sure to call seek() after recording."
            )
        return handle
