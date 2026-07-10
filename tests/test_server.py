"""A minimal Range-capable HTTP server used as a fixture by the other tests.

``http.server.SimpleHTTPRequestHandler`` does not implement byte ranges, so we
serve a fixed in-memory blob with proper ``Range`` / ``Content-Range`` / ``ETag``
handling, and a ``no_range`` mode to exercise the single-stream fallback path.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def make_blob(size: int) -> bytes:
    # Deterministic, non-repeating enough to catch offset bugs.
    return (b"".join(i.to_bytes(4, "big") for i in range(size // 4 + 1)))[:size]


class _State:
    blob = b""
    etag = ""
    supports_range = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _common_headers(self):
        self.send_header("ETag", _State.etag)
        if _State.supports_range:
            self.send_header("Accept-Ranges", "bytes")
        else:
            self.send_header("Accept-Ranges", "none")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(_State.blob)))
        self._common_headers()
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and _State.supports_range:
            start, _, end = rng.removeprefix("bytes=").partition("-")
            start = int(start)
            end = int(end) if end else len(_State.blob) - 1
            body = _State.blob[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(_State.blob)}")
            self.send_header("Content-Length", str(len(body)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(_State.blob)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(_State.blob)


@contextlib.contextmanager
def serve(size: int = 1_000_000, supports_range: bool = True):
    _State.blob = make_blob(size)
    _State.etag = '"' + hashlib.sha256(_State.blob).hexdigest()[:16] + '"'
    _State.supports_range = supports_range
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address
        yield f"http://{host}:{port}/blob", _State.blob
    finally:
        httpd.shutdown()
        thread.join()


def test_server_serves_ranges():
    import urllib.request

    with serve(size=1000) as (url, blob):
        req = urllib.request.Request(url, headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206
            assert r.read() == blob[10:20]
