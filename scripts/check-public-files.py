"""扫描 Git 候选文件；只输出文件名和问题类型，绝不打印命中的凭据。"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    raw = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    paths = sorted(set(name for name in raw.decode().split("\0") if name))
    secrets = []
    for path in (ROOT / "secrets").glob("*"):
        if not path.is_file():
            continue
        value = path.read_text().strip()
        if len(value) > 8 or (path.name == "iam-username" and value):
            secrets.append(value)
        if "cookie" in path.name:
            try:
                secrets.extend(item["value"] for item in json.loads(value) if len(item.get("value", "")) > 8)
            except (ValueError, TypeError, KeyError):
                pass
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"SMTP_USERNAME", "MAIL_FROM", "MAIL_TO", "MAIL_TEST_TO"}:
                secrets.extend(address.strip().strip("\"'") for address in value.split(",") if "@" in address)
    issues = []
    for name in paths:
        path = ROOT / name
        if not path.is_file():
            continue
        if (
            name.startswith(("har/", "secrets/", ".local/"))
            or name.endswith(".har")
            or ("Canvas" in name and "Plan" in name)
            or (name.startswith(".env") and name != ".env.example")
        ):
            issues.append({"file": name, "reason": "private_path_tracked_or_unignored"})
            continue
        data = path.read_bytes()
        if any(value.encode() in data for value in secrets):
            issues.append({"file": name, "reason": "credential_value_found"})
        if re.search(rb'(?i)(?:authorization|cookie)["\s]*:["\s]*(?:Bearer\s+)[A-Za-z0-9_+/=-]{30,}', data):
            issues.append({"file": name, "reason": "literal_auth_header_found"})
    must_ignore = [
        "har/example.har",
        "Tongji_Canvas_Enhancement_Plan-2.md",
        "secrets/canvas-token",
        ".env",
        ".local/state.db",
    ]
    for name in must_ignore:
        result = subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=ROOT)
        if result.returncode:
            issues.append({"file": name, "reason": "missing_ignore_rule"})
    print(json.dumps({"candidate_files": len(paths), "issues": issues}, ensure_ascii=False, indent=2))
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())
