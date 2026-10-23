import base64
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from canvas_notifier.canvas.http import CanvasClient, CanvasError, PageResult
from canvas_notifier.db import Delivery, Resource, Scope, SyncRun
from canvas_notifier.delivery.formatting import format_changes
from canvas_notifier.delivery.images import download_image
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.delivery.smtp import deliver_batch, render_message
from canvas_notifier.domain.normalize import changes_for, normalize
from canvas_notifier.domain.time import now_utc
from canvas_notifier.sync.state import apply_scope

ORIGIN = "https://canvas.tongji.edu.cn"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def image_task(file_id):
    return {
        "id": 2,
        "name": "图片作业",
        "description": f'<p><img src="/courses/1/files/{file_id}/preview?verifier=private" alt="题图.png"></p>',
    }


async def test_image_baseline_upgrade_is_silent_then_replacement_notifies(settings, sessions):
    async with sessions() as db, db.begin():
        db.add(Scope(key="assignment:1:", kind="assignment", course_id="1", baseline=True, status="ok"))
        db.add(
            Resource(
                key="assignment:1:2",
                kind="assignment",
                external_id="2",
                course_id="1",
                scope="assignment:1:",
                data={
                    "id": "2",
                    "name": "图片作业",
                    "description": "<p></p>",
                    "html_url": ORIGIN + "/courses/1/assignments/2",
                },
                last_seen=now_utc(),
            )
        )
    async with sessions() as db, db.begin():
        await apply_scope(db, settings, "assignment", "1", PageResult([image_task(10)], 1, True))
    async with sessions() as db:
        assert not (await db.scalars(select(Delivery))).all()
        assert "private" not in str((await db.get(Resource, "assignment:1:2")).data)
    async with sessions() as db, db.begin():
        await apply_scope(db, settings, "assignment", "1", PageResult([image_task(11)], 1, True))
    async with sessions() as db:
        rows = (await db.scalars(select(Delivery))).all()
        assert len(rows) == 1 and rows[0].payload["kind"] == "assignment_content_changed"
        assert rows[0].payload["changes"]["content_images"]["before"][0]["file_id"] == "10"
        assert rows[0].payload["changes"]["content_images"]["after"][0]["file_id"] == "11"


def test_assignment_changes_distinguish_order_access_content_and_dates():
    before = {"position": 1, "locked_for_user": False, "description": "旧说明", "due_at": None}
    after = {
        "position": 2,
        "locked_for_user": True,
        "description": "新说明",
        "due_at": "2030-01-01T00:00:00Z",
    }
    events = dict(changes_for("assignment", before, after))
    assert set(events) == {
        "assignment_order_changed",
        "assignment_access_changed",
        "assignment_content_changed",
        "assignment_dates_changed",
    }
    assert set(events["assignment_content_changed"]) == {"description"}
    assert set(events["assignment_access_changed"]) == {"locked_for_user"}
    text = format_changes(
        {
            "position": {"before": 1, "after": 2},
            "locked_for_user": {"before": False, "after": True},
            "submissions": {"before": {"submitted": False}, "after": {"submitted": True}},
        },
        "Asia/Shanghai",
    )
    assert "不是题目行号" in text and "对你锁定访问：否 → 是" in text and "已提交：否" in text
    assert "position" not in text and "locked_for_user" not in text and "False" not in text


def test_image_mail_contains_inline_bytes_and_plain_link(settings):
    raw = normalize("assignment", image_task(10), ORIGIN, "1")
    p = {
        "kind": "assignment_created",
        "label": "新任务",
        "title": "图片作业",
        "course": "示例课程",
        "canvas_origin": ORIGIN,
        "excerpt": raw["_email"]["body"],
        "content_images": raw["content_images"],
        "changes": {},
        "attachments": [],
        "manage_url": "https://example.test/rules",
    }
    message = render_message(
        settings,
        SimpleNamespace(payload=p, recipient="student@example.test", message_id="<image@example.test>"),
        inline_images={ORIGIN + "/courses/1/files/10": (PNG, "png")},
    )
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert 'src="cid:' in html and "题图.png" in plain and ORIGIN + "/courses/1/files/10" in plain
    images = [part for part in message.walk() if part.get_content_maintype() == "image"]
    assert len(images) == 1 and images[0].get_payload(decode=True) == PNG
    assert "verifier" not in message.as_string()
    without_image = render_message(
        settings,
        SimpleNamespace(payload=p, recipient="student@example.test", message_id="<fallback@example.test>"),
    )
    assert "未能内嵌" in without_image.get_body(preferencelist=("plain",)).get_content()


async def test_image_download_uses_auth_only_for_canvas_and_blocks_redirect(settings):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test/files/10/download"})

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        with pytest.raises(CanvasError, match="image_url_blocked"):
            await download_image(client, ORIGIN + "/courses/1/files/10", "token")
    assert len(seen) == 1 and seen[0].url.host == "canvas.tongji.edu.cn"


async def test_image_download_rejects_html_and_oversize(settings, monkeypatch):
    import canvas_notifier.delivery.images as images

    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 16)
    for content, code in ((b"<html>login", "image_type_blocked"), (PNG, "image_too_large")):
        async with CanvasClient(
            settings, httpx.MockTransport(lambda request: httpx.Response(200, content=content))
        ) as client:
            with pytest.raises(CanvasError, match=code):
                await download_image(client, ORIGIN + "/files/10", "token")


