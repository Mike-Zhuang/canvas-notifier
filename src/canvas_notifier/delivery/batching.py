"""一轮一课一封邮件；关联资源保留为同一事项的补充记录，不丢事件。"""


def related_items(payloads):
    groups = {}
    priority = {"assignment": 0, "file": 0, "submission": 1, "module_item": 2, "planner": 3}
    for index, payload in enumerate(payloads):
        groups.setdefault(payload.get("entity_key") or payload.get("resource_key") or str(index), []).append(
            payload
        )
    items = []
    for group in groups.values():
        ordered = sorted(group, key=lambda p: priority.get(p.get("resource_kind"), 1))
        items.append({**ordered[0], "related_items": [dict(p) for p in ordered[1:]]})
    return items


def batch_payload(rows):
    primary = rows[0].payload
    course_batch = bool(primary.get("course_batch")) and not primary.get("digest")
    items = related_items([row.payload for row in rows])
    return {
        **primary,
        "kind": "course_batch" if course_batch else "digest",
        "label": "课程更新" if course_batch else "更新摘要",
        "title": f"{len(items)} 项更新"
        + (f"（含 {len(rows)} 条关联记录）" if len(items) != len(rows) else ""),
        "course": primary.get("course", "") if course_batch else "",
        "digest_items": items,
        "related_items": [],
        "excerpt": "",
        "link": "",
        "file_size": None,
        "filename": None,
        "content_type": None,
        "folder_id": None,
        "resource_kind": "",
        "content_images": [],
        "is_assignment": False,
        "points_possible": None,
        "due_at": None,
        "lock_at": None,
        "unlock_at": None,
        "personal_at": None,
        "submitted_at": None,
        "boundary": None,
        "source_created_at": None,
        "source_modified_at": None,
        "source_updated_at": None,
        "source_published_at": None,
        "author": None,
        "author_email": None,
        "author_profile_url": None,
        "author_avatar_url": None,
        "feedback_created_at": None,
        "feedback_edited_at": None,
        "media_kind": None,
        "media_name": None,
        "attachments": [],
        "changes": {},
        "rule_text": "同轮课程更新合并，逐项保留完整内容与关联变化" if course_batch else "按配置合并摘要",
    }
