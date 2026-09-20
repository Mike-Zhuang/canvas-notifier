import pytest

from canvas_notifier.config import Settings, write_secret
from canvas_notifier.db import create_schema, database


@pytest.fixture
def settings(tmp_path):
    write_secret(tmp_path / "token", "synthetic-token")
    write_secret(tmp_path / "admin", "synthetic-admin-password")
    write_secret(tmp_path / "session", "synthetic-session-key-longer-than-thirty-two-characters")
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///" + str(tmp_path / "test.db"),
        canvas_token_file=tmp_path / "token",
        canvas_cookie_file=tmp_path / "cookies.json",
        admin_password_file=tmp_path / "admin",
        session_secret_file=tmp_path / "session",
        smtp_host="127.0.0.1",
        smtp_port=1025,
        smtp_tls_mode="none",
        smtp_username="",
        smtp_password_file=tmp_path / "missing",
        mail_from="canvas@example.test",
        mail_to="student@example.test",
        mail_test_to="student@example.test",
        request_interval=0,
        iam_username_file=tmp_path / "iam-username",
        iam_password_file=tmp_path / "iam-password",
        iam_cookie_file=tmp_path / "iam-cookies.json",
    )


@pytest.fixture
async def sessions(settings):
    import os
    import uuid

    from sqlalchemy import text

    test_url = os.environ.get("TEST_DATABASE_URL")
    engine, factory = database(test_url or settings.database_url)
    schema = None
    if test_url:
        if not test_url.startswith("postgresql+"):
            raise ValueError("TEST_DATABASE_URL must be PostgreSQL")
        schema = "test_" + uuid.uuid4().hex
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = engine.execution_options(schema_translate_map={None: schema})
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
    await create_schema(engine)
    try:
        yield factory
    finally:
        if schema:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
