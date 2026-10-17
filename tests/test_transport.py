from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import FileReferenceExpiredError
from telethon.tl.types import DocumentAttributeVideo

from doctovid.media import MediaInfo
from doctovid.transport import FileChanged, TelethonTransport


async def ignore_progress(done: int, total: int) -> None:
    pass


@dataclass
class FakeClient:
    uploads: list[dict[str, Any]] = field(default_factory=list)
    sends: list[dict[str, Any]] = field(default_factory=list)
    #: Media whose file reference has expired. Only fetching the message
    #: again gives one that works.
    stale_media: set[Any] = field(default_factory=set)
    fetches: int = 0
    messages: dict[int, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)

    def action(self, chat_id: int, kind: str) -> contextlib.nullcontext[None]:
        self.actions.append(kind)
        return contextlib.nullcontext()

    async def upload_file(self, file: Any, **kwargs: Any) -> Any:
        self.uploads.append({"file": file, **kwargs})
        return SimpleNamespace(name=kwargs["file_name"])

    async def send_file(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        if any(file == stale for stale in self.stale_media):
            raise FileReferenceExpiredError(request=None)
        self.sends.append({"chat_id": chat_id, "file": file, **kwargs})
        return SimpleNamespace(id=900 + len(self.sends))

    async def get_messages(self, chat_id: int, ids: int) -> Any:
        self.fetches += 1
        return self.messages.get(ids)

    async def download_file(self, document: Any, dest: str, **kwargs: Any) -> None:
        Path(dest).write_bytes(b"x" * kwargs["file_size"])


def sent(document_id: int, size: int) -> Any:
    return SimpleNamespace(
        document=SimpleNamespace(id=document_id), media=object(), file=SimpleNamespace(size=size)
    )


async def test_a_file_is_downloaded_only_if_it_is_still_the_one_offered(tmp_path: Path) -> None:
    client = FakeClient(messages={5: sent(9000, 3)})
    transport = TelethonTransport(client)
    dest = str(tmp_path / "source")

    assert await transport.download(42, 5, dest, ignore_progress, document_id=9000, size=3)
    with pytest.raises(FileChanged):
        await transport.download(42, 5, dest, ignore_progress, document_id=1234, size=3)
    with pytest.raises(FileChanged):
        await transport.download(42, 5, dest, ignore_progress, document_id=9000, size=4)
    assert not await transport.download(42, 6, dest, ignore_progress, document_id=9000, size=3)


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


class Refreshing(FakeClient):
    """Each fetch of a message carries a new file reference."""

    async def get_messages(self, chat_id: int, ids: int) -> Any:
        self.fetches += 1
        return SimpleNamespace(media=f"reference {self.fetches}")


async def test_a_copy_refetches_a_stale_file_reference() -> None:
    client = Refreshing(stale_media={"reference 1"})
    assert await TelethonTransport(client).copy(-100, 5, 42) == 901
    assert client.sends[0]["file"] == "reference 2"


async def test_a_reference_still_stale_after_refetching_is_an_error() -> None:
    client = Refreshing(stale_media={"reference 1", "reference 2"})
    with pytest.raises(FileReferenceExpiredError):
        await TelethonTransport(client).copy(-100, 5, 42)
    assert client.fetches == 2


async def test_copying_a_deleted_message_returns_none() -> None:
    assert await TelethonTransport(FakeClient()).copy(-100, 5, 42) is None
