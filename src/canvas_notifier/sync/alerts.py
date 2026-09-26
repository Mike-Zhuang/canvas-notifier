"""持久化连续失败窗口：一次故障只告警一次，短暂抖动不发邮件。"""

from sqlalchemy import select

from canvas_notifier.db import Delivery, Health
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.domain.time import now_utc, parse_time


async def observe_sync(session, settings, failures, *, now=None):
    now = now or now_utc()
    row = await session.get(Health, "sync_alert")
    if row is None:
        row = Health(name="sync_alert", status="ok", details={})
        session.add(row)
    state = dict(row.details)
    if failures:
        state["successes"] = 0
        state["failures"] = state.get("failures", 0) + 1
        state.setdefault("first_failure", now.isoformat())
        state["affected"] = failures
        elapsed = (now - parse_time(state["first_failure"])).total_seconds()
        if (
            not state.get("notified")
            and state["failures"] >= settings.sync_alert_failures
            and elapsed >= settings.sync_alert_seconds
        ):
            incident = "sync-incident:" + state["first_failure"]
            await enqueue(
                session,
                settings,
                incident,
                None,
                "sync_interrupted",
                {
                    "连续失败轮数": state["failures"],
                    "开始时间": state["first_failure"],
                    "受影响范围": failures,
                    "后续重试": "每 5 分钟重试，IAM 临时错误遵循独立退避；需要验证时等待本人处理",
                },
                now=now,
            )
            state["notified"] = True
            state["incident"] = incident
        row.status = "alerting" if state.get("notified") else "watching"
    else:
        state["successes"] = state.get("successes", 0) + 1
        state["failures"] = 0
        if not state.get("notified"):
            state = {}
            row.status = "ok"
        elif state["successes"] >= settings.sync_recovery_successes:
            # 只对真正投递过的故障发恢复通知；尚未发送的旧告警直接取消。
            notices = list(
                (
                    await session.scalars(
                        select(Delivery).where(Delivery.unique_key.startswith(state["incident"] + ":"))
                    )
                ).all()
            )
            if any(n.status == "sending" for n in notices):
                row.details, row.updated_at = state, now
                return settings.poll_seconds
            accepted = any(n.status == "accepted" for n in notices)
            for notice in notices:
                if notice.status in ("pending", "retry"):
                    notice.status, notice.reason = "cancelled", "故障已恢复"
            if accepted:
                await enqueue(
                    session,
                    settings,
                    state["incident"] + ":recovered",
                    None,
                    "sync_recovered",
                    {"状态": "连续两轮正常，持续故障已恢复", "原受影响范围": state.get("affected", [])},
                    now=now,
                )
            state, row.status = {}, "ok"
    row.details, row.updated_at = state, now
    return (60, 120, 300)[min(max(state.get("failures", 1) - 1, 0), 2)] if failures else settings.poll_seconds
