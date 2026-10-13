"""Operator jobs against a real Postgres (same database and migration as test_keys_postgres)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from app import config, maintenance
from app.auth import PostgresKeyStore
from app.db import make_engine
from tests.test_keys_postgres import URL, store  # noqa: F401  (the migrated-database fixture)

ROLE = "gw_test_readonly"
PASSWORD = "a-long-test-password-1234"  # noqa: S105 (a throwaway test role)


@pytest.fixture
async def db(store: PostgresKeyStore, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:  # noqa: F811
    monkeypatch.setattr(config.settings, "database_url", URL)
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE usage_log, decision_log"))
    yield
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE usage_log, decision_log"))
        if await conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}):
            db_name = await conn.scalar(text("SELECT current_database()"))
            await conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}"))
            await conn.execute(text(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}"))
            await conn.execute(text(f'REVOKE CONNECT ON DATABASE "{db_name}" FROM {ROLE}'))
            await conn.execute(text(f"DROP ROLE {ROLE}"))
    await engine.dispose()


async def insert_rows(ages_days: list[int]) -> None:
    engine = make_engine(URL)
    async with engine.begin() as conn:
        for age in ages_days:
            await conn.execute(
                text(
                    "INSERT INTO usage_log (created_at, request_id, key_prefix, alias, status, "
                    "streamed, fallback, attempts, prompt_tokens, completion_tokens, "
                    "cached_tokens, usage_estimated, latency_ms) VALUES "
                    "(now() - make_interval(days => :age), 'r', 'gw_x', 'a', 200, "
                    "false, false, 1, 1, 1, 0, false, 1)"
                ),
                {"age": age},
            )
    await engine.dispose()


async def count_rows() -> int:
    engine = make_engine(URL)
    async with engine.connect() as conn:
        n = int(await conn.scalar(text("SELECT count(*) FROM usage_log")) or 0)
    await engine.dispose()
    return n


async def test_prune_deletes_only_old_rows_in_batches(db: None) -> None:
    await insert_rows([1, 5, 100, 200, 400])
    assert await maintenance.prune(days=90, batch=2) == 3  # several batches
    assert await count_rows() == 2


async def insert_decisions(ages_days: list[int]) -> None:
    engine = make_engine(URL)
    async with engine.begin() as conn:
        for age in ages_days:
            await conn.execute(
                text(
                    "INSERT INTO decision_log (at, request_id, subject, subject_kind, "
                    "subject_name, roles, tier, action, data_class, decision, code, reason, "
                    "policy_version, detail) VALUES (now() - make_interval(days => :age), "
                    "'r', 'key:1', 'key', 'k', '[]', 'standard', 'model:a', 'internal', "
                    "'allow', 'ok', 'ok', '1', '{}')"
                ),
                {"age": age},
            )
    await engine.dispose()


async def count_decisions() -> int:
    engine = make_engine(URL)
    async with engine.connect() as conn:
        n = int(await conn.scalar(text("SELECT count(*) FROM decision_log")) or 0)
    await engine.dispose()
    return n


async def test_prune_decisions_deletes_only_old_rows_and_leaves_usage(db: None) -> None:
    await insert_decisions([1, 5, 100, 200, 400])
    await insert_rows([400])
    assert await maintenance.prune_decisions(days=90, batch=2) == 3  # several batches
    assert await count_decisions() == 2
    assert await count_rows() == 1  # a different table's retention is its own


async def test_prune_decisions_dry_run_deletes_nothing(db: None) -> None:
    await insert_decisions([1, 100, 400])
    assert await maintenance.prune_decisions(days=90, dry_run=True) == 2
    assert await count_decisions() == 3


async def test_readonly_role_cannot_read_key_hashes(db: None) -> None:
    await maintenance.grant_readonly(ROLE, PASSWORD)
    await maintenance.grant_readonly(ROLE, PASSWORD)  # idempotent (ALTER the second time)
    ro_url = URL.replace("://gateway:gateway@", f"://{ROLE}:{PASSWORD}@")
    engine = make_engine(ro_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT count(*) FROM usage_log"))
            await conn.execute(text("SELECT count(*) FROM judge_scores"))  # the quality panel
            await conn.execute(text("SELECT id, name, team FROM api_keys"))
        with pytest.raises(Exception, match="permission denied"):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT key_hash FROM api_keys"))
        with pytest.raises(Exception, match="permission denied"):
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM usage_log"))
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("role", "password"), [("Bad-Role", PASSWORD), (ROLE, "short"), (ROLE, "x" * 16 + "\x00")]
)
async def test_grant_validates_inputs(role: str, password: str) -> None:
    with pytest.raises(ValueError):
        await maintenance.grant_readonly(role, password)


def test_password_is_sent_as_a_scram_verifier() -> None:
    v = maintenance.scram_verifier("it's a 'secret' \\ password")
    assert v.startswith("SCRAM-SHA-256$4096:") and "secret" not in v
    maintenance._literal(v)  # only safe characters
    with pytest.raises(ValueError):
        maintenance._literal("x'; DROP TABLE api_keys; --")


def test_identifiers_are_quoted() -> None:
    assert maintenance._ident("user") == '"user"' and maintenance._ident('a"b') == '"a""b"'


def test_bad_batch_sizes_are_rejected() -> None:
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(maintenance.prune(days=1, batch=0))
    with pytest.raises(SystemExit):
        maintenance.main(["prune", "--days", "1", "--batch", "0"])


def test_prune_decisions_rejects_bad_arguments() -> None:
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(maintenance.prune_decisions(days=0))
    with pytest.raises(SystemExit):
        maintenance.main(["prune-decisions", "--days", "0"])
    with pytest.raises(SystemExit):  # no default age: the audit record is the operator's call
        maintenance.main(["prune-decisions"])
