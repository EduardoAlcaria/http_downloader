"""Download queue with a global concurrency cap.

Holds every :class:`Download` the user has added and runs at most
``max_parallel`` at a time (each of which itself fans out to N chunk workers).
A single shared :class:`httpx.AsyncClient` gives connection pooling + HTTP/2
reuse across all downloads.
"""

from __future__ import annotations

import asyncio

import httpx

from .downloader import Download, Status


class DownloadQueue:
    def __init__(self, dest_dir: str, max_parallel: int = 3) -> None:
        self.dest_dir = dest_dir
        self.downloads: list[Download] = []
        self._sem = asyncio.Semaphore(max_parallel)
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=max_parallel * 16),
        )
        self._tasks: set[asyncio.Task] = set()

    def add(self, url: str, *, sha256: str | None = None) -> Download:
        dl = Download(url, self.dest_dir, sha256=sha256)
        self.downloads.append(dl)
        task = asyncio.create_task(self._run(dl))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return dl

    async def _run(self, dl: Download) -> None:
        async with self._sem:
            if dl.status == Status.QUEUED:
                await dl.run(self._client)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()
