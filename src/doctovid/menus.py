"""What the bot says, and the buttons under it. No Telegram calls here."""

from __future__ import annotations

from html import escape

from . import progress
from .jobs import Request
from .names import with_extension
from .transport import DANGER, PRIMARY, SUCCESS, Key, Keyboard, Style

HELP_BUTTON = "❓ Help"
USAGE_BUTTON = "📊 My usage"
ADMIN_BUTTON = "🛠 Admin panel"
KEYBOARD_LABELS = frozenset({HELP_BUTTON, USAGE_BUTTON, ADMIN_BUTTON})


def main_keyboard(is_admin: bool) -> list[list[tuple[str, Style | None]]]:
    """The keyboard that sits under the message box."""
    rows: list[list[tuple[str, Style | None]]] = [[(HELP_BUTTON, None), (USAGE_BUTTON, None)]]
    if is_admin:
        rows.append([(ADMIN_BUTTON, PRIMARY)])
    return rows


def welcome(max_file_mb: int, cache_on: bool) -> str:
    lines = [
        "<b>Send me a video as a file</b> and I'll send it back as a video you can play "
        "right here in Telegram.",
        "",
        "I can also rename any file, change its extension, or give it a new thumbnail.",
        "",
        f"Files up to {progress.size(max_file_mb * 1024 * 1024)}. "
        f"Tap {HELP_BUTTON} below for details.",
    ]
    if cache_on:
        lines += [
            "",
            "<i>Videos converted without changes are kept in a private channel, so the "
            "same file comes back instantly next time.</i>",
        ]
    return "\n".join(lines)


HELP = """<b>How it works</b>

1. Send a file. For a video, send it <b>as a file</b> (in the attach menu, choose File).
2. Pick what to do with it:
   <b>Make playable</b>: for videos. Telegram only plays MP4 inline. A video that is \
already a playable MP4 is sent straight back; an MKV, AVI, WebM and so on is repacked \
into MP4 without re-encoding, so the quality is unchanged and it takes seconds.
   <b>Send as file</b>: returns the file untouched, with any new name or thumbnail.
   <b>Rename</b>: type a new name. Add an extension to change it too.
   <b>Thumbnail</b>: send a photo to use as the cover.
3. If other files are ahead of yours, you'll see your place in the queue.

Videos that would need re-encoding (for example VP9) can't be made playable, but can \
still be renamed or given a thumbnail.

There is a daily and a monthly limit on how many files, and how much data, each person \
can send. Tap 📊 My usage to see where you are."""


def request_menu(request: Request) -> tuple[str, Keyboard]:
    lines = [f"<b>{escape(request.file_name)}</b> · {progress.size(request.size)}"]
    if request.new_name is not None:
        lines.append(f"New name: <b>{escape(request.new_name)}</b>")
    if request.is_video and request.new_name is not None:
        playable = with_extension(request.new_name, ".mp4")
        if playable != request.new_name:
            lines.append(f"<i>Made playable, it will be called {escape(playable)}.</i>")
    if request.thumbnail is not None:
        lines.append("Thumbnail: your picture")
    lines += ["", "What should I do with it?"]

    rid = request.id
    keyboard: list[list[Key]] = []
    # The main action is blue. For a video that is playback; for anything
    # else, sending it back is the only thing there is to do.
    if request.is_video:
        keyboard.append([Key("▶️ Make playable", f"video:{rid}", PRIMARY)])
        keyboard.append([Key("📄 Send as file", f"file:{rid}")])
    else:
        keyboard.append([Key("📄 Send as file", f"file:{rid}", PRIMARY)])
    thumbnail = "🖼 Change thumbnail" if request.thumbnail else "🖼 Thumbnail"
    keyboard.append([Key("✏️ Rename", f"name:{rid}"), Key(thumbnail, f"thumb:{rid}")])
    last = [Key("✖️ Cancel", f"drop:{rid}", DANGER)]
    if request.thumbnail is not None:
        last.insert(0, Key("🗑 Remove thumbnail", f"nothumb:{rid}"))
    keyboard.append(last)
    return "\n".join(lines), keyboard


def queued(name: str, position: int) -> str:
    if position <= 0:
        return f"⏳ Starting <b>{escape(name)}</b>…"
    return f"⏳ <b>{escape(name)}</b> is waiting its turn: number {position} in the queue."


def rename_prompt(name: str) -> str:
    return (
        f"Send the new name for <b>{escape(name)}</b>.\n"
        "Leave off the extension to keep it, or type one to change it."
    )


THUMBNAIL_PROMPT = "Send a photo to use as the thumbnail."


def join_required(link: str) -> tuple[str, Keyboard]:
    return (
        "To use this bot, please join our channel first.",
        [[Key("📢 Join the channel", link, PRIMARY)], [Key("✅ I've joined", "joined:0", SUCCESS)]],
    )
