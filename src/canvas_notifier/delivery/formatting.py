from zoneinfo import ZoneInfo

import bleach

from canvas_notifier.domain.time import parse_time

NAMES = {
    "due_at": "正式截止",
    "lock_at": "停止提交",
    "unlock_at": "开放时间",
    "submitted_at": "提交时间",
    "name": "名称",
    "title": "标题",
    "description": "任务说明",
    "message": "正文",
    "body": "正文",
    "grade": "成绩",
    "score": "分数",
    "published_score": "可见分数",
    "published_grade": "可见成绩",
    "workflow_state": "状态",
    "points_possible": "满分",
    "rubric_assessment": "评分量表",
    "coalesced_count": "合并的提醒点",
    "status": "状态",
    "scope": "受影响范围",
}


def readable(value, timezone):
    if value is None:
        return "未设置"
    if isinstance(value, dict):
        return "，".join(f"{NAMES.get(k, k)}：{readable(v, timezone)}" for k, v in value.items())
    if isinstance(value, list):
        return "、".join(readable(v, timezone) for v in value)
    if isinstance(value, str):
        try:
            stamp = parse_time(value)
            if stamp:
                return stamp.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            pass
        return bleach.clean(value, tags=set(), strip=True)[:1500]
    return str(value)


def format_changes(changes, timezone):
    lines = []
    for key, value in changes.items():
        if key in ("items", "future_tasks") and isinstance(value, list):
            for item in value:
                lines.append(" · ".join(str(item[k]) for k in ("label", "title", "name") if item.get(k)))
                for field in ("due_at", "lock_at"):
                    if item.get(field):
                        lines.append(f"  {NAMES[field]}：{readable(item[field], timezone)}")
                if item.get("link"):
                    lines.append(item["link"])
        elif key in ("comment", "message") and isinstance(value, dict):
            lines.append(readable(value.get("comment") or value.get("body") or "", timezone))
        elif isinstance(value, dict) and set(value) == {"before", "after"}:
            lines.append(
                f"{NAMES.get(key, key)}：{readable(value['before'], timezone)} → {readable(value['after'], timezone)}"
            )
        else:
            lines.append(f"{NAMES.get(key, key)}：{readable(value, timezone)}")
    return "\n".join(lines)
