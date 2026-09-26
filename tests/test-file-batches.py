from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.canvas.http import PageResult
from canvas_notifier.db import Delivery, Lease, Resource, Scope, SyncRun
from canvas_notifier.delivery.smtp import deliver_batch
from canvas_notifier.domain.time import now_utc
from canvas_notifier.sync.state import apply_scope


async def test_complete_course_batch_waits_for_sync_and_retries_as_one_mail(settings, sessions):
    now = now_utc()
    async with sessions() as db, db.begin():
        run = SyncRun(status="running")
        db.add(run)
        await db.flush()
        run_id = run.id
        db.add(Lease(name="sync", owner="test", expires_at=now + timedelta(minutes=10)))
        for kind in ("file", "folder"):
            db.add(Scope(key=f"{kind}:1:", kind=kind, course_id="1", baseline=True, status="ok"))
        await db.flush()
        await apply_scope(
            db,
            settings,
            "file",
            "1",
            PageResult(
                [
                    {
                        "id": i,
                        "display_name": f"Lecture-{i}.pdf",
                        "filename": f"original-{i}.pdf",
                        "size": i * 1024,
                        "content-type": "application/pdf",
                        "folder_id": 9,
                        "created_at": "2030-01-01T00:00:00Z",
                    }
                    for i in range(1, 34)
                ],
                1,
                True,
            ),
            sync_run_id=run_id,
            now=now,
        )
        await apply_scope(
            db,
            settings,
            "folder",
            "1",
            PageResult(
                [{"id": 9, "name": "Slides", "full_name": "course files/Slides", "files_count": 33}], 1, True
            ),
            sync_run_id=run_id,
            now=now,
        )
    calls = []

    async def failing(settings, message):
        calls.append(message)
        raise TimeoutError()

    assert await deliver_batch(settings, sessions, sender=failing, now=now, limit=2) == 0
    assert not calls
    async with sessions() as db, db.begin():
        run = await db.get(SyncRun, run_id)
        run.finished_at = now
    await deliver_batch(settings, sessions, sender=failing, now=now, limit=2)
    assert len(calls) == 1
    message = calls[0]
    text = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    for i in range(1, 34):
        for body in (text, html):
            assert f"Lecture-{i}.pdf" in body and f"original-{i}.pdf" in body
            assert f"/courses/1/files/{i}" in body
    assert "application/pdf" in text and "2030" in text and "Slides" in text

    async def success(settings, message):
        calls.append(message)

    await deliver_batch(settings, sessions, sender=success, now=now + timedelta(seconds=61), limit=2)
    assert len(calls) == 2 and calls[0]["Message-ID"] == calls[1]["Message-ID"]
    async with sessions() as db:
        rows = (await db.scalars(select(Delivery))).all()
        assert len(rows) == 34 and all(r.status == "accepted" for r in rows)
    await deliver_batch(settings, sessions, sender=success, now=now + timedelta(seconds=120))
    assert len(calls) == 2


async def test_courses_and_recipients_never_mix(settings, sessions):
    from canvas_notifier.delivery.queue import enqueue

    now = now_utc()
    async with sessions() as db, db.begin():
        run = SyncRun(status="ok", finished_at=now)
        db.add(run)
        await db.flush()
        for cid, recipient in [
            ("1", "first@example.test"),
            ("1", "second@example.test"),
            ("2", "first@example.test"),
        ]:
            resource = Resource(
                key=f"file:{cid}:{recipient}",
                kind="file",
                course_id=cid,
                external_id="1",
                scope=f"file:{cid}:",
                data={"display_name": f"Course-{cid}.pdf"},
                last_seen=now,
            )
            db.add(resource)
            await db.flush()
            await enqueue(
                db,
                settings,
                resource.key,
                resource,
                "file_created",
                force_recipient=recipient,
                sync_run_id=run.id,
                now=now,
            )
    messages = []

    async def sender(settings, message):
        messages.append(message)

    await deliver_batch(settings, sessions, sender=sender, now=now)
    assert len(messages) == 3
    for message in messages:
        body = message.get_body(preferencelist=("plain",)).get_content()
        assert not ("Course-1.pdf" in body and "Course-2.pdf" in body)
