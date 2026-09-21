from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.db import Delivery, Rule
from canvas_notifier.delivery.queue import LABELS, enqueue
from canvas_notifier.delivery.smtp import deliver_batch
from canvas_notifier.domain.time import parse_time
from canvas_notifier.scheduling.policies import enable_immediate_notifications
from canvas_notifier.scheduling.rules import effective_rules

NOW = parse_time("2030-01-01T16:30:00Z")


async def test_every_event_kind_is_immediate_by_default(settings, sessions):
    async with sessions() as db, db.begin():
        for kind in LABELS:
            assert await enqueue(db, settings, "default:" + kind, None, kind, now=NOW) == "queued"
    async with sessions() as db:
        rows = (await db.scalars(select(Delivery))).all()
        assert len(rows) == len(LABELS)
        assert all(row.available_at == NOW and not row.payload["digest"] for row in rows)


async def test_policy_clears_all_scope_overrides_and_is_idempotent(settings, sessions):
    async with sessions() as db, db.begin():
        db.add(Rule(scope="global", data={"default_event": "digest", "quiet_enabled": True}))
        db.add(
            Rule(
                scope="course:1",
                data={"events": {"file_created": "digest"}, "quiet_enabled": True, "due": ["PT2H"]},
            )
        )
        db.add(Rule(scope="assignment:1:2", data={"enabled": False, "events": {"grade_changed": "off"}}))
        await db.flush()
        result = await enable_immediate_notifications(db, now=NOW)
        assert result["updated_rule_scopes"] == 3
        rule, _ = await effective_rules(db, "1", "assignment:1:2")
        assert rule.enabled and not rule.quiet_enabled and rule.default_event == "immediate"
        assert set(rule.events.values()) == {"immediate"} and rule.due == ["PT7200S"]
        versions = {r.scope: r.version for r in (await db.scalars(select(Rule))).all()}
        assert (await enable_immediate_notifications(db, now=NOW))["updated_rule_scopes"] == 0
        assert versions == {r.scope: r.version for r in (await db.scalars(select(Rule))).all()}


async def test_waiting_digest_released_but_sent_not_replayed(settings, sessions):
    async with sessions() as db, db.begin():
        db.add(Rule(scope="global", data={"default_event": "digest", "quiet_enabled": True}))
        await db.flush()
        for key in ("pending", "accepted", "retry"):
            await enqueue(db, settings, key, None, "file_created", now=NOW)
        rows = (await db.scalars(select(Delivery).order_by(Delivery.id))).all()
        rows[1].status = "accepted"
        rows[1].accepted_at = NOW
        rows[1].attempts = 1
        rows[2].status = "retry"
        rows[2].attempts = 1
        rows[2].available_at = NOW + timedelta(minutes=5)
        ids = [r.message_id for r in rows]
        result = await enable_immediate_notifications(db, now=NOW)
        assert result["released_pending_deliveries"] == 1
        assert rows[0].available_at == NOW and not rows[0].payload["digest"]
        assert rows[1].status == "accepted" and rows[1].payload["digest"]
        assert rows[2].available_at == NOW + timedelta(minutes=5) and not rows[2].payload["digest"]
    sent = []

    async def sender(settings, message):
        sent.append(message["Message-ID"])

    assert await deliver_batch(settings, sessions, sender=sender, now=NOW) == 1
    assert sent == [ids[0]]
    assert await deliver_batch(settings, sessions, sender=sender, now=NOW) == 0
