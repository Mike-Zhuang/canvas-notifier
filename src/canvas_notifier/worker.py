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
                result = await sync_once(settings, sessions, force=False)
                retry_seconds = result.get("retry_seconds", settings.poll_seconds)
            except Exception as error:
                from canvas_notifier.sync.alerts import observe_sync

                async with sessions() as session, session.begin():
                    retry_seconds = await observe_sync(
                        session, settings, [{"scope": "worker", "status": "error:" + type(error).__name__}]
                    )
                    await health(session, "sync", "error:" + type(error).__name__)
            await asyncio.sleep(retry_seconds)

    async def delivery_loop():
        while True:
            try:
                await tick(settings, sessions)
            except Exception as error:
                async with sessions() as session, session.begin():
                    await health(session, "worker", "error:" + type(error).__name__)
            await asyncio.sleep(15)

    await asyncio.gather(poll_loop(), delivery_loop())
