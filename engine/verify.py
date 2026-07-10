"""Integrity verification run once all chunks report done.

Size check is mandatory (guards against truncation / short writes). SHA256 is
optional: only checked when the caller supplies an expected digest (pasted by the
user or parsed from a server ``Digest`` header). A mismatch is a hard failure and
the ``.part`` state is intentionally kept so the user can retry.
"""

from __future__ import annotations

import hashlib


class VerifyError(Exception):
    pass


def _sha256(path: str, buf_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(buf_size), b""):
            h.update(block)
    return h.hexdigest()


def verify(path: str, expected_size: int, expected_sha256: str | None = None) -> None:
    """Raise VerifyError if ``path`` fails size or (optional) hash checks."""
    import os

    actual_size = os.path.getsize(path)
    if expected_size >= 0 and actual_size != expected_size:
        raise VerifyError(
            f"size mismatch: expected {expected_size} bytes, got {actual_size}"
        )

    if expected_sha256:
        actual = _sha256(path)
        if actual.lower() != expected_sha256.strip().lower():
            raise VerifyError(
                f"sha256 mismatch: expected {expected_sha256}, got {actual}"
            )
