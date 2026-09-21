from datetime import timedelta

from sqlalchemy import func, select

from canvas_notifier.canvas.http import PageResult
from canvas_notifier.db import Delivery, Event, Reminder, Resource, Rule, Scope
from canvas_notifier.delivery.smtp import deliver_batch
from canvas_notifier.domain.time import parse_time
from canvas_notifier.scheduling.planner import dispatch_reminders, rebuild
from canvas_notifier.sync.state import apply_scope

NOW = parse_time("2026-10-01T00:00:00Z")


def assignment(name="A", due=None, lock=None):
    return {"id": "2", "name": name, "due_at": due, "lock_at": lock, "submission_types": ["online_upload"]}


async def apply(session, settings, items, complete=True, kind="assignment", now=NOW):
    return await apply_scope(
        session,
        settings,
        kind,
        "1",
        PageResult(items, 1, complete, error=None if complete else "network_error"),
        now=now,
    )


async def test_baseline_aba_and_repeat(sessions, settings):
    async with sessions() as session, session.begin():
        await apply(session, settings, [assignment()])
        await apply(session, settings, [assignment("B")])
        await apply(session, settings, [assignment()])
        await apply(session, settings, [assignment()])
    async with sessions() as session:
        events = (await session.scalars(select(Event))).all()
        resource = await session.get(Resource, "assignment:1:2")
        assert len(events) == 2 and resource.version == 3
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 2


async def test_partial_does_not_baseline_or_delete(sessions, settings):
    async with sessions() as session, session.begin():
        await apply(session, settings, [assignment()], False)
        assert not (await session.get(Scope, "assignment:1:")).baseline
        await apply(session, settings, [assignment()])
        await apply(session, settings, [], False)
        assert (await session.get(Resource, "assignment:1:2")).missing_count == 0
        await apply(session, settings, [])
        assert (await session.get(Resource, "assignment:1:2")).availability == "missing_unconfirmed"
        assert not (await session.scalars(select(Event).where(Event.kind.contains("deleted")))).all()


async def test_missing_fields_do_not_clear_visible_grades(sessions, settings):
    async with sessions() as session, session.begin():
        await apply(
            session,
            settings,
            [{"id": "3", "assignment_id": "2", "published_score": 72, "published_grade": "72"}],
            kind="submission",
        )
        await apply(
            session,
            settings,
            [{"id": "3", "assignment_id": "2", "workflow_state": "graded"}],
            kind="submission",
        )
        resource = await session.get(Resource, "submission:1:2")
        assert resource.data["published_score"] == 72
        assert not (await session.scalars(select(Event))).all()


async def test_lock_only_schedule_and_restart_coalesce(sessions, settings):
    lock = NOW + timedelta(hours=1)
    async with sessions() as session, session.begin():
        await apply(session, settings, [assignment(lock=lock.isoformat())])
        await rebuild(session, NOW)
    async with sessions() as session, session.begin():
        await rebuild(session, NOW)
        await dispatch_reminders(session, settings, NOW)
    async with sessions() as session:
        jobs = (await session.scalars(select(Reminder))).all()
        deliveries = (await session.scalars(select(Delivery))).all()
        assert len(jobs) == 2 and all(j.boundary_type == "lock" for j in jobs)
        assert len(deliveries) == 1 and deliveries[0].payload["changes"]["coalesced_count"] == 2
        assert deliveries[0].payload["no_due"] is True
    sent = []

    async def sender(settings, message):
        sent.append(message)

    assert await deliver_batch(settings, sessions, sender=sender, now=NOW) == 1
    async with sessions() as session, session.begin():
        await rebuild(session, NOW)
        await dispatch_reminders(session, settings, NOW)
    assert await deliver_batch(settings, sessions, sender=sender, now=NOW) == 0 and len(sent) == 1


async def test_date_change_only_rebuild_affected_boundary(sessions, settings):
    async with sessions() as session, session.begin():
        await apply(
            session,
            settings,
            [
                assignment(
                    due=(NOW + timedelta(days=2)).isoformat(), lock=(NOW + timedelta(days=4)).isoformat()
                )
            ],
        )
        await rebuild(session, NOW)
        old_lock = {
            j.key
            for j in (await session.scalars(select(Reminder).where(Reminder.boundary_type == "lock"))).all()
        }
        await apply(
            session,
            settings,
            [
                assignment(
                    due=(NOW + timedelta(days=3)).isoformat(), lock=(NOW + timedelta(days=4)).isoformat()
                )
            ],
        )
        await rebuild(session, NOW)
        new_lock = {
            j.key
            for j in (await session.scalars(select(Reminder).where(Reminder.boundary_type == "lock"))).all()
        }
        assert new_lock == old_lock
        assert (await session.scalars(select(Reminder).where(Reminder.status == "cancelled"))).all()


