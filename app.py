"""Textual TUI for the HTTP download manager.

Paste a URL, press Enter, watch it download. The engine runs on Textual's own
asyncio loop; this screen just polls each download's plain attributes on a timer
and renders a row per download (progress, speed, ETA, status). Speed and ETA are
derived here from bytes-since-last-tick so the engine stays UI-agnostic.
"""

from __future__ import annotations

import os
import sys
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input

from engine import DownloadQueue, Status

DEST_DIR = os.path.join(os.getcwd(), "downloads")


def human_bytes(n: int) -> str:
    if n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def human_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class DownloaderApp(App):
    CSS = """
    #url { dock: top; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        ("p", "pause_resume", "Pause/Resume"),
        ("x", "cancel", "Cancel"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Paste HTTP(S) URL, press Enter...", id="url")
        yield DataTable(id="table", cursor_type="row")
        yield Footer()

    dest_dir = DEST_DIR
    auth_headers: dict | None = None
    auth_cookies = None

    def on_mount(self) -> None:
        self.queue = DownloadQueue(
            self.dest_dir, headers=self.auth_headers, cookies=self.auth_cookies
        )
        table = self.query_one(DataTable)
        table.add_columns("File", "Size", "%", "Speed", "ETA", "Status")
        # per-download sampling state for speed: idx -> (bytes, monotonic)
        self._samples: dict[int, tuple[int, float]] = {}
        self._rows = 0  # rows are append-only, so row index == download index
        self.set_interval(0.5, self.refresh_rows)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        url = event.value.strip()
        if not url:
            return
        self.queue.add(url)
        event.input.value = ""

    def refresh_rows(self) -> None:
        table = self.query_one(DataTable)
        now = time.monotonic()
        for idx, dl in enumerate(self.queue.downloads):
            prev = self._samples.get(idx)
            speed = 0.0
            if prev:
                dt = now - prev[1]
                if dt > 0:
                    speed = (dl.downloaded - prev[0]) / dt
            self._samples[idx] = (dl.downloaded, now)

            pct = (dl.downloaded / dl.total * 100) if dl.total > 0 else 0.0
            eta = (dl.total - dl.downloaded) / speed if (speed > 0 and dl.total > 0) else float("inf")
            status = dl.error if dl.status == Status.ERROR else dl.status.value
            row = (
                dl.filename[:40],
                human_bytes(dl.total),
                f"{pct:5.1f}%" if dl.total > 0 else "--",
                f"{human_bytes(int(speed))}/s" if dl.status == Status.DOWNLOADING else "--",
                human_eta(eta) if dl.status == Status.DOWNLOADING else "--",
                status,
            )
            if idx < self._rows:
                for col, value in enumerate(row):
                    table.update_cell_at((idx, col), value)
            else:
                table.add_row(*row)
                self._rows += 1

    def _selected(self):
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        idx = table.cursor_row
        if idx is None or idx >= len(self.queue.downloads):
            return None
        return self.queue.downloads[idx]

    def action_pause_resume(self) -> None:
        dl = self._selected()
        if not dl:
            return
        if dl.status == Status.DOWNLOADING:
            dl.pause()
        elif dl.status == Status.PAUSED:
            dl.resume()

    def action_cancel(self) -> None:
        dl = self._selected()
        if dl:
            dl.pause()  # ponytail: cancel == pause for now; task cleanup on quit

    async def action_quit(self) -> None:
        await self.queue.aclose()
        self.exit()


def load_browser_cookies(browser: str):
    """Read auth cookies from a local browser profile via browser_cookie3."""
    try:
        import browser_cookie3
    except ImportError:
        sys.exit("--browser needs browser_cookie3: pip install browser_cookie3")
    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        sys.exit(f"unknown browser '{browser}' (try: firefox, chrome, edge)")
    return loader()


def parse_args(argv: list[str]):
    import argparse

    p = argparse.ArgumentParser(prog="http-downloader")
    p.add_argument("urls", nargs="*", help="URLs to download (omit for TUI)")
    p.add_argument("--dest", default=DEST_DIR, help="output dir (default ./downloads)")
    p.add_argument("--cookie", help='raw Cookie header, e.g. "SID=...; HSID=..."')
    p.add_argument("--header", action="append", default=[], metavar="K:V",
                   help="extra request header (repeatable)")
    p.add_argument("--browser", help="read cookies from this browser (firefox/chrome/edge)")
    return p.parse_args(argv)


def build_auth(args):
    """Return (headers dict, cookies) from parsed CLI auth flags."""
    headers = {}
    if args.cookie:
        headers["Cookie"] = args.cookie
    for h in args.header:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    cookies = load_browser_cookies(args.browser) if args.browser else None
    return headers, cookies


def main() -> None:
    args = parse_args(sys.argv[1:])
    headers, cookies = build_auth(args)

    if args.urls:
        # Headless one-shot mode for quick use / CI.
        import asyncio

        async def _run() -> None:
            q = DownloadQueue(args.dest, headers=headers, cookies=cookies)
            for url in args.urls:
                q.add(url)
            while any(d.status not in (Status.DONE, Status.ERROR) for d in q.downloads):
                await asyncio.sleep(0.3)
            for d in q.downloads:
                print(f"{d.status.value}: {d.filename} ({d.error or human_bytes(d.total)})")
            await q.aclose()

        asyncio.run(_run())
    else:
        app = DownloaderApp()
        app.dest_dir = args.dest
        app.auth_headers = headers
        app.auth_cookies = cookies
        app.run()


if __name__ == "__main__":
    main()
