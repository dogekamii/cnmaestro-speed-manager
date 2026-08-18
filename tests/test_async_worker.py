import asyncio
import threading

from operations_toolkit.core.async_worker import AsyncWorker


def test_async_worker_reuses_one_event_loop_thread_and_closes_idempotently() -> None:
    worker = AsyncWorker()

    async def identity() -> tuple[int, int]:
        return threading.get_ident(), id(asyncio.get_running_loop())

    first = worker.run(identity())
    second = worker.run(identity())
    worker.close()
    worker.close()

    assert first == second
    assert first[0] != threading.get_ident()
