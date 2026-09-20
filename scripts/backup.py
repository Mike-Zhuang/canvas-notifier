"""备份私有状态；可在隔离的本地 PostgreSQL 数据库验证恢复。"""

import argparse
import asyncio
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import make_url

from canvas_notifier.config import Settings
from canvas_notifier.db import database


async def counts(url):
    engine, sessions = database(url)
    try:
        async with sessions() as session:
            return {
                table: await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("resources", "events", "reminder_jobs", "outbox", "notification_rules")
            }
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    url = make_url(settings.database_url)
    directory = settings.backup_directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if url.get_backend_name() == "sqlite":
        target = directory / (name + ".db")
        with sqlite3.connect(url.database) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        target.chmod(0o600)
        if args.verify:
            with sqlite3.connect(target) as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        print({"backup": str(target), "restore_verified": args.verify})
        return
    if url.host not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("This local backup verifier only supports loopback PostgreSQL")
    target = directory / (name + ".dump")
    environment = os.environ.copy()
    if url.password:
        environment["PGPASSWORD"] = url.password
    connection = ["-h", url.host, "-p", str(url.port or 5432), "-U", url.username]
    subprocess.run(
        ["pg_dump", *connection, "-Fc", "-f", str(target), url.database], env=environment, check=True
    )
    target.chmod(0o600)
    if args.verify:
        restore_name = "canvas_restore_" + uuid.uuid4().hex[:12]
        subprocess.run(["createdb", *connection, restore_name], env=environment, check=True)
        try:
            subprocess.run(
                ["pg_restore", *connection, "--no-owner", "--no-privileges", "-d", restore_name, str(target)],
                env=environment,
                check=True,
            )
            original = asyncio.run(counts(settings.database_url))
            restored = asyncio.run(
                counts(url.set(database=restore_name).render_as_string(hide_password=False))
            )
            assert original == restored, (
                "Source changed during restore verification; retry with worker stopped"
            )
            print({"backup": str(target), "restore_verified": True, "table_counts": restored})
        finally:
            subprocess.run(["dropdb", *connection, restore_name], env=environment, check=True)
    else:
        print({"backup": str(target), "restore_verified": False})


if __name__ == "__main__":
    main()
