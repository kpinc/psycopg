"""
Information about PostgreSQL types

These types allow to read information from the system catalog and provide
information to the adapters if needed.
"""

# Copyright (C) 2020 The Psycopg Team

from typing import Any, Dict, Iterator, Optional, overload
from typing import Sequence, Tuple, Type, TypeVar, Union, TYPE_CHECKING
from typing_extensions import TypeAlias

from . import sql
from . import errors as e
from .abc import AdaptContext, Query
from .rows import dict_row

if TYPE_CHECKING:
    from .connection import BaseConnection, Connection
    from .connection_async import AsyncConnection

T = TypeVar("T", bound="TypeInfo")
RegistryKey: TypeAlias = Union[str, int, Tuple[type, int]]


class TypeInfo:
    """
    Hold information about a PostgreSQL base type.
    """

    __module__ = "psycopg.types"

    def __init__(
        self,
        name: str,
        oid: int,
        array_oid: int,
        *,
        regtype: str = "",
        delimiter: str = ",",
    ):
        self.name = name
        self.oid = oid
        self.array_oid = array_oid
        self.regtype = regtype or name
        self.delimiter = delimiter

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__qualname__}:"
            f" {self.name} (oid: {self.oid}, array oid: {self.array_oid})>"
        )

    @overload
    @classmethod
    def fetch(
        cls: Type[T], conn: "Connection[Any]", name: Union[str, sql.Identifier]
    ) -> Optional[T]:
        ...

    @overload
    @classmethod
    async def fetch(
        cls: Type[T], conn: "AsyncConnection[Any]", name: Union[str, sql.Identifier]
    ) -> Optional[T]:
        ...

    @classmethod
    def fetch(
        cls: Type[T], conn: "BaseConnection[Any]", name: Union[str, sql.Identifier]
    ) -> Any:
        """Query a system catalog to read information about a type."""
        from .connection import Connection
        from .connection_async import AsyncConnection

        if isinstance(name, sql.Composable):
            name = name.as_string(conn)

        if isinstance(conn, Connection):
            return cls._fetch(conn, name)
        elif isinstance(conn, AsyncConnection):
            return cls._fetch_async(conn, name)
        else:
            raise TypeError(
                f"expected Connection or AsyncConnection, got {type(conn).__name__}"
            )

    @classmethod
    def _fetch(cls: Type[T], conn: "Connection[Any]", name: str) -> Optional[T]:
        # This might result in a nested transaction. What we want is to leave
        # the function with the connection in the state we found (either idle
        # or intrans)
        try:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(cls._get_info_query(conn), {"name": name})
                    recs = cur.fetchall()
        except e.UndefinedObject:
            return None

        return cls._from_records(name, recs)

    @classmethod
    async def _fetch_async(
        cls: Type[T], conn: "AsyncConnection[Any]", name: str
    ) -> Optional[T]:
        try:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(cls._get_info_query(conn), {"name": name})
                    recs = await cur.fetchall()
        except e.UndefinedObject:
            return None

        return cls._from_records(name, recs)

    @classmethod
    def _from_records(
        cls: Type[T], name: str, recs: Sequence[Dict[str, Any]]
    ) -> Optional[T]:
        if len(recs) == 1:
            return cls(**recs[0])
        elif not recs:
            return None
        else:
            raise e.ProgrammingError(f"found {len(recs)} different types named {name}")

    def register(self, context: Optional[AdaptContext] = None) -> None:
        """
        Register the type information, globally or in the specified `!context`.
        """
        if context:
            types = context.adapters.types
        else:
            from . import postgres

            types = postgres.types

        types.add(self)

        if self.array_oid:
            from .types.array import register_array

            register_array(self, context)

    @classmethod
    def _get_info_query(cls, conn: "BaseConnection[Any]") -> Query:
        return sql.SQL(
            """\
SELECT
    typname AS name, oid, typarray AS array_oid,
    oid::regtype::text AS regtype, typdelim AS delimiter
FROM pg_type t
WHERE t.oid = {regtype}
ORDER BY t.oid
"""
        ).format(regtype=cls._to_regtype(conn))

    @classmethod
    def _has_to_regtype_function(cls, conn: "BaseConnection[Any]") -> bool:
        # to_regtype() introduced in PostgreSQL 9.4 and CockroachDB 22.2
        info = conn.info
        if info.vendor == "PostgreSQL":
            return info.server_version >= 90400
        elif info.vendor == "CockroachDB":
            return info.server_version >= 220200
        else:
            return False

    @classmethod
    def _to_regtype(cls, conn: "BaseConnection[Any]") -> sql.SQL:
        # `to_regtype()` returns the type oid or NULL, unlike the :: operator,
        # which returns the type or raises an exception, which requires
        # a transaction rollback and leaves traces in the server logs.

        if cls._has_to_regtype_function(conn):
            return sql.SQL("to_regtype(%(name)s)")
        else:
            return sql.SQL("%(name)s::regtype")

    def _added(self, registry: "TypesRegistry") -> None:
        """Method called by the `!registry` when the object is added there."""
        pass


