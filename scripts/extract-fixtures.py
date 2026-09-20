"""只提取显式白名单字段；不复制 HAR headers、Cookie、姓名、正文、原始 ID 或 URL。"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
IDS = {}


def pseudonym(value):
    if value is None:
        return None
    value = str(value)
    if value not in IDS:
        IDS[value] = str(1000 + len(IDS))
    return IDS[value]


DATES = {
    "due_at",
    "lock_at",
    "unlock_at",
    "submitted_at",
    "posted_at",
    "graded_at",
    "cached_due_date",
    "created_at",
    "updated_at",
    "modified_at",
}
FLAGS = {
    "excused",
    "missing",
    "late",
    "redo_request",
    "grade_matches_current_submission",
    "locked",
    "hidden",
    "hidden_for_user",
}
STATES = {
    "graded",
    "submitted",
    "unsubmitted",
    "pending_review",
    "untaken",
    "complete",
    "available",
    "pending",
}
TYPES = {
    "online_quiz",
    "online_upload",
    "online_text_entry",
    "online_url",
    "on_paper",
    "none",
    "external_tool",
}


def clean(record, kind):
    result = {}
    for key, value in record.items():
        if key in ("id", "assignment_id", "user_id", "course_id", "folder_id"):
            result[key] = pseudonym(value)
        elif key in DATES:
            if value is None or (
                isinstance(value, str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d\d:\d\d)", value)
            ):
                # 先保留以进行全局排序，写出前统一映射到合成时间轴。
                result[key] = value
        elif key in FLAGS and (isinstance(value, bool) or value is None):
            result[key] = value
        elif key == "workflow_state" and value in STATES:
            result[key] = value
        elif key in ("score", "published_score", "entered_score", "points_possible"):
            result[key] = None if value is None else 0 if value == 0 else 72
        elif key in ("grade", "published_grade"):
            result[key] = None if value is None else "0" if str(value) == "0" else "72"
        elif key == "attempt" and isinstance(value, int):
            result[key] = value
        elif key == "submission_types":
            result[key] = [v for v in value if v in TYPES]
        elif key == "submission_type" and (value in TYPES or value is None):
            result[key] = value
        elif key == "size" and isinstance(value, int):
            result[key] = 1024  # 不暴露原始文档尺寸指纹。
    if kind in ("assignment", "file", "announcement", "planner"):
        result["name" if kind == "assignment" else "title"] = f"去敏{kind}-{result.get('id', 'sample')}"
    if kind == "submission":
        result.setdefault("submission_comments", [])
    return result


def body(entry):
    content = entry["response"].get("content", {})
    value = content.get("text", "")
    if content.get("encoding") == "base64":
        try:
            value = base64.b64decode(value).decode()
        except (ValueError, UnicodeDecodeError):
            return ""
    return value


def main():
    bundle = {
        "courses": [],
        "assignments": [],
        "submissions": [],
        "file_pages": [],
        "sources": [],
        "inbox": [],
        "planner": [],
        "announcements": [],
        "graded_detail": {},
        "empty_resources": [],
    }
    for path in sorted((ROOT / "har").glob("*.har")):
        raw = path.read_bytes()
        entries = json.loads(raw)["log"]["entries"]
        evidence = {
            "file": path.name,
            "selected": [],
        }
        for index, entry in enumerate(entries):
            endpoint = urlsplit(entry["request"]["url"]).path
            content = body(entry)
            try:
                data = json.loads(content)
            except (ValueError, TypeError):
                data = None
            if (
                path.name == "02_assignment.har"
                and endpoint.endswith("/assignment_groups")
                and isinstance(data, list)
            ):
                cid = pseudonym(endpoint.split("/")[4])
                if cid not in [course["id"] for course in bundle["courses"]]:
                    bundle["courses"].append({"id": cid, "name": f"去敏课程 {len(bundle['courses']) + 1}"})
                    assignments = [a for group in data for a in group.get("assignments", [])]
                    bundle["assignments"].extend(
                        [{**clean(a, "assignment"), "course_id": cid} for a in assignments]
                    )
                    evidence["selected"].append(
                        {"entry": index, "kind": "assignments", "count": len(assignments)}
                    )
            if (
                path.name == "02_assignment.har"
                and endpoint.endswith("/students/submissions")
                and isinstance(data, list)
            ):
                cid = pseudonym(endpoint.split("/")[4])
                bundle["submissions"].extend([{**clean(a, "submission"), "course_id": cid} for a in data])
                evidence["selected"].append({"entry": index, "kind": "submissions", "count": len(data)})
            if (
                path.name == "canvas.tongji.edu.cn.har"
                and index in (2641, 2647, 2677, 2705)
                and isinstance(data, list)
            ):
                bundle["file_pages"].append([clean(a, "file") for a in data])
                evidence["selected"].append({"entry": index, "kind": "files", "count": len(data)})
            if endpoint.endswith("/conversations") and data == []:
                evidence["selected"].append({"entry": index, "kind": "empty_inbox"})
            if path.name == "06_module_page.har" and data == []:
                bundle["empty_resources"].append({"status": entry["response"]["status"], "entry": index})
            if path.name == "03_graded_assignment.har" and not bundle["graded_detail"]:
                # 网页内嵌 JSON 仅抽取已确认的评分字段，不导出 HTML 本身。
                match = re.search(r'"published_score"\s*:\s*(-?\d+(?:\.\d+)?)', content)
                if match:
                    window = content[max(0, match.start() - 2500) : match.end() + 2500]
                    detail = {}
                    for key in (
                        "published_score",
                        "published_grade",
                        "score",
                        "grade",
                        "attempt",
                        "posted_at",
                        "graded_at",
                        "grade_matches_current_submission",
                    ):
                        found = re.search(
                            r'"' + key + r'"\s*:\s*("[^"\n]*"|-?\d+(?:\.\d+)?|true|false|null)', window
                        )
                        if found:
                            detail[key] = json.loads(found[1])
                    bundle["graded_detail"] = clean(detail, "submission")
                    evidence["selected"].append({"entry": index, "kind": "visible_grade"})
            if (
                path.name == "01_profile_settings.har"
                and endpoint == "/profile/tokens"
                and isinstance(data, dict)
            ):
                # 只记录存在与时间关系，不输出 token 内容。
                evidence["token_metadata"] = {
                    "created": "created_at" in data,
                    "expires": "expires_at" in data,
                    "permanent_expiry_null": data.get("permanent_expires_at") is None,
                }
        bundle["sources"].append(evidence)
    if not bundle["assignments"] or not bundle["submissions"]:
        raise SystemExit("Private HAR inputs are missing; existing public fixtures were not changed")
    dates = set()

    def collect(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in DATES and value:
                    dates.add(value)
                else:
                    collect(value)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)

    collect(bundle)
    anchor = datetime(2030, 1, 1, tzinfo=UTC)
    replacement = {
        value: (anchor + timedelta(hours=index)).isoformat()
        for index, value in enumerate(
            sorted(dates, key=lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")))
        )
    }

    def scrub(obj):
        if isinstance(obj, dict):
            obj.pop("entry", None)
            for key, value in obj.items():
                if key in DATES and value:
                    obj[key] = replacement[value]
                else:
                    scrub(value)
        elif isinstance(obj, list):
            for item in obj:
                scrub(item)

    scrub(bundle)
    target = ROOT / "tests/fixtures/har-sanitized.json"
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "assignments": len(bundle["assignments"]),
                "submissions": len(bundle["submissions"]),
                "file_page_counts": [len(p) for p in bundle["file_pages"]],
                "sources": len(bundle["sources"]),
                "graded_fields": list(bundle["graded_detail"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
