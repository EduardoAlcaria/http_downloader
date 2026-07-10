"""Resume: a half-finished sidecar re-fetches only the missing chunks."""

from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine.downloader import Download, Status  # noqa: E402
from engine.resume import DownloadState  # noqa: E402
from tests.test_server import serve  # noqa: E402


@pytest.mark.asyncio
async def test_resume_skips_completed_chunks(tmp_path):
    size = 400_000
    chunk = 100_000  # -> 4 chunks
    with serve(size=size) as (url, blob):
        # First pass: run fully so the file + sidecar exist and are correct.
        dl = Download(url, str(tmp_path), chunk_size=chunk, workers=4)
        async with httpx.AsyncClient() as client:
            await dl.run(client)
        assert dl.status == Status.DONE

        # Simulate an interrupted download: recreate .part from the final file,
        # write a sidecar with only the first two chunks marked done, corrupt the
        # bytes belonging to the not-done chunks so a real re-fetch is required.
        part = dl.dest + ".part"
        data = bytearray(blob)
        data[200_000:] = b"\x00" * (size - 200_000)
        with open(part, "wb") as fh:
            fh.write(data)
        state = DownloadState.plan(url, dl.filename, size, True, chunk, dl._state.etag)
        state.chunks[0].done = True
        state.chunks[1].done = True
        state.save(part)

        dl2 = Download(url, str(tmp_path), chunk_size=chunk, workers=4)
        async with httpx.AsyncClient() as client:
            await dl2.run(client)
        assert dl2.status == Status.DONE, dl2.error
        # Only the two missing chunks (200_000 bytes) should have been fetched.
        assert dl2.downloaded == size
        with open(dl2.dest, "rb") as fh:
            assert fh.read() == blob


@pytest.mark.asyncio
async def test_etag_change_restarts(tmp_path):
    with serve(size=300_000) as (url, blob):
        dl = Download(url, str(tmp_path), chunk_size=100_000)
        async with httpx.AsyncClient() as client:
            await dl.run(client)
        part = dl.dest + ".part"
        # Write a stale sidecar with a mismatching etag; engine must discard it.
        state = DownloadState.plan(url, dl.filename, 300_000, True, 100_000, '"stale"')
        for c in state.chunks:
            c.done = True
        with open(part, "wb") as fh:
            fh.write(b"\x00" * 300_000)
        state.save(part)

        dl2 = Download(url, str(tmp_path), chunk_size=100_000)
        async with httpx.AsyncClient() as client:
            await dl2.run(client)
        assert dl2.status == Status.DONE, dl2.error
        with open(dl2.dest, "rb") as fh:
            assert fh.read() == blob
