import asyncio
import json
import socket
from datetime import timedelta
from pathlib import Path

import httpx
from aiosmtpd.controller import Controller
from sqlalchemy import func, select

from canvas_notifier.db import Delivery, Event, Resource, Rule
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.delivery.smtp import deliver_batch
from canvas_notifier.domain.time import now_utc
from canvas_notifier.sync.leases import lease
from canvas_notifier.sync.service import sync_once


async def test_real_smtp_protocol_multipart_outbox(settings, sessions):
    messages = []

    class Inbox:
        async def handle_DATA(self, server, session, envelope):
            messages.append(envelope.content)
            return "250 accepted"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    controller = Controller(Inbox(), hostname="127.0.0.1", port=port)
    controller.start()
    settings.smtp_port = port
    try:
        async with sessions() as session, session.begin():
            await enqueue(session, settings, "synthetic:mail", None, "test")
        assert await deliver_batch(settings, sessions) == 1
        assert await deliver_batch(settings, sessions) == 0
        from email import policy
        from email.parser import BytesParser

        msg = BytesParser(policy=policy.default).parsebytes(messages[0])
        assert msg.get_content_type() == "multipart/alternative"
        assert "通知测试" in msg["Subject"]
        assert msg.get_body(preferencelist=("plain",))
        assert msg.get_body(preferencelist=("html",))
    finally:
        controller.stop()


async def test_concurrent_workers_claim_once(settings, sessions):
    async with sessions() as session, session.begin():
        await enqueue(session, settings, "concurrent", None, "test")
    calls = []

    async def sender(settings, message):
        calls.append(message["Message-ID"])
        await asyncio.sleep(0.01)

    await asyncio.gather(
        deliver_batch(settings, sessions, sender=sender), deliver_batch(settings, sessions, sender=sender)
    )
    assert len(calls) == 1
    async with lease(sessions, "sync", seconds=10) as first:
        assert first
        async with lease(sessions, "sync", seconds=10) as second:
            assert not second


async def test_expired_delivery_lease_recovered(settings, sessions):
    async with sessions() as session, session.begin():
        await enqueue(session, settings, "crash", None, "test")
        row = await session.scalar(select(Delivery))
        row.status = "sending"
        row.lease_owner = "old-worker"
        row.lease_until = now_utc() - timedelta(seconds=1)
        old_id = row.message_id
    calls = []

    async def sender(settings, message):
        calls.append(message["Message-ID"])

    assert await deliver_batch(settings, sessions, sender=sender) == 1
    assert calls == [old_id]


async def test_har_sync_end_to_end_and_change(settings, sessions):
    data = json.loads(Path("tests/fixtures/har-sanitized.json").read_text())
    changed = False

    def handler(request):
        path = request.url.path
        if path.endswith("/users/self"):
            return httpx.Response(200, json={"id": "self-user"})
        if path == "/api/v1/courses":
            return httpx.Response(200, json=data["courses"])
        parts = path.split("/")
        course = parts[4] if len(parts) > 4 else ""
        if path.endswith("/assignments"):
            items = [
                {**a, "name": a["name"] + (" changed" if changed else "")}
                for a in data["assignments"]
                if a["course_id"] == course
            ]
            return httpx.Response(200, json=items)
        if path.endswith("/students/submissions"):
            return httpx.Response(
                200,
                json=[{**a, "user_id": "self-user"} for a in data["submissions"] if a["course_id"] == course],
            )
        if path.endswith("/submissions/self"):
            aid = parts[6]
            submission = next(a for a in data["submissions"] if a["assignment_id"] == aid)
            return httpx.Response(200, json={**submission, "user_id": "self-user"})
        if path.endswith("/files"):
            page = int(request.url.params.get("page", "1"))
            headers = (
                {"Link": str(request.url.copy_set_param("page", str(page + 1))).join(["<", '>; rel="next"'])}
                if page < 4
                else {}
            )
            return httpx.Response(200, json=data["file_pages"][page - 1], headers=headers)
        return httpx.Response(200, json=[])

    result = await sync_once(settings, sessions, transport=httpx.MockTransport(handler))
    assert result["status"] == "ok"
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Resource).where(Resource.kind == "assignment")
            )
            == 52
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(Resource).where(Resource.kind == "submission")
            )
            == 52
        )
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
    changed = True
    result = await sync_once(settings, sessions, transport=httpx.MockTransport(handler))
    assert result["status"] == "ok"
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.kind == "assignment_content_changed")
            )
            == 52
        )
    result = await sync_once(settings, sessions, transport=httpx.MockTransport(handler))
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.kind == "assignment_content_changed")
            )
            == 52
        )


async def test_digest_coalesces_messages(settings, sessions):
    async with sessions() as session, session.begin():
        session.add(Rule(scope="global", data={"default_event": "digest"}))
        await session.flush()
        await enqueue(session, settings, "digest:1", None, "file_created")
        await enqueue(session, settings, "digest:2", None, "file_changed")
    calls = []

    async def sender(settings, message):
        calls.append(message)

    result = await deliver_batch(settings, sessions, sender=sender, now=now_utc() + timedelta(days=1))
    async with sessions() as db:
        assert result == 2, [(r.status, r.reason) for r in (await db.scalars(select(Delivery))).all()]
    assert len(calls) == 1 and "更新摘要" in calls[0]["Subject"]


async def test_utc_roundtrip_for_quiet_and_digest(sessions, settings):
    from canvas_notifier.domain.time import parse_time

    async with sessions() as db, db.begin():
        await enqueue(db, settings, "timezone-roundtrip", None, "test")
        row = await db.scalar(select(Delivery))
        row.available_at = parse_time("2026-10-01T18:00:00+08:00").astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
        )
    async with sessions() as db:
        row = await db.scalar(select(Delivery))
        assert row.available_at == parse_time("2026-10-01T10:00:00Z")


async def test_dry_run_keeps_formal_database_unchanged(settings, sessions, tmp_path):
    from canvas_notifier.db import create_schema, database

    async with sessions() as db, db.begin():
        await enqueue(db, settings, "formal-sentinel", None, "test")

    def handler(request):
        return httpx.Response(200, json={"id": "1"} if request.url.path.endswith("/users/self") else [])

    engine, temporary = database("sqlite+aiosqlite:///" + str(tmp_path / "dry-run.db"))
    await create_schema(engine)
    try:
        result = await sync_once(
            settings.model_copy(update={"mail_to": ""}), temporary, transport=httpx.MockTransport(handler)
        )
        assert result["status"] == "ok"
    finally:
        await engine.dispose()
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Delivery)) == 1
        assert not (await db.scalars(select(Resource))).all()


async def test_skipped_failed_scope_keeps_global_partial(settings, sessions):
    from canvas_notifier.db import Scope

    async with sessions() as session, session.begin():
        session.add(
            Scope(
                key="page:999:", kind="page", course_id="999", status="resource_unavailable", complete=False
            )
        )

    def handler(request):
        return httpx.Response(200, json={"id": "1"} if request.url.path.endswith("/users/self") else [])

    result = await sync_once(settings, sessions, force=False, transport=httpx.MockTransport(handler))
    assert result["status"] == "partial" and result["unhealthy_scopes"] == 1
