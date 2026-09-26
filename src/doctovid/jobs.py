"""Requests, jobs, and the queue that runs them a few at a time."""

from __future__ import annotations

import asyncio
import enum
import itertools
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: How many waiting jobs get their position refreshed when the queue moves.
#: Everyone further back sees a slightly stale number rather than the bot
#: spending an edit per waiting job per completed job.
POSITION_UPDATES = 10


class Mode(enum.StrEnum):
    VIDEO = "video"
    FILE = "file"


@dataclass(slots=True)
class Request:
    """A file someone sent, and what they have chosen to do with it so far."""

    id: int
    user_id: int
    message_id: int
    document_id: int
    file_name: str
    size: int
    mime_type: str | None
    is_video: bool
    expires_at: float
    menu_message_id: int = 0
    new_name: str | None = None
    thumbnail: str | None = None

    @property
    def output_name(self) -> str:
        return self.new_name or self.file_name

    @property
    def customised(self) -> bool:
        return self.new_name is not None or self.thumbnail is not None


@dataclass(slots=True, eq=False)
class Job:
    id: int
    request: Request
    mode: Mode
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def user_id(self) -> int:
        return self.request.user_id

    @property
    def status_message_id(self) -> int:
        return self.request.menu_message_id


class QueueFull(Exception):
    pass


class UserLimit(Exception):
    pass


Runner = Callable[[Job], Coroutine[Any, Any, None]]
PositionHook = Callable[[Job, int], Awaitable[None]]


class JobQueue:
    """First in, first out, with `workers` jobs running at once.

    Positions are 1-based among waiting jobs; 0 means the job is running.
    """

    def __init__(
        self,
        run: Runner,
        *,
        workers: int,
        max_waiting: int,
        per_user: int,
        on_position: PositionHook | None = None,
    ) -> None:
        self._run = run
        self._workers = workers
        self._max_waiting = max_waiting
        self._per_user = per_user
        self._on_position = on_position
        self._waiting: deque[Job] = deque()
        self._running: set[Job] = set()
        self._wakeup = asyncio.Condition()
        self._tasks: list[asyncio.Task[None]] = []
        self._ids = itertools.count(1)

    def next_id(self) -> int:
        return next(self._ids)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"worker-{n}") for n in range(self._workers)
        ]

    async def stop(self) -> None:
        for job in list(self._running):
            if job.task is not None:
                job.task.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------ queries

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    @property
    def running(self) -> int:
        return len(self._running)

    def jobs_for(self, user_id: int) -> list[Job]:
        return [job for job in (*self._running, *self._waiting) if job.user_id == user_id]

    def find(self, job_id: int) -> Job | None:
        return next((job for job in (*self._running, *self._waiting) if job.id == job_id), None)

    def position(self, job_id: int) -> int | None:
        if any(job.id == job_id for job in self._running):
            return 0
        for index, job in enumerate(self._waiting, start=1):
            if job.id == job_id:
                return index
        return None

    # ------------------------------------------------------------ commands

    async def submit(self, job: Job) -> int:
        """Queue `job` and return its position. Raises QueueFull or UserLimit."""
        if len(self.jobs_for(job.user_id)) >= self._per_user:
            raise UserLimit
        # Counted from everything accepted, not from `_waiting` alone: a job
        # handed to an idle worker sits in `_waiting` until that worker next
        # gets to run, and is not really waiting for anyone.
        position = max(0, len(self._running) + len(self._waiting) - self._workers + 1)
        if position > self._max_waiting:
            raise QueueFull
        self._waiting.append(job)
        async with self._wakeup:
            self._wakeup.notify()
        return position

    def cancel(self, job_id: int) -> Job | None:
        """Drop a waiting job or stop a running one. Returns the job, if found."""
        for job in self._waiting:
            if job.id == job_id:
                self._waiting.remove(job)
                return job
        for job in self._running:
            if job.id == job_id:
                if job.task is not None:
                    job.task.cancel()
                return job
        return None

    # ------------------------------------------------------------ workers

    async def _worker(self) -> None:
        while True:
            async with self._wakeup:
                await self._wakeup.wait_for(lambda: bool(self._waiting))
                job = self._waiting.popleft()
            self._running.add(job)
            # The task exists before anything else awaits, so a cancel that
            # arrives in between has something to cancel.
            job.task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
            await self._announce_positions()
            try:
                await job.task
            except asyncio.CancelledError:
                # Either this job was cancelled, which is routine, or the
                # worker itself is being stopped, which must propagate.
                if _stopping():
                    raise
            except Exception:
                log.exception("job %s crashed", job.id)
            finally:
                self._running.discard(job)

    async def _announce_positions(self) -> None:
        if self._on_position is None:
            return
        for position, job in enumerate(list(self._waiting)[:POSITION_UPDATES], start=1):
            try:
                await self._on_position(job, position)
            except Exception:
                log.debug("position update failed", exc_info=True)


def _stopping() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
