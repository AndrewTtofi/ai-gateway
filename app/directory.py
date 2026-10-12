"""The directory: users and groups provisioned by the identity provider (ADR 0029).

SCIM (`app/scim.py`) writes it; authentication reads it. A caller's groups come from
here, not from token claims, so removing someone from a group or deactivating them in the
identity provider takes effect on their next request, not when their token expires.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import DirectoryGroupRow, DirectoryMemberRow, DirectoryUserRow

UserAttr = Literal["user_name", "external_id"]


class Conflict(Exception):
    """A unique value (userName, a group's displayName) is already taken."""


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class User:
    id: str
    user_name: str
    external_id: str | None = None
    active: bool = True
    display_name: str | None = None
    email: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class Group:
    id: str
    display_name: str
    external_id: str | None = None
    members: tuple[str, ...] = ()  # user ids
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


USER_FIELDS = ("user_name", "external_id", "active", "display_name", "email")


class Directory(Protocol):
    async def create_user(self, user: User) -> User: ...
    async def get_user(self, user_id: str) -> User | None: ...
    async def find_user(self, attr: UserAttr, value: str) -> User | None: ...
    async def list_users(
        self, attr: UserAttr | None, value: str | None, start: int, count: int
    ) -> tuple[int, list[User]]: ...
    async def update_user(self, user_id: str, changes: dict[str, Any]) -> User | None: ...
    async def delete_user(self, user_id: str) -> bool: ...
    async def groups_of(self, user_id: str) -> list[Group]: ...

    async def create_group(self, group: Group) -> Group: ...
    async def get_group(self, group_id: str) -> Group | None: ...
    async def list_groups(
        self, display_name: str | None, start: int, count: int
    ) -> tuple[int, list[Group]]: ...
    async def update_group(
        self,
        group_id: str,
        display_name: str | None = None,
        external_id: str | None = None,
        members: list[str] | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> Group | None: ...
    async def delete_group(self, group_id: str) -> bool: ...


def _page[T](items: list[T], start: int, count: int) -> tuple[int, list[T]]:
    return len(items), items[max(start, 1) - 1 : max(start, 1) - 1 + count]


class MemoryDirectory:
    """For tests and `GATEWAY_STORES=memory`."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.groups: dict[str, Group] = {}

    async def create_user(self, user: User) -> User:
        if any(u.user_name == user.user_name for u in self.users.values()):
            raise Conflict("userName")
        self.users[user.id] = user
        return user

    async def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    async def find_user(self, attr: UserAttr, value: str) -> User | None:
        return next((u for u in self.users.values() if getattr(u, attr) == value), None)

    async def list_users(
        self, attr: UserAttr | None, value: str | None, start: int, count: int
    ) -> tuple[int, list[User]]:
        users = sorted(self.users.values(), key=lambda u: u.user_name)
        if attr is not None:
            users = [u for u in users if getattr(u, attr) == value]
        return _page(users, start, count)

    async def update_user(self, user_id: str, changes: dict[str, Any]) -> User | None:
        user = self.users.get(user_id)
        if user is None:
            return None
        name = changes.get("user_name")
        if name and any(u.user_name == name and u.id != user_id for u in self.users.values()):
            raise Conflict("userName")
        self.users[user_id] = replace(user, **changes, updated_at=_now())
        return self.users[user_id]

    async def delete_user(self, user_id: str) -> bool:
        if self.users.pop(user_id, None) is None:
            return False
        for gid, g in self.groups.items():
            if user_id in g.members:
                self.groups[gid] = replace(g, members=tuple(m for m in g.members if m != user_id))
        return True

    async def groups_of(self, user_id: str) -> list[Group]:
        return [g for g in self.groups.values() if user_id in g.members]

    def _known(self, ids: list[str]) -> tuple[str, ...]:
        """Members that exist, once each, in the order given. Ids the directory doesn't
        know are skipped: they grant nothing, and a group push mustn't fail on them."""
        return tuple(dict.fromkeys(i for i in ids if i in self.users))

    async def create_group(self, group: Group) -> Group:
        if any(g.display_name == group.display_name for g in self.groups.values()):
            raise Conflict("displayName")
        self.groups[group.id] = replace(group, members=self._known(list(group.members)))
        return self.groups[group.id]

    async def get_group(self, group_id: str) -> Group | None:
        return self.groups.get(group_id)

    async def list_groups(
        self, display_name: str | None, start: int, count: int
    ) -> tuple[int, list[Group]]:
        groups = sorted(self.groups.values(), key=lambda g: g.display_name)
        if display_name is not None:
            groups = [g for g in groups if g.display_name == display_name]
        return _page(groups, start, count)

    async def update_group(
        self,
        group_id: str,
        display_name: str | None = None,
        external_id: str | None = None,
        members: list[str] | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> Group | None:
        g = self.groups.get(group_id)
        if g is None:
            return None
        if display_name and any(
            o.display_name == display_name and o.id != group_id for o in self.groups.values()
        ):
            raise Conflict("displayName")
        current = list(g.members) if members is None else list(members)
        current = [m for m in [*current, *(add or [])] if m not in set(remove or [])]
        self.groups[group_id] = replace(
            g,
            display_name=display_name or g.display_name,
            external_id=external_id if external_id is not None else g.external_id,
            members=self._known(current),
            updated_at=_now(),
        )
        return self.groups[group_id]

    async def delete_group(self, group_id: str) -> bool:
        return self.groups.pop(group_id, None) is not None


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _user(row: DirectoryUserRow) -> User:
    return User(
        id=str(row.id),
        user_name=row.user_name,
        external_id=row.external_id,
        active=row.active,
        display_name=row.display_name,
        email=row.email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresDirectory:
    def __init__(self, sessions: async_sessionmaker[Any]) -> None:
        self.sessions = sessions

    async def create_user(self, user: User) -> User:
        row = DirectoryUserRow(
            id=uuid.UUID(user.id),
            created_at=user.created_at,
            updated_at=user.updated_at,
            **{f: getattr(user, f) for f in USER_FIELDS},
        )
        try:
            async with self.sessions() as s, s.begin():
                s.add(row)
        except IntegrityError as exc:
            raise Conflict("userName") from exc
        return user

    async def get_user(self, user_id: str) -> User | None:
        uid = _uuid(user_id)
        if uid is None:
            return None
        async with self.sessions() as s:
            row = await s.get(DirectoryUserRow, uid)
            return _user(row) if row else None

    async def find_user(self, attr: UserAttr, value: str) -> User | None:
        column = getattr(DirectoryUserRow, attr)
        async with self.sessions() as s:
            row = (
                await s.execute(select(DirectoryUserRow).where(column == value).limit(1))
            ).scalar_one_or_none()
            return _user(row) if row else None

    async def list_users(
        self, attr: UserAttr | None, value: str | None, start: int, count: int
    ) -> tuple[int, list[User]]:
        query = select(DirectoryUserRow)
        total = select(func.count()).select_from(DirectoryUserRow)
        if attr is not None:
            condition = getattr(DirectoryUserRow, attr) == value
            query, total = query.where(condition), total.where(condition)
        query = query.order_by(DirectoryUserRow.user_name).offset(max(start, 1) - 1).limit(count)
        async with self.sessions() as s:
            n = int(await s.scalar(total) or 0)
            return n, [_user(r) for r in await s.scalars(query)]

    async def update_user(self, user_id: str, changes: dict[str, Any]) -> User | None:
        uid = _uuid(user_id)
        if uid is None:
            return None
        values = {k: v for k, v in changes.items() if k in USER_FIELDS}
        try:
            async with self.sessions() as s, s.begin():
                result = await s.execute(
                    update(DirectoryUserRow)
                    .where(DirectoryUserRow.id == uid)
                    .values(**values, updated_at=_now())
                )
                if not getattr(result, "rowcount", 0):
                    return None
        except IntegrityError as exc:
            raise Conflict("userName") from exc
        return await self.get_user(user_id)

    async def delete_user(self, user_id: str) -> bool:
        uid = _uuid(user_id)
        if uid is None:
            return False
        async with self.sessions() as s, s.begin():
            await s.execute(delete(DirectoryMemberRow).where(DirectoryMemberRow.user_id == uid))
            result = await s.execute(delete(DirectoryUserRow).where(DirectoryUserRow.id == uid))
            return bool(getattr(result, "rowcount", 0))

    async def _group(self, s: Any, row: DirectoryGroupRow) -> Group:
        members = await s.scalars(
            select(DirectoryMemberRow.user_id)
            .where(DirectoryMemberRow.group_id == row.id)
            .order_by(DirectoryMemberRow.user_id)
        )
        return Group(
            id=str(row.id),
            display_name=row.display_name,
            external_id=row.external_id,
            members=tuple(str(m) for m in members),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def groups_of(self, user_id: str) -> list[Group]:
        uid = _uuid(user_id)
        if uid is None:
            return []
        async with self.sessions() as s:
            rows = await s.scalars(
                select(DirectoryGroupRow)
                .join(DirectoryMemberRow, DirectoryMemberRow.group_id == DirectoryGroupRow.id)
                .where(DirectoryMemberRow.user_id == uid)
                .order_by(DirectoryGroupRow.display_name)
            )
            return [Group(str(r.id), r.display_name, r.external_id, (user_id,)) for r in rows]

    async def _set_members(self, s: Any, gid: uuid.UUID, ids: list[str]) -> None:
        """Add the members that exist and aren't in the group yet (unknown ids are skipped)."""
        wanted = {u for u in (_uuid(i) for i in ids) if u is not None}
        if not wanted:
            return
        known = set(
            await s.scalars(select(DirectoryUserRow.id).where(DirectoryUserRow.id.in_(wanted)))
        )
        already = set(
            await s.scalars(
                select(DirectoryMemberRow.user_id).where(DirectoryMemberRow.group_id == gid)
            )
        )
        s.add_all(DirectoryMemberRow(group_id=gid, user_id=u) for u in known - already)

    async def create_group(self, group: Group) -> Group:
        gid = uuid.UUID(group.id)
        try:
            async with self.sessions() as s, s.begin():
                s.add(
                    DirectoryGroupRow(
                        id=gid,
                        display_name=group.display_name,
                        external_id=group.external_id,
                        created_at=group.created_at,
                        updated_at=group.updated_at,
                    )
                )
                await s.flush()
                await self._set_members(s, gid, list(group.members))
        except IntegrityError as exc:
            raise Conflict("displayName") from exc
        created = await self.get_group(group.id)
        assert created is not None
        return created

    async def get_group(self, group_id: str) -> Group | None:
        gid = _uuid(group_id)
        if gid is None:
            return None
        async with self.sessions() as s:
            row = await s.get(DirectoryGroupRow, gid)
            return await self._group(s, row) if row else None

    async def list_groups(
        self, display_name: str | None, start: int, count: int
    ) -> tuple[int, list[Group]]:
        query = select(DirectoryGroupRow)
        total = select(func.count()).select_from(DirectoryGroupRow)
        if display_name is not None:
            condition = DirectoryGroupRow.display_name == display_name
            query, total = query.where(condition), total.where(condition)
        query = (
            query.order_by(DirectoryGroupRow.display_name).offset(max(start, 1) - 1).limit(count)
        )
        async with self.sessions() as s:
            n = int(await s.scalar(total) or 0)
            return n, [await self._group(s, r) for r in list(await s.scalars(query))]

    async def update_group(
        self,
        group_id: str,
        display_name: str | None = None,
        external_id: str | None = None,
        members: list[str] | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> Group | None:
        gid = _uuid(group_id)
        if gid is None:
            return None
        values: dict[str, Any] = {"updated_at": _now()}
        if display_name:
            values["display_name"] = display_name
        if external_id is not None:
            values["external_id"] = external_id
        try:
            async with self.sessions() as s, s.begin():
                result = await s.execute(
                    update(DirectoryGroupRow).where(DirectoryGroupRow.id == gid).values(**values)
                )
                if not getattr(result, "rowcount", 0):
                    return None
                if members is not None:
                    await s.execute(
                        delete(DirectoryMemberRow).where(DirectoryMemberRow.group_id == gid)
                    )
                    await s.flush()
                    await self._set_members(s, gid, members)
                if add:
                    await s.flush()
                    await self._set_members(s, gid, add)
                gone = [u for u in (_uuid(i) for i in remove or []) if u is not None]
                if gone:
                    await s.flush()
                    await s.execute(
                        delete(DirectoryMemberRow).where(
                            DirectoryMemberRow.group_id == gid, DirectoryMemberRow.user_id.in_(gone)
                        )
                    )
        except IntegrityError as exc:
            raise Conflict("displayName") from exc
        return await self.get_group(group_id)

    async def delete_group(self, group_id: str) -> bool:
        gid = _uuid(group_id)
        if gid is None:
            return False
        async with self.sessions() as s, s.begin():
            await s.execute(delete(DirectoryMemberRow).where(DirectoryMemberRow.group_id == gid))
            result = await s.execute(delete(DirectoryGroupRow).where(DirectoryGroupRow.id == gid))
            return bool(getattr(result, "rowcount", 0))


@dataclass(frozen=True)
class Identity:
    """What authentication needs about a user: who they are and their groups, right now."""

    user: User
    groups: tuple[str, ...]  # group display names, sorted


class CachedIdentities:
    """Per-process cache in front of the directory, so a request doesn't cost two queries.

    A SCIM write clears it at once on every replica (the same announcement API keys use),
    and `ttl` bounds how stale an entry can be if that announcement is lost. Misses are
    not cached: a user provisioned a second ago must work.
    """

    def __init__(
        self,
        directory: Directory,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 10_000,
    ) -> None:
        self.directory, self.ttl, self._now, self._max = directory, ttl, clock, max_entries
        self._hits: dict[tuple[str, str], tuple[Identity, float]] = {}

    async def lookup(self, attr: UserAttr, value: str) -> Identity | None:
        now = self._now()
        hit = self._hits.get((attr, value))
        if hit is not None and now - hit[1] < self.ttl:
            return hit[0]
        user = await self.directory.find_user(attr, value)
        if user is None:
            self._hits.pop((attr, value), None)
            return None
        groups = tuple(sorted(g.display_name for g in await self.directory.groups_of(user.id)))
        identity = Identity(user, groups)
        if len(self._hits) >= self._max:
            self._hits.clear()  # rare, and cheaper than tracking recency
        self._hits[(attr, value)] = (identity, now)
        return identity

    def invalidate(self) -> None:
        self._hits.clear()
