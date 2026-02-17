"""Timestep callback registration and thread-pool dispatch."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class CallbackManager:
    """Manages timestep callback registration and dispatch.

    Callbacks are submitted to a dedicated thread pool so they never block
    the server event loop.
    """

    def __init__(self, workers: int = 64) -> None:
        self._callbacks: list[Callable[[int], None]] = []
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers),
            thread_name_prefix="viser4d-callbacks",
        )

    @property
    def thread_pool(self) -> ThreadPoolExecutor:
        """Return the callback thread pool."""
        return self._executor

    def register(self, callback: Callable[[int], None]) -> None:
        """Register a callback to be invoked when the timestep changes."""
        self._callbacks.append(callback)

    def fire(
        self, t: int, done_events: list[threading.Event] | None = None
    ) -> None:
        """Submit all registered callbacks to the thread pool.

        If *done_events* are provided they are signalled after every callback
        future has completed.
        """
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
        """Shut down the thread pool and clear callbacks."""
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._callbacks.clear()