async def test_same_course_assignments_and_associated_changes_send_one_complete_mail(settings, sessions):
    now = now_utc()
    async with sessions() as db, db.begin():
        run = SyncRun(status="ok", finished_at=now)
        db.add(run)
        await db.flush()
        db.add(
            Resource(
                key="course::1",
                kind="course",
                external_id="1",
                course_id="",
                scope="course::",
                data={"name": "示例课程"},
                last_seen=now,
            )
        )
        for i in range(1, 6):
            task = Resource(
                key=f"assignment:1:{i}",
                kind="assignment",
                external_id=str(i),
                course_id="1",
                scope="assignment:1:",
                data={
                    "name": f"作业{i}",
                    "description": f"详细说明{i}",
                    "due_at": (now + timedelta(days=i)).isoformat(),
                    "points_possible": i * 10,
                },
                last_seen=now,
            )
            db.add(task)
            await db.flush()
            await enqueue(db, settings, f"new-{i}", task, "assignment_created", sync_run_id=run.id, now=now)
            if i > 1:
                await enqueue(
                    db,
                    settings,
                    f"sort-{i}",
                    task,
                    "assignment_order_changed",
                    {"position": {"before": i, "after": i + 5}},
                    sync_run_id=run.id,
                    now=now,
                )
        planner = Resource(
            key="planner::assignment:1",
            kind="planner",
            external_id="assignment:1",
            course_id="",
            scope="planner::",
            data={
                "plannable_type": "assignment",
                "plannable_id": "1",
                "plannable": {"title": "错误的旧标题"},
                "submissions": {"submitted": True},
            },
            last_seen=now,
        )
        db.add(planner)
        await db.flush()
        await enqueue(
            db,
            settings,
            "planner-1",
            planner,
            "planner_changed",
            {"submissions": {"before": {"submitted": False}, "after": {"submitted": True}}},
            sync_run_id=run.id,
            now=now,
        )
    messages = []

    async def sender(settings, message):
        messages.append(message)

    assert await deliver_batch(settings, sessions, sender=sender, limit=1, now=now) == 10
    assert len(messages) == 1
    for part in ("plain", "html"):
        body = messages[0].get_body(preferencelist=(part,)).get_content()
        for i in range(1, 6):
            assert f"作业{i}" in body and f"详细说明{i}" in body and f"/assignments/{i}" in body
        assert "关联变化" in body and "列表排序位置" in body and "已提交：否" in body
        assert "错误的旧标题" not in body and "Canvas 更新" not in body
    async with sessions() as db:
        assert len({r.message_id for r in (await db.scalars(select(Delivery))).all()}) == 1


async def test_file_module_relations_are_nested_but_standalone_module_change_survives(settings, sessions):
    now = now_utc()
    async with sessions() as db, db.begin():
        run = SyncRun(status="ok", finished_at=now)
        db.add(run)
        await db.flush()
        for kind, rid, data in [
            ("file", "10", {"display_name": "课件.pdf", "size": 2048}),
            ("module_item", "20", {"title": "课件入口", "type": "File", "content_id": "10"}),
            ("module", "30", {"name": "课程目录"}),
        ]:
            resource = Resource(
                key=f"{kind}:1:{rid}",
                kind=kind,
                external_id=rid,
                course_id="1",
                scope=f"{kind}:1:",
                data=data,
                last_seen=now,
            )
            db.add(resource)
            await db.flush()
            await enqueue(
                db, settings, resource.key, resource, kind + "_created", sync_run_id=run.id, now=now
            )
    messages = []

    async def sender(settings, message):
        messages.append(message)

    await deliver_batch(settings, sessions, sender=sender, now=now)
    assert len(messages) == 1
    text = messages[0].get_body(preferencelist=("plain",)).get_content()
    assert (
        "课件.pdf" in text
        and "课件入口" in text
        and "课程目录" in text
        and "关联变化" in text
        and "2.0 KB" in text
    )


async def test_same_course_deadlines_merge_but_completed_task_is_cancelled(settings, sessions):
    from canvas_notifier.db import Reminder

    now = now_utc()
    boundary = now + timedelta(days=1)
    async with sessions() as db, db.begin():
        for i in range(1, 4):
            task = Resource(
                key=f"assignment:1:{i}",
                kind="assignment",
                course_id="1",
                external_id=str(i),
                scope="assignment:1:",
                last_seen=now,
                local={},
                data={
                    "name": f"截止作业{i}",
                    "due_at": boundary.isoformat(),
                    "submission_types": ["online_upload"],
                },
            )
            db.add(task)
            key = str(i) * 64
            db.add(
                Reminder(
                    key=key,
                    resource_key=task.key,
                    boundary_type="due",
                    boundary_at=boundary,
                    scheduled_at=now,
                    offset_seconds=86400,
                    status="queued",
                    rule_fingerprint="test",
                )
            )
            await db.flush()
            await enqueue(
                db, settings, f"due-{i}", task, "due", boundary=boundary, reminder_keys=[key], now=now
            )
        db.add(
            Resource(
                key="submission:1:3",
                kind="submission",
                course_id="1",
                external_id="3",
                scope="submission:1:",
                last_seen=now,
                data={"submitted_at": now.isoformat()},
            )
        )
    messages = []

    async def sender(settings, message):
        messages.append(message)

    assert await deliver_batch(settings, sessions, sender=sender, limit=1, now=now) == 2
    assert len(messages) == 1
    text = messages[0].get_body(preferencelist=("plain",)).get_content()
    assert "截止作业1" in text and "截止作业2" in text and "截止作业3" not in text
    assert "正式截止提醒" in text
    async with sessions() as db:
        assert (await db.get(Reminder, "1" * 64)).status == "sent"
        assert (await db.get(Reminder, "2" * 64)).status == "sent"
        assert (
            await db.scalar(select(Delivery).where(Delivery.resource_key == "assignment:1:3"))
        ).status == "cancelled"
