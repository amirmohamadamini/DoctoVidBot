"""File names: what a user sends us is untrusted, what we send back must be sane."""

from __future__ import annotations

import os

MAX_NAME_LENGTH = 200

VIDEO_EXTENSIONS = frozenset(
    [
        ".3gp",
        ".avi",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogv",
        ".ts",
        ".webm",
        ".wmv",
    ]
)


def safe_name(name: str, fallback: str = "file") -> str:
    """Reduce `name` to a single, harmless path component.

    The name on a Telegram document is whatever the sending client put there.
    Official apps never produce a slash, but the API accepts one, and this name
    is joined onto a directory on our disk.
    """
    cleaned = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    cleaned = cleaned.strip().strip(".").strip()
    if not cleaned:
        return fallback
    if len(cleaned) <= MAX_NAME_LENGTH:
        return cleaned
    stem, ext = os.path.splitext(cleaned)
    if len(ext) > 16:
        return cleaned[:MAX_NAME_LENGTH]
    return stem[: MAX_NAME_LENGTH - len(ext)] + ext


def extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def with_extension(name: str, ext: str) -> str:
    return os.path.splitext(name)[0] + ext


def apply_rename(original: str, typed: str) -> str:
    """The name a user asked for, keeping the old extension if they left it off.

    Typing `holiday` for `IMG_0042.mkv` means "call it holiday", not "strip the
    extension". Typing `holiday.mp4` changes the extension, as asked.
    """
    new = safe_name(typed, fallback="")
    if not new:
        return original
    if extension(new):
        return new
    return new + extension(original)


def looks_like_video(name: str, mime_type: str | None) -> bool:
    if mime_type and mime_type.startswith("video/"):
        return True
    return extension(name) in VIDEO_EXTENSIONS
