"""Timestep callback registration and thread-pool dispatch."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class CallbackManager:
    def __init__(self, workers: int = 64) -> None:
        self._callbacks: list[Callable[[int], None]] = []
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers),
            thread_name_prefix="viser4d-callbacks",
        )

    @property
    def thread_pool(self) -> ThreadPoolExecutor:
        return self._executor

    def register(self, callback: Callable[[int], None]) -> None:
        self._callbacks.append(callback)

    def fire(
        self, t: int, done_events: list[threading.Event] | None = None
    ) -> None:
        futures = [
            self._executor.submit(cb, t) for cb in tuple(self._callbacks)
        ]
        if done_events:
            if not futures:
                for ev in done_events:
                    ev.set()
            else:

                def _wait_and_signal() -> None:
                    for f in futures:
                        f.result()
                    for ev in done_events:
                        ev.set()

                self._executor.submit(_wait_and_signal)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._callbacks.clear()
