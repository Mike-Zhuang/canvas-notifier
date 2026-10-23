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
    "position": "列表排序位置（不是题目行号）",
    "locked_for_user": "对你锁定访问",
    "locked": "已锁定",
    "hidden": "已隐藏",
    "hidden_for_user": "对你隐藏",
    "published": "已发布",
    "content_images": "正文图片",
    "items_count": "模块内容数量",
    "files_count": "文件数量",
    "folders_count": "子文件夹数量",
    "folder_id": "所在文件夹 ID",
    "parent_folder_id": "父文件夹 ID",
    "full_name": "完整目录名",
    "filename": "原始文件名",
    "display_name": "显示名称",
    "size": "文件大小（字节）",
    "content-type": "文件类型",
    "modified_at": "文件修改时间",
    "updated_at": "更新时间",
    "created_at": "创建时间",
    "submission_types": "允许的提交方式",
    "submissions": "提交记录状态",
    "submitted": "已提交",
    "excused": "已豁免",
    "graded": "已评分",
    "graded_at": "评分时间",
    "posted_at": "发布时间",
    "late": "迟交",
    "missing": "缺交",
    "needs_grading": "等待评分",
    "has_feedback": "有反馈",
    "redo_request": "要求重新提交",
    "attempt": "提交次数",
    "grade_matches_current_submission": "成绩对应当前提交",
    "cached_due_date": "提交记录中的截止时间",
    "submission_type": "提交方式",
    "grade_hidden": "成绩隐藏",
    "start_at": "开始时间",
    "end_at": "结束时间",
    "all_day": "全天日程",
    "all_day_date": "全天日程日期",
    "location_name": "地点",
    "context_code": "所属课程或用户",
    "context_type": "所属范围类型",
    "course_code": "课程编号",
    "term": "学期",
    "id": "编号",
    "file_id": "图片文件编号",
    "url": "链接",
    "html_url": "查看链接",
    "plannable": "计划事项",
    "plannable_id": "计划事项编号",
    "plannable_type": "计划事项类型",
    "plannable_date": "计划时间",
    "todo_date": "待办时间",
    "details": "详细说明",
    "is_quiz_assignment": "测验作业",
    "quiz_id": "测验编号",
    "has_overrides": "有个人或分组日期覆盖",
    "only_visible_to_overrides": "仅指定学生可见",
    "completion_requirement": "完成条件",
    "completed": "已完成",
    "completed_at": "完成时间",
    "state": "状态",
    "type": "内容类型",
    "content_id": "关联内容编号",
    "prerequisite_module_ids": "前置模块编号",
    "require_sequential_progress": "要求按顺序学习",
    "last_reply_at": "最近回复时间",
    "discussion_subentry_count": "讨论回复数量",
    "is_announcement": "公告",
    "subject": "主题",
    "last_message_at": "最近消息时间",
    "message_count": "消息数量",
    "before": "变更前",
    "after": "变更后",
    "parent_id": "上级编号",
    "user_id": "用户编号",
    "editing_roles": "可编辑角色",
    "auth_rejected": "认证已失效",
    "server_error": "学校接口服务异常",
    "network_error": "网络连接失败",
    "resource_unavailable": "资源接口不可用",
    "resource_forbidden": "无权读取该资源",
    "iam_interaction_required": "需要本人完成增强认证",
    "iam_rejected": "IAM 拒绝登录",
    "iam_retry_cooldown": "等待 IAM 重试间隔",
}

VALUES = {
    "unsubmitted": "尚未提交",
    "submitted": "已提交",
    "pending_review": "等待审核",
    "graded": "已评分",
    "published": "已发布",
    "unpublished": "未发布",
    "available": "可用",
    "locked": "已锁定",
    "unlocked": "已开放",
    "completed": "已完成",
    "started": "已开始",
    "active": "进行中",
    "online_upload": "上传文件",
    "online_text_entry": "在线文本",
    "online_quiz": "在线测验",
    "online_url": "提交网址",
    "on_paper": "纸质或线下提交",
    "none": "无需在线提交",
    "external_tool": "外部教学工具",
    "not_graded": "不计分",
    "media_recording": "音视频录制",
    "student_annotation": "文件批注",
    "File": "文件",
    "Assignment": "作业",
    "Page": "课程页面",
    "Discussion": "讨论",
    "ExternalUrl": "外部链接",
    "ExternalTool": "外部工具",
    "assignment": "作业",
    "course": "课程",
    "Course": "课程",
    "ok": "正常",
    "partial": "部分资源不可用",
}


def readable(value, timezone):
    if value is None:
        return "未设置"
    if isinstance(value, bool):
        return "是" if value else "否"
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
        text = bleach.clean(value, tags=set(), strip=True)
        return VALUES.get(text, NAMES.get(text, text))
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