async def test_smtp_retry_and_completion_recheck(sessions, settings):
    async with sessions() as session, session.begin():
        await apply(session, settings, [assignment(due=(NOW + timedelta(hours=1)).isoformat())])
        await rebuild(session, NOW)
        await dispatch_reminders(session, settings, NOW)

    async def failure(settings, message):
        raise ConnectionError("do not log credentials")

    await deliver_batch(settings, sessions, sender=failure, now=NOW)
    async with sessions() as session, session.begin():
        row = await session.scalar(select(Delivery))
        assert row.status == "retry" and row.attempts == 1 and row.reason == "ConnectionError"
        await apply(
            session,
            settings,
            [{"id": "3", "assignment_id": "2", "submitted_at": NOW.isoformat(), "user_id": "9"}],
            kind="submission",
        )
    sent = []

    async def sender(settings, message):
        sent.append(message)

    await deliver_batch(settings, sessions, sender=sender, now=NOW + timedelta(minutes=2))
    assert not sent
    async with sessions() as session:
        assert (await session.scalar(select(Delivery))).status == "cancelled"


async def test_rules_inheritance_and_equal_save(sessions, settings):
    from canvas_notifier.scheduling.rules import effective_rules

    async with sessions() as session, session.begin():
        session.add(Rule(scope="global", data={"due": ["PT1H"], "lock": []}))
        session.add(Rule(scope="course:1", data={"due": ["PT2H"]}))
        session.add(Rule(scope="assignment:1:2", data={"only_incomplete": False}))
        await session.flush()
        rules, sources = await effective_rules(session, "1", "assignment:1:2")
        assert rules.due == ["PT7200S"] and not rules.only_incomplete and rules.lock == []
        assert sources == ["global", "course:1", "assignment:1:2"]


async def test_missed_boundary_is_not_sent(sessions, settings):
    async with sessions() as session, session.begin():
        await apply(session, settings, [assignment(lock=(NOW + timedelta(hours=1)).isoformat())])
        await rebuild(session, NOW)
        await dispatch_reminders(session, settings, NOW + timedelta(hours=2))
        assert not (await session.scalars(select(Delivery))).all()
        jobs = (await session.scalars(select(Reminder))).all()
        assert jobs and all(j.status == "cancelled" for j in jobs)


async def test_assignment_rule_applies_to_grades(sessions, settings):
    async with sessions() as session, session.begin():
        session.add(Rule(scope="assignment:1:2", data={"events": {"grade_published": "off"}}))
        await session.flush()
        await apply(session, settings, [{"id": "3", "assignment_id": "2", "score": None}], kind="submission")
        await apply(session, settings, [{"id": "3", "assignment_id": "2", "score": 0}], kind="submission")
        event = await session.scalar(select(Event))
        assert event.kind == "grade_published" and event.disposition == "suppressed_by_rule"
        assert not (await session.scalars(select(Delivery))).all()


async def test_stale_submission_pauses_reminder_even_with_fresh_assignment(sessions, settings):
    async with sessions() as session, session.begin():
        session.add(Rule(scope="global", data={"stale_source_policy": "pause"}))
        await apply(session, settings, [assignment(due=(NOW + timedelta(hours=1)).isoformat())])
        await apply(
            session,
            settings,
            [{"id": "3", "assignment_id": "2", "submitted_at": None}],
            kind="submission",
            now=NOW - timedelta(days=1),
        )
        await rebuild(session, NOW)
        await dispatch_reminders(session, settings, NOW)
        assert not (await session.scalars(select(Delivery))).all()
        jobs = (await session.scalars(select(Reminder))).all()
        assert any(job.reason == "等待新鲜同步数据" for job in jobs)


async def test_mail_metadata_upgrade_does_not_emit_old_content(sessions, settings):
    value = {
        **assignment(),
        "description": '<p>说明</p><table><tr><td>课程表</td></tr></table><a href="https://example.org/resource">资源</a>',
    }
    async with sessions() as db, db.begin():
        await apply(db, settings, [value])
        resource = await db.get(Resource, "assignment:1:2")
        resource.data = {k: v for k, v in resource.data.items() if k != "_email"}
        await apply(db, settings, [value])
        assert resource.version == 1
        assert "<table>" in resource.data["_email"]["body"]
        assert not (await db.scalars(select(Event))).all()


async def test_source_timestamp_metadata_backfill_does_not_notify(sessions, settings):
    raw = {"id": "12", "display_name": "Synthetic file.pdf", "size": 4096, "updated_at": NOW.isoformat()}
    async with sessions() as db, db.begin():
        await apply(db, settings, [raw], kind="file")
        row = await db.get(Resource, "file:1:12")
        version = row.version
        await apply(db, settings, [{**raw, "created_at": (NOW - timedelta(days=1)).isoformat()}], kind="file")
        assert row.version == version
        assert row.data["_email"]["source_created_at"]
        assert not (await db.scalars(select(Event))).all()
