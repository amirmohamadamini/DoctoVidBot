"""Everything the job runner asks of Telegram, behind one small interface.

The runner is tested against a fake of this; only `TelethonTransport` talks to
the real thing.
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import os
from collections.abc import Sequence
from typing import Any, BinaryIO, Literal, NamedTuple, Protocol

from telethon import Button, TelegramClient
from telethon.errors import FileReferenceExpiredError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeVideo

from .media import MediaInfo
from .progress import ProgressCallback

log = logging.getLogger(__name__)

#: The largest part Telegram accepts. Each part is a round trip, so the
#: default of 128 KB for small files makes them four times slower than needed.
PART_SIZE_KB = 512

#: Telegram's button colours: blue for the main action, green to confirm, red
#: to cancel. Clients too old to know them show an ordinary button.
Style = Literal["primary", "success", "danger"]
PRIMARY: Style = "primary"
SUCCESS: Style = "success"
DANGER: Style = "danger"


class Key(NamedTuple):
    """One button. An action that is a link opens it; anything else is sent
    back to the bot as callback data."""

    label: str
    action: str
    style: Style | None = None


Keyboard = Sequence[Sequence[Key]]

#: Shown in the empty message box while the keyboard is up.
INPUT_PLACEHOLDER = "Send a file…"


class Transport(Protocol):
    async def download(
        self, chat_id: int, message_id: int, dest: str, progress: ProgressCallback
    ) -> bool:
        """Fetch a message's file to `dest`. False if the message is gone."""

    async def upload(
        self,
        chat_id: int,
        file: BinaryIO,
        *,
        size: int,
        name: str,
        video: MediaInfo | None,
        thumbnail: bytes | None,
        reply_to: int | None,
        progress: ProgressCallback,
    ) -> int:
        """Send an open file of `size` bytes, as a streamable video when `video`
        is given. Returns the new message's id."""

    async def copy(
        self, from_chat: int, message_id: int, to_chat: int, *, reply_to: int | None = None
    ) -> int | None:
        """Re-send a message's file without a forward header. None if it is gone."""

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None
    ) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...


def to_buttons(keyboard: Keyboard | None) -> list[list[Any]] | None:
    if not keyboard:
        return None
    return [[_button(key) for key in row] for row in keyboard]


def to_keyboard(rows: Sequence[Sequence[tuple[str, Style | None]]]) -> list[list[Any]]:
    """A keyboard under the message box. Each tap sends its label as a message."""
    return [
        [
            Button.text(
                label,
                style=style,
                resize=True,
                persistent=True,
                placeholder=INPUT_PLACEHOLDER,
            )
            for label, style in row
        ]
        for row in rows
    ]


def _button(key: Key) -> Any:
    if key.action.startswith("https://"):
        return Button.url(key.label, key.action, style=key.style)
    return Button.inline(key.label, key.action.encode(), style=key.style)


class TelethonTransport:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def download(
        self, chat_id: int, message_id: int, dest: str, progress: ProgressCallback
    ) -> bool:
        # Fetched again rather than kept from when it arrived: file references
        # expire, and a job can wait in the queue for a long time.
        message = await self._client.get_messages(chat_id, ids=message_id)
        if message is None or message.media is None:
            return False
        await self._client.download_file(
            message.document or message.media,
            dest,
            part_size_kb=PART_SIZE_KB,
            file_size=message.file.size,
            progress_callback=progress,
        )
        return os.path.exists(dest)

    async def upload(
        self,
        chat_id: int,
        file: BinaryIO,
        *,
        size: int,
        name: str,
        video: MediaInfo | None,
        thumbnail: bytes | None,
        reply_to: int | None,
        progress: ProgressCallback,
    ) -> int:
        # "sending a video…" under the chat's name while this runs.
        async with self._client.action(chat_id, "video" if video else "document"):
            return await self._upload(
                chat_id,
                file,
                size=size,
                name=name,
                video=video,
                thumbnail=thumbnail,
                reply_to=reply_to,
                progress=progress,
            )

    async def _upload(
        self,
        chat_id: int,
        file: BinaryIO,
        *,
        size: int,
        name: str,
        video: MediaInfo | None,
        thumbnail: bytes | None,
        reply_to: int | None,
        progress: ProgressCallback,
    ) -> int:
        # Uploaded separately because `send_file` has no way to set the file
        # name. The uploaded handle carries `name`.
        handle = await self._client.upload_file(
            file,
            file_size=size,
            part_size_kb=PART_SIZE_KB,
            file_name=name,
            progress_callback=progress,
        )
        if video is not None:
            attributes = [
                DocumentAttributeVideo(
                    duration=video.duration_s,
                    w=video.width,
                    h=video.height,
                    supports_streaming=True,
                )
            ]
            sent = await self._client.send_file(
                chat_id,
                handle,
                attributes=attributes,
                mime_type="video/mp4",
                thumb=thumbnail,
                supports_streaming=True,
                reply_to=reply_to,
            )
        else:
            sent = await self._client.send_file(
                chat_id,
                handle,
                mime_type=mimetypes.guess_type(name)[0],
                thumb=thumbnail,
                force_document=True,
                reply_to=reply_to,
            )
        return int(sent.id)

    async def copy(
        self, from_chat: int, message_id: int, to_chat: int, *, reply_to: int | None = None
    ) -> int | None:
        for attempt in (1, 2):
            source = await self._client.get_messages(from_chat, ids=message_id)
            if source is None or source.media is None:
                return None
            try:
                sent = await self._client.send_file(to_chat, source.media, reply_to=reply_to)
            except FileReferenceExpiredError:
                if attempt == 2:
                    raise
                continue
            return int(sent.id)
        raise AssertionError("unreachable")

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None
    ) -> None:
        with contextlib.suppress(MessageNotModifiedError):
            await self._client.edit_message(
                chat_id, message_id, text, buttons=to_buttons(buttons), link_preview=False
            )

    async def delete(self, chat_id: int, message_id: int) -> None:
        await self._client.delete_messages(chat_id, [message_id])
