"""Chunked download correctness + single-stream fallback."""

from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine.downloader import Download, Status  # noqa: E402
from tests.test_server import serve  # noqa: E402


@pytest.mark.asyncio
async def test_chunked_download_is_byte_identical(tmp_path):
    with serve(size=1_000_000) as (url, blob):
        dl = Download(url, str(tmp_path), chunk_size=64 * 1024, workers=8)
        async with httpx.AsyncClient() as client:
            await dl.run(client)
        assert dl.status == Status.DONE, dl.error
        assert dl.total == len(blob)
        assert dl.downloaded == len(blob)
        with open(dl.dest, "rb") as fh:
            assert fh.read() == blob
        # sidecar cleaned up on success
        assert not os.path.exists(dl.part + ".json")


def test_guard_rejects_login_html():
    req = httpx.Request("GET", "https://accounts.google.com/v3/signin/challenge/pwd")
    resp = httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, request=req)
    with pytest.raises(httpx.HTTPError, match="not a file"):
        Download._guard_login_page(resp)


def test_guard_allows_html_attachment():
    req = httpx.Request("GET", "https://example.com/page.html")
    resp = httpx.Response(
        200,
        headers={"Content-Type": "text/html", "Content-Disposition": "attachment; filename=page.html"},
        request=req,
    )
    Download._guard_login_page(resp)  # no raise: explicitly an attachment


@pytest.mark.asyncio
async def test_single_stream_fallback(tmp_path):
    with serve(size=500_000, supports_range=False) as (url, blob):
        dl = Download(url, str(tmp_path))
        async with httpx.AsyncClient() as client:
            await dl.run(client)
        assert dl.status == Status.DONE, dl.error
        with open(dl.dest, "rb") as fh:
            assert fh.read() == blob
