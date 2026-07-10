"""Torrent-like resume state.

Each download owns a sidecar ``<file>.part.json`` describing every byte-range
chunk and whether it is complete. Written atomically so a crash mid-flush never
corrupts it. On restart we reload the sidecar, re-validate the server identity
(size + etag), and re-request only the chunks still marked ``done=false``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


@dataclass
class ChunkState:
    index: int
    start: int
    end: int  # inclusive byte offset, HTTP Range semantics
    done: bool = False

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass
class DownloadState:
    url: str
    filename: str
    total: int
    supports_range: bool
    chunk_size: int
    etag: str | None = None
    chunks: list[ChunkState] = field(default_factory=list)

    # ----- construction -------------------------------------------------
    @classmethod
    def plan(
        cls,
        url: str,
        filename: str,
        total: int,
        supports_range: bool,
        chunk_size: int,
        etag: str | None = None,
    ) -> "DownloadState":
        """Build a fresh state, splitting ``total`` into fixed-size chunks."""
        chunks: list[ChunkState] = []
        if supports_range and total > 0:
            index = 0
            start = 0
            while start < total:
                end = min(start + chunk_size, total) - 1
                chunks.append(ChunkState(index, start, end))
                start = end + 1
                index += 1
        else:
            # Single-stream: one chunk covering everything (end unknown -> -1).
            chunks.append(ChunkState(0, 0, max(total - 1, -1)))
        return cls(url, filename, total, supports_range, chunk_size, etag, chunks)

    # ----- progress -----------------------------------------------------
    @property
    def downloaded(self) -> int:
        """Bytes accounted for by completed chunks."""
        return sum(c.length for c in self.chunks if c.done and c.length > 0)

    def remaining(self) -> list[ChunkState]:
        return [c for c in self.chunks if not c.done]

    def is_complete(self) -> bool:
        return all(c.done for c in self.chunks)

    def matches(self, total: int, etag: str | None) -> bool:
        """True if a fresh probe describes the same server resource."""
        if self.etag and etag:
            return self.etag == etag
        return self.total == total

    # ----- persistence --------------------------------------------------
    @staticmethod
    def sidecar_path(dest: str) -> str:
        return dest + ".part.json"

    def save(self, dest: str) -> None:
        """Atomic write: temp file in the same dir, then os.replace."""
        path = self.sidecar_path(dest)
        tmp = path + ".tmp"
        payload = asdict(self)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, dest: str) -> "DownloadState | None":
        path = cls.sidecar_path(dest)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        data["chunks"] = [ChunkState(**c) for c in data["chunks"]]
        return cls(**data)

    @staticmethod
    def clear(dest: str) -> None:
        for p in (dest + ".part.json", dest + ".part.json.tmp"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
