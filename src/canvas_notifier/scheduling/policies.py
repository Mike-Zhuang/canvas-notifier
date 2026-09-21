"""显式切换通知策略，保留事件身份、已接受记录和传输失败的重试退避。"""

from sqlalchemy import select

from canvas_notifier.db import Delivery, Rule
from canvas_notifier.domain.time import now_utc


async def enable_immediate_notifications(session, *, now=None):
    now = now or now_utc()
    rows = list((await session.scalars(select(Rule))).all())
    if not any(row.scope == "global" for row in rows):
        global_rule = Rule(scope="global", data={}, version=0)
        session.add(global_rule)
        rows.append(global_rule)
    updated = 0
    for row in rows:
        # 后续新增事件类型也通过 default_event 走即时通知，不逐类别硬编码。
        data = {
            **row.data,
            "enabled": True,
            "default_event": "immediate",
            "quiet_enabled": False,
            "events": {name: "immediate" for name in row.data.get("events", {})},
        }
        if data != row.data:
            row.data, row.version, row.updated_at = data, (row.version or 0) + 1, now
            updated += 1
    released = 0
    deliveries = (
        await session.scalars(select(Delivery).where(Delivery.status.in_(["pending", "retry"])))
    ).all()
    for delivery in deliveries:
        if delivery.payload.get("reminder_keys"):
            continue
        payload = dict(delivery.payload)
        if payload.get("digest"):
            payload["digest"] = False
            payload["rule_text"] = "发现后即时通知"
            delivery.payload = payload
        if delivery.status == "pending" and delivery.available_at > now:
            delivery.available_at = now
            delivery.reason = "通知策略已改为即时发送"
            released += 1
        # SMTP 失败的 retry 保留退避，避免配置切换形成密集重试；accepted 不在查询范围内。
    return {"updated_rule_scopes": updated, "released_pending_deliveries": released}
