from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    backup_directory: Path = Path(".local/backups")
    database_url: str = "sqlite+aiosqlite:///.local/canvas.db"
    canvas_base_url: str = "https://canvas.tongji.edu.cn"
    canvas_token_file: Path = Path("secrets/canvas-token")
    canvas_cookie_file: Path = Path("secrets/canvas-cookies.json")
    canvas_auth_mode: Literal["token", "cookie"] = "token"
    canvas_cookie_fallback: bool = False
    canvas_cookie_resources: str = ""
    iam_auto_login: bool = False
    iam_cookie_file: Path = Path("secrets/iam-cookies.json")
    iam_username_file: Path = Path("secrets/iam-username")
    iam_password_file: Path = Path("secrets/iam-password")
    iam_timeout_seconds: int = Field(default=90, ge=10, le=300)
    iam_retry_seconds: int = Field(default=900, ge=60, le=86400)
    public_base_url: str = "http://127.0.0.1:8000"
    admin_password_file: Path = Path("secrets/admin-password")
    session_secret_file: Path = Path("secrets/session-key")
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_tls_mode: Literal["none", "starttls", "tls"] = "none"
    smtp_username: str = ""
    smtp_password_file: Path = Path("secrets/smtp-password")
    mail_from: str = "canvas@localhost.test"
    mail_to: str = ""
    mail_test_to: str = ""
    display_timezone: str = "Asia/Shanghai"
    poll_seconds: int = 300
    content_poll_seconds: int = 900
    historical_poll_seconds: int = 3600
    request_interval: float = 0.2
    max_pages: int = 1000

    @field_validator("canvas_base_url")
    @classmethod
    def canvas_origin(cls, value: str) -> str:
        p = urlsplit(value)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.path not in ("", "/")
            or p.query
            or p.fragment
        ):
            raise ValueError("Canvas must be an HTTPS origin")
        return value.rstrip("/")

    @field_validator("mail_from", "mail_to", "mail_test_to", "smtp_username")
    @classmethod
    def no_header_injection(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Newlines are not allowed")
        return value


def read_secret(path: Path) -> str:
    if not path.exists():
        return ""
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Secret file must have mode 600: {path.name}")
    return path.read_text().strip()


def write_secret(path: Path, value: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
