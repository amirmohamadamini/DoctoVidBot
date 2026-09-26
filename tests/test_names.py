from __future__ import annotations

import pytest

from doctovid.names import MAX_NAME_LENGTH, apply_rename, looks_like_video, safe_name


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("holiday.mkv", "holiday.mkv"),
        ("../../etc/cron.d/job", "job"),
        ("..\\..\\windows\\evil.exe", "evil.exe"),
        ("  spaced out.mp4  ", "spaced out.mp4"),
        ("tab\there.mp4", "tabhere.mp4"),
        ("..", "file"),
        ("", "file"),
        ("/", "file"),
    ],
)
def test_safe_name_keeps_one_harmless_component(given: str, expected: str) -> None:
    assert safe_name(given) == expected


def test_safe_name_shortens_but_keeps_the_extension() -> None:
    name = safe_name("a" * 500 + ".mkv")
    assert len(name) == MAX_NAME_LENGTH
    assert name.endswith(".mkv")


@pytest.mark.parametrize(
    ("original", "typed", "expected"),
    [
        ("IMG_0042.mkv", "holiday", "holiday.mkv"),
        ("IMG_0042.mkv", "holiday.mp4", "holiday.mp4"),
        ("report.pdf", "Tax return 2026.pdf", "Tax return 2026.pdf"),
        ("report.pdf", "   ", "report.pdf"),
        ("report.pdf", "../secret", "secret.pdf"),
    ],
)
def test_rename(original: str, typed: str, expected: str) -> None:
    assert apply_rename(original, typed) == expected


def test_a_video_is_known_by_mime_type_or_extension() -> None:
    assert looks_like_video("x.bin", "video/x-matroska")
    assert looks_like_video("x.MKV", None)
    assert not looks_like_video("x.pdf", "application/pdf")
