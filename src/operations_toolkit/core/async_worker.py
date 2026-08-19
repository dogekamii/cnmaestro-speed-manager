from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncWorker:
    """Own one event loop thread for all live async client operations."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._closed = False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="operations-toolkit-async", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("async worker is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def run(self, coroutine: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        return self.submit(coroutine).result(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        if threading.current_thread() is not self._thread:
            self._thread.join()
