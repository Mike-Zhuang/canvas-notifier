import asyncio

from canvas_notifier.delivery.smtp import deliver_batch
from canvas_notifier.scheduling.planner import dispatch_reminders
from canvas_notifier.sync.leases import lease
from canvas_notifier.sync.preflight import refresh_due
from canvas_notifier.sync.service import sync_once
from canvas_notifier.sync.state import health


async def tick(settings, sessions):
    await refresh_due(settings, sessions)
    async with lease(sessions, "reminders") as acquired:
        if acquired:
            async with sessions() as session, session.begin():
                await dispatch_reminders(session, settings)
    await deliver_batch(settings, sessions)
    async with sessions() as session, session.begin():
        await health(session, "worker", "ok")


async def worker(settings, sessions):
    async def poll_loop():
        while True:
            try:
                await sync_once(settings, sessions, force=False)
            except Exception as error:
                async with sessions() as session, session.begin():
                    await health(session, "sync", "error:" + type(error).__name__)
            await asyncio.sleep(settings.poll_seconds)

    async def delivery_loop():
        while True:
            try:
                await tick(settings, sessions)
            except Exception as error:
                async with sessions() as session, session.begin():
                    await health(session, "worker", "error:" + type(error).__name__)
            await asyncio.sleep(15)

    await asyncio.gather(poll_loop(), delivery_loop())
