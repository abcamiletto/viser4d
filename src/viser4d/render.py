"""Async diff-compute and incremental op-dispatch render pipeline."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .timeline import SceneRenderer, Timeline


class RenderPipeline:
    def __init__(
        self,
        timeline: Timeline,
        renderer: SceneRenderer,
        event_loop: asyncio.AbstractEventLoop,
        on_render_complete: Callable[[int, list[threading.Event]], None],
    ) -> None:
        self._timeline = timeline
        self._renderer = renderer
        self._event_loop = event_loop
        self._on_render_complete = on_render_complete
        self._applied_time: int | None = None
        self._pending_render_time: int | None = None
        self._render_in_flight = False
        self._render_target_time: int | None = None
        self._render_ops: deque[tuple[str, str, Any]] = deque()
        self._render_done_events: list[threading.Event] = []
        self._pending_done_events: list[threading.Event] = []
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="viser4d-render"
        )

    def schedule_render(self, t: int) -> None:
        self._pending_render_time = t
        self._pump()

    def transfer_done_events(self, events: list[threading.Event]) -> None:
        self._pending_done_events.extend(events)

    def reset(self) -> None:
        self._renderer.reset()
        self._applied_time = None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _pump(self) -> None:
        if self._render_ops:
            kind, target, payload = self._render_ops.popleft()
            if kind == "remove":
                self._renderer.remove_node(target)
            elif kind == "create_or_replace":
                self._renderer.create_or_replace_node(target, payload)
            else:
                self._renderer.update_node_members(target, payload)
            self._event_loop.call_soon(self._pump)
            return

        target_time = self._render_target_time
        if target_time is not None:
            self._render_target_time = None
            self._applied_time = target_time
            done_events = list(self._render_done_events)
            self._render_done_events.clear()
            self._on_render_complete(target_time, done_events)

        if self._render_in_flight:
            return

        target_time = self._pending_render_time
        if target_time is None:
            return

        self._pending_render_time = None
        self._render_in_flight = True
        self._render_done_events.extend(self._pending_done_events)
        self._pending_done_events.clear()
        future = self._executor.submit(
            self._timeline.diff_between, self._applied_time, target_time
        )
        future.add_done_callback(
            lambda fut, tt=target_time: self._event_loop.call_soon_threadsafe(
                self._on_diff_ready, tt, fut.result()
            )
        )

    def _on_diff_ready(self, target_time: int, diff: Any) -> None:
        self._render_in_flight = False
        self._render_target_time = target_time
        self._render_ops.extend(
            ("remove", t, None) for t in diff.nodes_to_remove
        )
        self._render_ops.extend(
            ("create_or_replace", t, s)
            for t, s in diff.nodes_to_create_or_replace.items()
        )
        self._render_ops.extend(
            ("update", t, u) for t, u in diff.member_updates.items()
        )
        self._pump()
