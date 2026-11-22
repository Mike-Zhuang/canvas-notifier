import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from math import ceil

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from canvas_notifier.canvas.http import CanvasClient
from canvas_notifier.db import Health, Lease
from canvas_notifier.domain.time import now_utc, parse_time


@asynccontextmanager
async def lease(sessions, name: str, seconds=120):
    owner = uuid.uuid4().hex
    async with sessions() as session:
        async with session.begin():
            result = await session.execute(
                update(Lease)
                .where(Lease.name == name, Lease.expires_at < now_utc())
                .values(owner=owner, expires_at=now_utc() + timedelta(seconds=seconds))
            )
            acquired = result.rowcount > 0
        if not acquired:
            try:
                async with session.begin():
                    session.add(
                        Lease(name=name, owner=owner, expires_at=now_utc() + timedelta(seconds=seconds))
                    )
                acquired = True
            except IntegrityError:
                acquired = False
    if not acquired:
        yield False
        return

    async def heartbeat():
        while True:
            await asyncio.sleep(seconds / 3)
            async with sessions() as session, session.begin():
                await session.execute(
                    update(Lease)
                    .where(Lease.name == name, Lease.owner == owner)
                    .values(expires_at=now_utc() + timedelta(seconds=seconds))
                )

    task = asyncio.create_task(heartbeat())
    try:
        yield True
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        async with sessions() as session, session.begin():
            await session.execute(delete(Lease).where(Lease.name == name, Lease.owner == owner))


def remaining_seconds(deadline):
    return max(0, ceil((deadline - now_utc()).total_seconds())) if deadline else 0


@asynccontextmanager
async def canvas_session(settings, sessions, *, transport=None):
    """在同步租约内共享 REST 冷却，让预检查与手动同步都遵守服务器的时间。"""
    async with sessions() as session, session.begin():
        state = await session.get(Health, "canvas_retry_after")
        retry_at = parse_time(state.details.get("next_attempt")) if state else None
        if state and not remaining_seconds(retry_at):
            await session.delete(state)

    async def remember_retry(when):
        async with sessions() as session, session.begin():
            row = await session.get(Health, "canvas_retry_after")
            if row is None:
                row = Health(name="canvas_retry_after", status="rate_limited")
                session.add(row)
            row.details, row.updated_at = {"next_attempt": when.isoformat()}, now_utc()

    async with CanvasClient(
        settings, transport=transport, retry_at=retry_at, remember_retry=remember_retry
    ) as client:
        yield client
