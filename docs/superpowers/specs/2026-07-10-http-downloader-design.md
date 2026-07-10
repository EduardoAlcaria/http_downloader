# HTTP Downloader — Design

**Date:** 2026-07-10
**Status:** Approved

A fast, resumable HTTP download manager with a Textual TUI. Paste any HTTP(S)
link; it downloads with multi-connection chunking for speed, survives crashes and
network drops via torrent-like chunk-state persistence, and verifies integrity on
finish.

## Goals

- Download any HTTP(S) URL fast (multi-connection, byte-range chunking).
- Resume after failure/crash/kill with no re-download of completed bytes.
- Queue: paste many URLs, download several in parallel, monitor all live.
- Verify integrity (size always, SHA256 when known).
- Live TUI: per-download progress, speed, ETA, global stats.

## Non-Goals (YAGNI)

- No BitTorrent/peer protocol — "torrent-like" refers only to resumable chunk
  state, not P2P.
- No FTP/magnet/other protocols. HTTP(S) only.
- No browser integration, no scheduling, no bandwidth throttling (add later if needed).

## Stack

Python 3.14, `httpx` (async, HTTP/2, Range support), `textual` (TUI), `pytest`.
No external binaries (no aria2). No new deps beyond these.

## Architecture

Async download engine (`asyncio` + `httpx`) fully decoupled from the Textual UI.
The engine emits progress events; the UI subscribes and renders. No blocking I/O
on the UI thread.

```
paste URL -> queue -> downloader.probe() -> preallocate file + sidecar
  -> N async workers (Range GET) -> write@offset + update chunk map
  -> periodic flush + emit progress event -> UI row updates
  -> all chunks done -> verify() -> rename .part -> final file
```

Failure mid-download (net drop, crash, kill) -> sidecar JSON survives -> re-adding
the same URL resumes from the chunk map, skipping completed chunks.

## Components

Each unit has one purpose, a defined interface, and is independently testable.

### `engine/downloader.py`
Manages one download.
- `probe(url)` — HEAD request: reads `Content-Length`, `Accept-Ranges`,
  `ETag`/`Last-Modified`, derives filename (Content-Disposition or URL path).
- Splits total size into N chunks (byte ranges of configurable size).
- Spawns N async workers; each issues `GET` with `Range: bytes=start-end`, streams
  its slice, seeks to `start` and writes into the preallocated file.
- Emits progress events (bytes done, per-chunk state) to a callback/queue.

### `engine/resume.py`
Torrent-like persistence. Sidecar file `<file>.part.json`:
```json
{
  "url": "...",
  "filename": "...",
  "total": 123456789,
  "etag": "...",
  "chunk_size": 8388608,
  "chunks": [{"index": 0, "start": 0, "end": 8388607, "done": true}, ...]
}
```
- Written atomically (temp file + rename) every ~1s and on chunk completion.
- On restart: load sidecar, validate `etag`/size against a fresh probe. Match ->
  re-request only chunks where `done=false`. Mismatch -> discard and restart.
- Target file is preallocated to `total` so offsets remain valid across restarts.

### `engine/verify.py`
On completion:
- Assert written file size == `Content-Length`.
- If SHA256 known (user-pasted or server `Digest`/`Content-MD5`-style header),
  hash the file and compare.
- On mismatch: mark download corrupt, keep `.part` files for retry, surface error.

### `engine/queue.py`
Holds many downloads. Caps global concurrency with an `asyncio.Semaphore`
(e.g. 3 concurrent files x 8 chunks each). Schedules queued downloads as slots free.

### `app.py`
Textual UI:
- URL input field (paste + Enter to enqueue).
- Table/list of downloads; each row: filename, progress bar, %, speed, ETA, status.
- Footer: global speed, active/queued counts.
- Keys: pause / resume / cancel selected; quit.

## Edge Cases

- **No Range support** (`Accept-Ranges: none`, or HEAD unsupported) -> single-stream
  fallback; resume via `Range: bytes=N-` append from current file size.
- **Unknown size** (no `Content-Length`) -> single stream, no chunking, no
  preallocation; progress shown as bytes-downloaded (no %/ETA).
- **ETag/Last-Modified changed on resume** -> server file changed; discard `.part`
  and restart cleanly.
- **Disk full / write error** -> pause that download, surface error in its row,
  keep sidecar.
- **Server drops connection mid-chunk** -> worker retries that chunk from its last
  written offset (bounded retries, exponential backoff).

## Testing

`pytest` with a local test server (`http.server` subclass supporting `Range`):
- Chunked download produces a byte-identical file vs source.
- Resume after simulated kill re-requests only incomplete chunks.
- Fallback path when server refuses Range.
- Verify step catches a corrupted/truncated result.
- ETag-changed-on-resume triggers clean restart.

## Layout

```
http_downloader/
  app.py
  engine/
    __init__.py
    downloader.py
    resume.py
    verify.py
    queue.py
  tests/
    test_server.py      # Range-capable local server fixture
    test_downloader.py
    test_resume.py
    test_verify.py
  pyproject.toml
  README.md
```
