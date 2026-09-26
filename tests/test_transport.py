from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from telethon.errors import FileReferenceExpiredError
from telethon.tl.types import DocumentAttributeVideo

from doctovid.media import MediaInfo
from doctovid.transport import TelethonTransport


async def ignore_progress(done: int, total: int) -> None:
    pass


@dataclass
class FakeClient:
    uploads: list[dict[str, Any]] = field(default_factory=list)
    sends: list[dict[str, Any]] = field(default_factory=list)
    stale_references: int = 0
    messages: dict[int, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)

    def action(self, chat_id: int, kind: str) -> contextlib.nullcontext[None]:
        self.actions.append(kind)
        return contextlib.nullcontext()

    async def upload_file(self, file: Any, **kwargs: Any) -> Any:
        self.uploads.append({"file": file, **kwargs})
        return SimpleNamespace(name=kwargs["file_name"])

    async def send_file(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        if self.stale_references:
            self.stale_references -= 1
            raise FileReferenceExpiredError(request=None)
        self.sends.append({"chat_id": chat_id, "file": file, **kwargs})
        return SimpleNamespace(id=900 + len(self.sends))

    async def get_messages(self, chat_id: int, ids: int) -> Any:
        return self.messages.get(ids)


async def test_the_chosen_name_is_the_uploaded_name() -> None:
    # `send_file` has no file_name parameter and silently ignores one, which
    # is why renaming never worked in the first version of this bot.
    client = FakeClient()
    await TelethonTransport(client).upload(
        1,
        io.BytesIO(b"video"),
        size=5,
        name="holiday.mp4",
        video=None,
        thumbnail=None,
        reply_to=None,
        progress=ignore_progress,
    )
    assert client.uploads[0]["file_name"] == "holiday.mp4"
    assert client.uploads[0]["file_size"] == 5
    assert client.sends[0]["file"].name == "holiday.mp4"
    assert "file_name" not in client.sends[0]


async def test_a_video_is_sent_streamable_with_its_attributes() -> None:
    client = FakeClient()
    info = MediaInfo(video_codec="h264", duration_s=61.5, width=1280, height=720)
    await TelethonTransport(client).upload(
        1,
        io.BytesIO(b"video"),
        size=5,
        name="film.mp4",
        video=info,
        thumbnail=b"jpeg",
        reply_to=7,
        progress=ignore_progress,
    )
    send = client.sends[0]
    [attribute] = send["attributes"]
    assert isinstance(attribute, DocumentAttributeVideo)
    assert (attribute.duration, attribute.w, attribute.h) == (61.5, 1280, 720)
    assert attribute.supports_streaming
    assert send["thumb"] == b"jpeg"
    assert not send.get("force_document")
    assert client.actions == ["video"]


async def test_a_file_is_sent_as_a_document() -> None:
    client = FakeClient()
    await TelethonTransport(client).upload(
        1,
        io.BytesIO(b"%PDF"),
        size=4,
        name="report.pdf",
        video=None,
        thumbnail=None,
        reply_to=None,
        progress=ignore_progress,
    )
    assert client.sends[0]["force_document"] is True
    assert client.sends[0]["mime_type"] == "application/pdf"


async def test_a_copy_refetches_a_stale_file_reference() -> None:
    client = FakeClient(stale_references=1, messages={5: SimpleNamespace(media="media")})
    assert await TelethonTransport(client).copy(-100, 5, 42) == 901


async def test_copying_a_deleted_message_returns_none() -> None:
    assert await TelethonTransport(FakeClient()).copy(-100, 5, 42) is None
