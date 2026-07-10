"""Integrity verification: size + optional sha256."""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine.verify import VerifyError, verify  # noqa: E402


def test_size_ok(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x" * 100)
    verify(str(p), 100)  # no raise


def test_size_mismatch(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x" * 90)
    with pytest.raises(VerifyError, match="size mismatch"):
        verify(str(p), 100)


def test_sha256_match(tmp_path):
    p = tmp_path / "f"
    data = b"hello world" * 10
    p.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    verify(str(p), len(data), digest)


def test_sha256_mismatch(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"corrupted")
    with pytest.raises(VerifyError, match="sha256 mismatch"):
        verify(str(p), 9, "0" * 64)
