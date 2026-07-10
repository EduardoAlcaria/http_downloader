# http_downloader

Fast, resumable HTTP(S) download manager with a Textual TUI.

- **Fast** — multi-connection chunked downloads (parallel HTTP `Range` requests),
  HTTP/2 connection reuse via `httpx`.
- **Resumable (torrent-like)** — per-chunk state in a `<file>.part.json` sidecar.
  Crash, kill it, pull the plug — re-add the URL and it continues, skipping every
  chunk already on disk. No re-downloading completed bytes.
- **Safe** — size check always; SHA256 check when a digest is known. Corrupt results
  are flagged, not silently kept.
- **Queue + live TUI** — paste many URLs, download several at once, watch progress,
  speed and ETA per download.

## Install

```bash
pip install -e .
# or: pip install "httpx[http2]" textual
```

## Run

TUI:

```bash
python app.py
```

Paste a URL, press Enter. Keys: `p` pause/resume, `x` cancel, `q` quit.
Files land in `./downloads`.

Headless (scripts / CI):

```bash
python app.py https://example.com/big.iso https://example.com/other.zip
```

## How resume works

On start, the target file is preallocated to its full size and split into fixed
8 MiB chunks. N workers each fetch their byte range and write at the right offset.
Chunk completion is flushed to the sidecar ~once a second. On restart the engine
re-probes the server, checks the `ETag`/size still match, then re-requests only the
chunks still marked incomplete. If the server file changed, it restarts cleanly.

Servers without `Range` support fall back to a single sequential stream (resumable
by byte offset where the server allows it).

## Tests

```bash
pip install pytest pytest-asyncio
pytest
```

Tests spin up a local `Range`-capable server and cover chunked correctness, resume
(only missing chunks re-fetched), ETag-change restart, single-stream fallback, and
integrity verification.

## Layout

```
app.py            Textual TUI + headless entrypoint
engine/
  downloader.py   one resumable download (probe, chunk, workers, retries)
  resume.py       sidecar chunk-state persistence
  verify.py       size + sha256 integrity check
  queue.py        many downloads, global concurrency cap
tests/            local Range server + engine tests
```
