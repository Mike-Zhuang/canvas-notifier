import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from canvas_notifier.db import Lease
from canvas_notifier.domain.time import now_utc


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