class TypesRegistry:
    """
    Container for the information about types in a database.
    """

    __module__ = "psycopg.types"

    def __init__(self, template: Optional["TypesRegistry"] = None):
        self._registry: Dict[RegistryKey, TypeInfo]

        # Make a shallow copy: it will become a proper copy if the registry
        # is edited.
        if template:
            self._registry = template._registry
            self._own_state = False
            template._own_state = False
        else:
            self.clear()

    def clear(self) -> None:
        self._registry = {}
        self._own_state = True

    def add(self, info: TypeInfo) -> None:
        self._ensure_own_state()
        if info.oid:
            self._registry[info.oid] = info
        if info.array_oid:
            self._registry[info.array_oid] = info
        self._registry[info.name] = info

        if info.regtype and info.regtype not in self._registry:
            self._registry[info.regtype] = info

        # Allow info to customise further their relation with the registry
        info._added(self)

    def __iter__(self) -> Iterator[TypeInfo]:
        seen = set()
        for t in self._registry.values():
            if id(t) not in seen:
                seen.add(id(t))
                yield t

    @overload
    def __getitem__(self, key: Union[str, int]) -> TypeInfo:
        ...

    @overload
    def __getitem__(self, key: Tuple[Type[T], int]) -> T:
        ...

    def __getitem__(self, key: RegistryKey) -> TypeInfo:
        """
        Return info about a type, specified by name or oid

        :param key: the name or oid of the type to look for.

        Raise KeyError if not found.
        """
        if isinstance(key, str):
            if key.endswith("[]"):
                key = key[:-2]
        elif not isinstance(key, (int, tuple)):
            raise TypeError(f"the key must be an oid or a name, got {type(key)}")
        try:
            return self._registry[key]
        except KeyError:
            raise KeyError(f"couldn't find the type {key!r} in the types registry")

    @overload
    def get(self, key: Union[str, int]) -> Optional[TypeInfo]:
        ...

    @overload
    def get(self, key: Tuple[Type[T], int]) -> Optional[T]:
        ...

    def get(self, key: RegistryKey) -> Optional[TypeInfo]:
        """
        Return info about a type, specified by name or oid

        :param key: the name or oid of the type to look for.

        Unlike `__getitem__`, return None if not found.
        """
        try:
            return self[key]
        except KeyError:
            return None

    def get_oid(self, name: str) -> int:
        """
        Return the oid of a PostgreSQL type by name.

        :param key: the name of the type to look for.

        Return the array oid if the type ends with "``[]``"

        Raise KeyError if the name is unknown.
        """
        t = self[name]
        if name.endswith("[]"):
            return t.array_oid
        else:
            return t.oid

    def get_by_subtype(self, cls: Type[T], subtype: Union[int, str]) -> Optional[T]:
        """
        Return info about a `TypeInfo` subclass by its element name or oid.

        :param cls: the subtype of `!TypeInfo` to look for. Currently
            supported are `~psycopg.types.range.RangeInfo` and
            `~psycopg.types.multirange.MultirangeInfo`.
        :param subtype: The name or OID of the subtype of the element to look for.
        :return: The `!TypeInfo` object of class `!cls` whose subtype is
            `!subtype`. `!None` if the element or its range are not found.
        """
        try:
            info = self[subtype]
        except KeyError:
            return None
        return self.get((cls, info.oid))

    def _ensure_own_state(self) -> None:
        # Time to write! so, copy.
        if not self._own_state:
            self._registry = self._registry.copy()
            self._own_state = True
