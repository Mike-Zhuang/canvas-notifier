from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.db import Delivery, Health
from canvas_notifier.domain.time import parse_time
from canvas_notifier.sync.alerts import observe_sync

NOW = parse_time("2030-01-01T00:00:00Z")
ERROR = [{"scope": "file:1:", "status": "auth_rejected"}]


async def step(sessions, settings, seconds, failures):
    async with sessions() as db, db.begin():
        return await observe_sync(db, settings, failures, now=NOW + timedelta(seconds=seconds))


async def test_transient_failure_is_silent_and_resets_window(settings, sessions):
    assert await step(sessions, settings, 0, ERROR) == 60
    assert await step(sessions, settings, 60, []) == settings.poll_seconds
    assert await step(sessions, settings, 3600, ERROR) == 60
    async with sessions() as db:
        assert not (await db.scalars(select(Delivery))).all()
        assert (await db.get(Health, "sync_alert")).details["failures"] == 1


async def test_persistent_failure_one_alert_then_stable_recovery(settings, sessions):
    assert await step(sessions, settings, 0, ERROR * 50) == 60
    assert await step(sessions, settings, 60, ERROR * 50) == 120
    await step(sessions, settings, 180, ERROR)
    async with sessions() as db:
        assert not (await db.scalars(select(Delivery))).all()
    await step(sessions, settings, 480, ERROR)
    await step(sessions, settings, 780, [{"scope": "planner::", "status": "network_error"}])
    async with sessions() as db, db.begin():
        notices = (await db.scalars(select(Delivery))).all()
        assert len(notices) == 1 and notices[0].payload["kind"] == "sync_interrupted"
        notices[0].status = "accepted"
    await step(sessions, settings, 1080, [])
    async with sessions() as db:
        assert len((await db.scalars(select(Delivery))).all()) == 1
    await step(sessions, settings, 1380, [])
    await step(sessions, settings, 1680, [])
    async with sessions() as db:
        notices = (await db.scalars(select(Delivery).order_by(Delivery.id))).all()
        assert [n.payload["kind"] for n in notices] == ["sync_interrupted", "sync_recovered"]


async def test_unsent_alert_cancelled_without_recovery_email(settings, sessions):
    for seconds in (0, 150, 300):
        await step(sessions, settings, seconds, ERROR)
    await step(sessions, settings, 600, [])
    await step(sessions, settings, 900, [])
    async with sessions() as db:
        notices = (await db.scalars(select(Delivery))).all()
        assert len(notices) == 1 and notices[0].status == "cancelled"
