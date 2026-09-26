from __future__ import annotations

import asyncio

import pytest

from doctovid.jobs import Job, JobQueue, QueueFull, UserLimit

from .conftest import make_job, make_request


class Gate:
    """A runner whose jobs finish only when told to."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.release: dict[int, asyncio.Event] = {}

    async def __call__(self, job: Job) -> None:
        self.started.append(job.id)
        await self.release.setdefault(job.id, asyncio.Event()).wait()

    def finish(self, job_id: int) -> None:
        self.release.setdefault(job_id, asyncio.Event()).set()


def job_for(user: int, job_id: int) -> Job:
    return make_job(make_request(user_id=user), job_id=job_id)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
async def gate() -> Gate:
    return Gate()


async def test_runs_workers_at_a_time_in_order(gate: Gate) -> None:
    queue = JobQueue(gate, workers=2, max_waiting=10, per_user=10)
    queue.start()
    positions = [await queue.submit(job_for(1, n)) for n in range(1, 5)]
    await settle()

    assert positions == [0, 0, 1, 2]
    assert gate.started == [1, 2]
    assert queue.position(3) == 1

    gate.finish(1)
    await settle()
    assert gate.started == [1, 2, 3]
    assert queue.position(4) == 1
    await queue.stop()


async def test_waiting_jobs_hear_their_new_position(gate: Gate) -> None:
    heard: list[tuple[int, int]] = []

    async def on_position(job: Job, position: int) -> None:
        heard.append((job.id, position))

    queue = JobQueue(gate, workers=1, max_waiting=10, per_user=10, on_position=on_position)
    queue.start()
    for n in range(1, 4):
        await queue.submit(job_for(1, n))
    await settle()
    heard.clear()

    gate.finish(1)
    await settle()
    assert heard == [(3, 1)]
    await queue.stop()


async def test_limits(gate: Gate) -> None:
    queue = JobQueue(gate, workers=1, max_waiting=2, per_user=2)
    queue.start()
    await queue.submit(job_for(1, 1))
    await queue.submit(job_for(1, 2))
    with pytest.raises(UserLimit):
        await queue.submit(job_for(1, 3))

    await queue.submit(job_for(2, 4))
    await settle()
    with pytest.raises(QueueFull):
        await queue.submit(job_for(3, 5))
    await queue.stop()


async def test_cancel_a_waiting_job(gate: Gate) -> None:
    queue = JobQueue(gate, workers=1, max_waiting=10, per_user=10)
    queue.start()
    await queue.submit(job_for(1, 1))
    await queue.submit(job_for(1, 2))
    await settle()

    assert queue.cancel(2) is not None
    gate.finish(1)
    await settle()
    assert gate.started == [1]
    assert queue.waiting == 0
    await queue.stop()


async def test_cancel_a_running_job_frees_its_worker(gate: Gate) -> None:
    queue = JobQueue(gate, workers=1, max_waiting=10, per_user=10)
    queue.start()
    await queue.submit(job_for(1, 1))
    await queue.submit(job_for(1, 2))
    await settle()

    queue.cancel(1)
    await settle()
    assert gate.started == [1, 2]
    assert queue.running == 1
    await queue.stop()


async def test_a_crashing_job_does_not_kill_its_worker() -> None:
    ran: list[int] = []

    async def run(job: Job) -> None:
        ran.append(job.id)
        if job.id == 1:
            raise RuntimeError("boom")

    queue = JobQueue(run, workers=1, max_waiting=10, per_user=10)
    queue.start()
    await queue.submit(job_for(1, 1))
    await queue.submit(job_for(1, 2))
    await settle()
    assert ran == [1, 2]
    await queue.stop()
