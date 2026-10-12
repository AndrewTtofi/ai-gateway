"""PostgresDirectory against a real Postgres (migration 0010)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from app.db import make_engine, make_sessions
from app.directory import CachedIdentities, Conflict, Group, PostgresDirectory, User
from tests.test_keys_postgres import URL, store  # noqa: F401 — migrates the test DB


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def directory(store: Any) -> AsyncIterator[PostgresDirectory]:  # noqa: F811
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE directory_members, directory_groups, directory_users CASCADE")
        )
    yield PostgresDirectory(make_sessions(engine))
    await engine.dispose()


async def test_users_round_trip_and_usernames_are_unique(directory: PostgresDirectory) -> None:
    alice = await directory.create_user(
        User(new_id(), "alice@example.com", external_id="ext-a", display_name="Alice")
    )
    assert await directory.get_user(alice.id) == await directory.find_user("external_id", "ext-a")
    found = await directory.find_user("user_name", "alice@example.com")
    assert found is not None and (found.active, found.display_name) == (True, "Alice")
    with pytest.raises(Conflict):
        await directory.create_user(User(new_id(), "alice@example.com"))
    assert await directory.get_user("not-a-uuid") is None

    off = await directory.update_user(alice.id, {"active": False})
    assert off is not None and off.active is False and off.updated_at >= alice.updated_at
    await directory.create_user(User(new_id(), "bob@example.com"))
    with pytest.raises(Conflict):
        await directory.update_user(alice.id, {"user_name": "bob@example.com"})

    total, page = await directory.list_users(None, None, 2, 5)
    assert total == 2 and [u.user_name for u in page] == ["bob@example.com"]
    total, page = await directory.list_users("user_name", "bob@example.com", 1, 5)
    assert total == 1 and page[0].user_name == "bob@example.com"


async def test_memberships_follow_users_and_groups(directory: PostgresDirectory) -> None:
    a = (await directory.create_user(User(new_id(), "a@example.com"))).id
    b = (await directory.create_user(User(new_id(), "b@example.com"))).id
    unknown = new_id()
    g = await directory.create_group(Group(new_id(), "compliance", members=(a, unknown)))
    assert g.members == (a,)  # ids the directory doesn't know are skipped
    with pytest.raises(Conflict):
        await directory.create_group(Group(new_id(), "compliance"))

    added = await directory.update_group(g.id, add=[b, a])  # a is already in: no duplicate
    assert added is not None and set(added.members) == {a, b}
    assert [x.display_name for x in await directory.groups_of(a)] == ["compliance"]

    removed = await directory.update_group(g.id, remove=[a])
    assert removed is not None and removed.members == (b,)
    replaced = await directory.update_group(g.id, display_name="trading-desk", members=[a])
    assert replaced is not None
    assert (replaced.display_name, replaced.members) == ("trading-desk", (a,))

    assert await directory.delete_user(a) is True  # memberships go with the user
    left = await directory.get_group(g.id)
    assert left is not None and left.members == ()
    assert await directory.delete_group(g.id) is True
    assert await directory.get_group(g.id) is None and await directory.delete_group(g.id) is False
    total, _ = await directory.list_groups(None, 1, 10)
    assert total == 0


async def test_the_identity_cache_reads_groups_and_is_dropped_on_demand(
    directory: PostgresDirectory,
) -> None:
    a = (await directory.create_user(User(new_id(), "a@example.com"))).id
    g = await directory.create_group(Group(new_id(), "eng-backend", members=(a,)))
    cache = CachedIdentities(directory)
    first = await cache.lookup("user_name", "a@example.com")
    assert first is not None and first.groups == ("eng-backend",)
    await directory.update_group(g.id, remove=[a])
    stale = await cache.lookup("user_name", "a@example.com")
    assert stale is not None and stale.groups == ("eng-backend",)  # within the TTL
    cache.invalidate()  # what a SCIM write does
    fresh = await cache.lookup("user_name", "a@example.com")
    assert fresh is not None and fresh.groups == ()
    assert await cache.lookup("user_name", "nobody@example.com") is None
