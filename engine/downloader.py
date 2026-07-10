"""One resumable HTTP download.

Strategy:
  1. Probe the URL (HEAD, falling back to a 1-byte ranged GET) for size, range
     support, etag and filename.
  2. If the server supports ranges and the size is known, preallocate the target
     file and split it into fixed-size chunks pulled by N concurrent workers, each
     writing at its own offset. Otherwise fall back to a single sequential stream.
  3. Persist chunk state to a sidecar every flush so a crash resumes cleanly.
  4. Verify size (+ optional sha256) and atomically rename ``.part`` -> final.

Runtime fields (``downloaded``, ``total``, ``status`` ...) are plain attributes the
UI polls on a timer; the engine pushes nothing, keeping the two fully decoupled.
"""

from __future__ import annotations

import asyncio
import enum
import os
import re
from urllib.parse import unquote, urlsplit

import httpx

from .resume import ChunkState, DownloadState
from .verify import VerifyError, verify

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB
WORKERS_PER_FILE = 8
MAX_RETRIES = 5
FLUSH_INTERVAL = 1.0  # seconds between sidecar writes


class Status(str, enum.Enum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    VERIFYING = "verifying"
    DONE = "done"
    ERROR = "error"


def _filename_from(url: str, content_disposition: str | None) -> str:
    if content_disposition:
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", content_disposition)
        if m:
            return _sanitize(unquote(m.group(1)))
    path = urlsplit(url).path
    name = unquote(os.path.basename(path)) or "download"
    return _sanitize(name)


def _sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or "download"


class Download:
    def __init__(
        self,
        url: str,
        dest_dir: str,
        *,
        sha256: str | None = None,
        chunk_size: int = CHUNK_SIZE,
        workers: int = WORKERS_PER_FILE,
    ) -> None:
        self.url = url
        self.dest_dir = dest_dir
        self.expected_sha256 = sha256
        self.chunk_size = chunk_size
        self.workers = workers

        # runtime (polled by UI)
        self.filename: str = urlsplit(url).path.rsplit("/", 1)[-1] or "download"
        self.total: int = -1
        self.downloaded: int = 0
        self.status: Status = Status.QUEUED
        self.error: str | None = None

        self._state: DownloadState | None = None
        self._gate = asyncio.Event()
        self._gate.set()  # running by default
        self._flush_lock = asyncio.Lock()

    # ----- public paths -------------------------------------------------
    @property
    def dest(self) -> str:
        return os.path.join(self.dest_dir, self.filename)

    @property
    def part(self) -> str:
        return self.dest + ".part"

    # ----- controls -----------------------------------------------------
    def pause(self) -> None:
        if self.status == Status.DOWNLOADING:
            self._gate.clear()
            self.status = Status.PAUSED

    def resume(self) -> None:
        if self.status == Status.PAUSED:
            self.status = Status.DOWNLOADING
            self._gate.set()

    # ----- main ---------------------------------------------------------
    async def run(self, client: httpx.AsyncClient) -> None:
        try:
            await self._prepare(client)
            if self._state and not self._state.is_complete():
                self.status = Status.DOWNLOADING
                await self._download(client)
            self.status = Status.VERIFYING
            verify(self.part, self.total, self.expected_sha256)
            os.replace(self.part, self.dest)
            DownloadState.clear(self.part)
            self.status = Status.DONE
        except (httpx.HTTPError, VerifyError, OSError) as exc:
            self.error = str(exc)
            self.status = Status.ERROR

    async def _prepare(self, client: httpx.AsyncClient) -> None:
        """Probe server, then load or plan chunk state and preallocate."""
        self.status = Status.PROBING
        total, supports_range, etag, filename = await self._probe(client)
        self.filename = filename
        self.total = total
        os.makedirs(self.dest_dir, exist_ok=True)

        existing = DownloadState.load(self.part)
        # A sidecar is only trustworthy if its data file is still there.
        if existing and existing.matches(total, etag) and os.path.exists(self.part):
            self._state = existing
            self.downloaded = existing.downloaded
        else:
            DownloadState.clear(self.part)
            self._state = DownloadState.plan(
                self.url, filename, total, supports_range, self.chunk_size, etag
            )
            self._preallocate()
            self._state.save(self.part)

    async def _probe(self, client: httpx.AsyncClient):
        try:
            r = await client.head(self.url, follow_redirects=True)
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", -1))
            supports_range = r.headers.get("Accept-Ranges", "").lower() == "bytes"
            etag = r.headers.get("ETag")
            filename = _filename_from(str(r.url), r.headers.get("Content-Disposition"))
            if total >= 0:
                return total, supports_range, etag, filename
        except httpx.HTTPError:
            pass
        # Fallback: ranged GET reveals total via Content-Range and range support.
        headers = {"Range": "bytes=0-0"}
        r = await client.get(self.url, headers=headers, follow_redirects=True)
        r.raise_for_status()
        etag = r.headers.get("ETag")
        filename = _filename_from(str(r.url), r.headers.get("Content-Disposition"))
        cr = r.headers.get("Content-Range")
        if r.status_code == 206 and cr and "/" in cr:
            total = int(cr.rsplit("/", 1)[-1])
            return total, True, etag, filename
        total = int(r.headers.get("Content-Length", -1))
        return total, False, etag, filename

    def _preallocate(self) -> None:
        if self.total > 0 and self._state and self._state.supports_range:
            with open(self.part, "wb") as fh:
                fh.truncate(self.total)

    async def _download(self, client: httpx.AsyncClient) -> None:
        assert self._state is not None
        if not self._state.supports_range or self.total < 0:
            await self._stream_single(client)
            return

        write_lock = asyncio.Lock()
        sem = asyncio.Semaphore(self.workers)
        fh = open(self.part, "r+b")
        stop = asyncio.Event()
        flusher = asyncio.create_task(self._flush_loop(stop))
        try:
            async def run_chunk(chunk: ChunkState) -> None:
                async with sem:
                    await self._fetch_chunk(client, chunk, fh, write_lock)

            await asyncio.gather(*(run_chunk(c) for c in self._state.remaining()))
        finally:
            stop.set()
            await flusher
            fh.close()
            self._state.save(self.part)

    async def _fetch_chunk(
        self,
        client: httpx.AsyncClient,
        chunk: ChunkState,
        fh,
        write_lock: asyncio.Lock,
    ) -> None:
        offset = chunk.start
        for attempt in range(MAX_RETRIES):
            try:
                headers = {"Range": f"bytes={offset}-{chunk.end}"}
                async with client.stream("GET", self.url, headers=headers, follow_redirects=True) as r:
                    r.raise_for_status()
                    async for block in r.aiter_bytes():
                        await self._gate.wait()  # honor pause
                        async with write_lock:
                            fh.seek(offset)
                            fh.write(block)
                        offset += len(block)
                        self.downloaded += len(block)
                chunk.done = True
                return
            except (httpx.HTTPError, OSError):
                if attempt == MAX_RETRIES - 1:
                    raise
                # roll counter back for the bytes we will re-fetch this chunk
                self.downloaded -= offset - chunk.start
                offset = chunk.start
                await asyncio.sleep(2 ** attempt * 0.5)

    async def _stream_single(self, client: httpx.AsyncClient) -> None:
        """No range support / unknown size: sequential stream, append-resume."""
        assert self._state is not None
        resume_at = os.path.getsize(self.part) if os.path.exists(self.part) else 0
        headers = {}
        mode = "wb"
        if resume_at and self._state.supports_range:
            headers["Range"] = f"bytes={resume_at}-"
            mode = "r+b"
        stop = asyncio.Event()
        flusher = asyncio.create_task(self._flush_loop(stop))
        try:
            with open(self.part, mode) as fh:
                if mode == "r+b":
                    fh.seek(resume_at)
                    self.downloaded = resume_at
                async with client.stream("GET", self.url, headers=headers, follow_redirects=True) as r:
                    r.raise_for_status()
                    async for block in r.aiter_bytes():
                        await self._gate.wait()
                        fh.write(block)
                        self.downloaded += len(block)
        finally:
            stop.set()
            await flusher
        self._state.chunks[0].done = True
        if self.total < 0:
            self.total = self.downloaded

    async def _flush_loop(self, stop: asyncio.Event) -> None:
        assert self._state is not None
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=FLUSH_INTERVAL)
            except asyncio.TimeoutError:
                pass
            async with self._flush_lock:
                self._state.save(self.part)
