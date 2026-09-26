from __future__ import annotations

from doctovid import menus
from doctovid.transport import Keyboard

from .conftest import make_request


def labels(keyboard: Keyboard) -> list[str]:
    return [key.label for row in keyboard for key in row]


def test_a_video_is_offered_playback() -> None:
    _, keyboard = menus.request_menu(make_request("film.mkv"))
    assert "▶️ Make playable" in labels(keyboard)


def test_other_files_are_not() -> None:
    _, keyboard = menus.request_menu(make_request("report.pdf", is_video=False))
    assert "▶️ Make playable" not in labels(keyboard)
    assert "📄 Send as file" in labels(keyboard)


def test_the_menu_says_what_a_renamed_video_will_be_called() -> None:
    text, _ = menus.request_menu(make_request("film.mkv", new_name="holiday.mkv"))
    assert "holiday.mkv" in text
    assert "holiday.mp4" in text


def test_names_are_escaped() -> None:
    text, _ = menus.request_menu(make_request("<b>bold</b>.mkv"))
    assert "<b>bold</b>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;.mkv" in text


def test_a_thumbnail_can_be_removed() -> None:
    _, keyboard = menus.request_menu(make_request("film.mkv", thumbnail="/tmp/t.jpg"))
    assert "🗑 Remove thumbnail" in labels(keyboard)


def test_button_data_fits_telegrams_limit() -> None:
    _, keyboard = menus.request_menu(make_request("film.mkv", id=10**12))
    assert all(len(key.action.encode()) <= 64 for row in keyboard for key in row)


def test_the_main_action_is_blue_and_cancel_is_red() -> None:
    _, keyboard = menus.request_menu(make_request("film.mkv"))
    styles = {key.label: key.style for row in keyboard for key in row}
    assert styles["▶️ Make playable"] == "primary"
    assert styles["📄 Send as file"] is None
    assert styles["✖️ Cancel"] == "danger"

    _, keyboard = menus.request_menu(make_request("report.pdf", is_video=False))
    assert keyboard[0][0].style == "primary"
