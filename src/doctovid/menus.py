"""What the bot says, and the buttons under it. No Telegram calls here."""

from __future__ import annotations

from .i18n import Html, Locale, Locales
from .jobs import Request
from .names import with_extension
from .transport import DANGER, PRIMARY, SUCCESS, Key, Keyboard, Style

#: The keyboard under the message box. A tap sends the button's label as a
#: message, in whichever language the keyboard was drawn in.
KEYBOARD = ("help", "usage", "language", "admin")


def main_keyboard(t: Locale, is_admin: bool) -> list[list[tuple[str, Style | None]]]:
    second: list[tuple[str, Style | None]] = [(t.label("keyboard.language"), None)]
    if is_admin:
        second.append((t.label("keyboard.admin"), PRIMARY))
    return [[(t.label("keyboard.help"), None), (t.label("keyboard.usage"), None)], second]


def keyboard_actions(locales: Locales) -> dict[str, str]:
    """Every keyboard label, in every language, and what it does. Someone who
    switched language may still have the old keyboard up."""
    return {t.label(f"keyboard.{action}"): action for t in locales for action in KEYBOARD}


def language_picker(locales: Locales, current: Locale | None) -> tuple[Html, Keyboard]:
    """Asked in every language at once, since the reader may know only one.
    Each line keeps its own direction."""
    text = Html("\n".join(t.text("language.choose") for t in locales))
    keys = [Key(t.name, f"lang:{t.code}", SUCCESS if t is current else PRIMARY) for t in locales]
    return text, [keys[i : i + 2] for i in range(0, len(keys), 2)]


def welcome(t: Locale, max_file_mb: int, cache_on: bool) -> Html:
    parts = [
        t.phrase(
            "start.welcome",
            size=t.size(max_file_mb * 1024 * 1024),
            help=t.word("keyboard.help"),
        )
    ]
    if cache_on:
        parts += [Html(""), t.phrase("start.cache_note")]
    return t.lines(*parts)


def help_text(t: Locale, is_admin: bool) -> Html:
    parts = [t.phrase("help.text", usage=t.word("keyboard.usage"))]
    if is_admin:
        parts += [Html(""), t.phrase("help.admin_note", admin=t.word("keyboard.admin"))]
    return t.lines(*parts)


def request_menu(request: Request, t: Locale) -> tuple[Html, Keyboard]:
    lines = [t.phrase("menu.title", name=request.file_name, size=t.size(request.size))]
    if request.new_name is not None:
        lines.append(t.phrase("menu.new_name", name=request.new_name))
    if request.is_video and request.new_name is not None:
        playable = with_extension(request.new_name, ".mp4")
        if playable != request.new_name:
            lines.append(t.phrase("menu.playable_name", name=playable))
    if request.thumbnail is not None:
        lines.append(t.phrase("menu.thumbnail"))
    lines += [Html(""), t.phrase("menu.question")]

    rid = request.id
    keyboard: list[list[Key]] = []
    # The main action is blue. For a video that is playback; for anything
    # else, sending it back is the only thing there is to do.
    if request.is_video:
        keyboard.append([Key(t.label("menu.make_playable"), f"video:{rid}", PRIMARY)])
        keyboard.append([Key(t.label("menu.send_file"), f"file:{rid}")])
    else:
        keyboard.append([Key(t.label("menu.send_file"), f"file:{rid}", PRIMARY)])
    thumbnail = "menu.change_thumbnail" if request.thumbnail else "menu.add_thumbnail"
    keyboard.append(
        [Key(t.label("menu.rename"), f"name:{rid}"), Key(t.label(thumbnail), f"thumb:{rid}")]
    )
    last = [Key(t.label("menu.cancel"), f"drop:{rid}", DANGER)]
    if request.thumbnail is not None:
        last.insert(0, Key(t.label("menu.remove_thumbnail"), f"nothumb:{rid}"))
    keyboard.append(last)
    return t.lines(*lines), keyboard


def queued(name: str, position: int, t: Locale) -> Html:
    if position <= 0:
        return t.text("queue.starting", name=name)
    return t.text("queue.waiting", name=name, position=position)


def join_required(link: str, t: Locale) -> tuple[Html, Keyboard]:
    return (
        t.text("join.prompt"),
        [
            [Key(t.label("join.open"), link, PRIMARY)],
            [Key(t.label("join.done"), "joined:0", SUCCESS)],
        ],
    )
