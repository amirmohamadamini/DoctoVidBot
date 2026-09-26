from __future__ import annotations

import os
from pathlib import Path

import pytest

from doctovid.safefile import UnsafeFile, claim, read_small


def test_a_regular_file_is_opened(tmp_path: Path) -> None:
    path = tmp_path / "out.mp4"
    path.write_bytes(b"video")
    with claim(path) as file:
        assert file.read() == b"video"


def test_a_symlink_is_refused_even_to_a_real_file(tmp_path: Path) -> None:
    secret = tmp_path / "bot.session"
    secret.write_bytes(b"auth key")
    link = tmp_path / "out.mp4"
    link.symlink_to(secret)
    with pytest.raises(UnsafeFile):
        claim(link)


def test_a_fifo_is_refused_without_hanging(tmp_path: Path) -> None:
    fifo = tmp_path / "out.mp4"
    os.mkfifo(fifo)
    with pytest.raises(UnsafeFile, match="not a regular file"):
        claim(fifo)


def test_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UnsafeFile):
        claim(tmp_path)


def test_read_small_enforces_its_limit(tmp_path: Path) -> None:
    path = tmp_path / "thumb.jpg"
    path.write_bytes(b"x" * 10)
    assert read_small(path, 10) == b"x" * 10
    with pytest.raises(UnsafeFile, match="larger than"):
        read_small(path, 9)
