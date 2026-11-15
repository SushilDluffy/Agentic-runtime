"""Single-host polling worker. Run under a process supervisor in deployment."""

import asyncio

from .runtime import Runtime


class Worker:
    def __init__(self, runtime: Runtime, *, concurrency: int = 4, poll_seconds: float = 0.25):
        if concurrency < 1 or poll_seconds <= 0:
            raise ValueError("Concurrency and poll interval must be positive")
        self.runtime = runtime
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds

    async def run_once(self):
        semaphore = asyncio.Semaphore(self.concurrency)

        async def execute(task):
            async with semaphore:
                return await self.runtime.run(task.id)

        tasks = self.runtime.store.list_tasks(
            ["queued", "running", "verifying", "waiting_input", "waiting_approval"]
        )
        return await asyncio.gather(*(execute(task) for task in tasks))

    async def serve(self, stop: asyncio.Event):
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
