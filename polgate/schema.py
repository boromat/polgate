"""Bring an existing database up to what the models declare.

``SQLModel.metadata.create_all`` creates tables that are missing and never touches one that already
exists. That is fine until a model grows a field: every database created before that day is one
column short, and the first query naming it fails with ``no such column`` — at startup, in the
retention job, or on somebody's next message.

This module closes exactly that gap and nothing wider. It **adds** columns the models declare and
the database lacks. It never drops, renames or retypes anything on its own: those need a decision a
program should not make against the only copy of somebody's data, so they are reported instead.

One leftover deserves the loud treatment. A column that no model declares is usually harmless, but
if it is ``NOT NULL`` with no default then every insert into that table fails — which is what a
renamed column leaves behind. Those are reported as conflicts with the command that fixes them:

    python -m polgate.schema                       # what the database and the models disagree about
    python -m polgate.schema --drop-orphan-columns # remove the leftovers, after saying what is lost

A real migration tool belongs here once the schema starts changing in ways this cannot express;
until then it would be a dependency and a workflow for a problem this file solves at startup.
"""

from __future__ import annotations

import argparse
import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import Column, Engine, inspect, text
from sqlalchemy.sql.schema import ScalarElementColumnDefault
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchemaReport:
    """What one reconciliation pass changed, refused and noticed."""

    #: Columns added, as ``table.column``.
    added: list[str] = field(default_factory=list)
    #: Columns the models declare that could not be added automatically, with the reason.
    blocked: list[str] = field(default_factory=list)
    #: Columns the database still has and no model declares. Harmless; never dropped here.
    unknown: list[str] = field(default_factory=list)
    #: Orphan columns that are ``NOT NULL`` with no default, so every insert into that table fails.
    conflicts: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when the database already matched the models."""
        return not (self.added or self.blocked or self.unknown or self.conflicts)

    @property
    def needs_attention(self) -> bool:
        """True when something is wrong that this pass could not put right."""
        return bool(self.blocked or self.conflicts)


def reconcile(engine: Engine, *, log: bool = True) -> SchemaReport:
    """Add missing columns to existing tables and report what could not be handled.

    Tables that do not exist at all are left to ``create_all``, which runs first.

    Args:
        engine: the database to inspect and alter.
        log: write the findings to the log. The command line turns this off because it prints
            the same report itself.

    Returns:
        A :class:`SchemaReport`. Callers decide how loudly to complain about `blocked`.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    report = SchemaReport()

    for name, table in SQLModel.metadata.tables.items():
        if name not in existing_tables:
            continue
        actual = {column["name"]: column for column in inspector.get_columns(name)}
        declared = {column.name for column in table.columns}
        for orphan in sorted(set(actual) - declared):
            target = report.conflicts if _breaks_inserts(actual[orphan]) else report.unknown
            target.append(f"{name}.{orphan}")
        present = set(actual)

        for column in table.columns:
            if column.name in present:
                continue
            reason = _cannot_add(column)
            if reason is not None:
                report.blocked.append(f"{name}.{column.name}: {reason}")
                continue
            _add_column(engine, name, column)
            report.added.append(f"{name}.{column.name}")

    if log:
        _log(report)
    return report


def _breaks_inserts(reflected: Mapping[str, object]) -> bool:
    """Return whether a leftover column will reject every insert that ignores it."""
    return not reflected.get("nullable", True) and reflected.get("default") is None


def orphan_columns(engine: Engine) -> dict[str, list[str]]:
    """Return ``table -> columns`` for every column the database has and no model declares."""
    inspector = inspect(engine)
    orphans: dict[str, list[str]] = {}
    for name, table in SQLModel.metadata.tables.items():
        if name not in set(inspector.get_table_names()):
            continue
        declared = {column.name for column in table.columns}
        found = sorted(
            column["name"]
            for column in inspector.get_columns(name)
            if column["name"] not in declared
        )
        if found:
            orphans[name] = found
    return orphans


def drop_orphan_columns(engine: Engine) -> list[str]:
    """Drop every column no model declares, and return what was dropped as ``table.column``.

    Destructive on purpose and never called at startup: it exists so an operator who has read the
    report can act on it in one step instead of writing SQL by hand.
    """
    quote = engine.dialect.identifier_preparer.quote
    dropped: list[str] = []
    for table, columns in orphan_columns(engine).items():
        indexes = inspect(engine).get_indexes(table)
        for column in columns:
            # SQLite refuses to drop an indexed column, and a renamed `conversation_id` with
            # `index=True` is exactly the leftover this command exists for. Its indexes go first.
            # Identifiers come from the database's own catalogue and go through the dialect quoter.
            statements = [
                f"DROP INDEX {quote(index['name'])}"
                for index in indexes
                if index["name"] and column in index["column_names"]
            ]
            statements.append(f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)}")
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))
            logger.warning("schema: dropped orphan column %s.%s", table, column)
            dropped.append(f"{table}.{column}")
    return dropped


def _cannot_add(column: Column[object]) -> str | None:
    """Return why a column cannot be added by an ``ALTER TABLE``, or None when it can."""
    if column.primary_key:
        return "part of the primary key"
    if column.unique:
        return "carries a unique constraint"
    if not column.nullable and _literal_default(column) is None:
        return "NOT NULL with no default; give the field a default or migrate by hand"
    return None


def _literal_default(column: Column[object]) -> str | None:
    """Return the column's default rendered as SQL, or None when there is nothing usable."""
    if column.server_default is not None:
        return str(getattr(column.server_default, "arg", ""))
    default = column.default
    if not isinstance(default, ScalarElementColumnDefault):
        return None
    value = default.arg
    if callable(value) or value is None:
        return None
    if isinstance(value, enum.Enum):
        # SQLModel stores an enum by its name ("AUTO"), and reading back a backfilled value such as
        # "auto" raised LookupError on every query of the table.
        return "'" + value.name.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _add_column(engine: Engine, table: str, column: Column[object]) -> None:
    """Run the ``ALTER TABLE ... ADD COLUMN`` for one column."""
    quote = engine.dialect.identifier_preparer.quote
    clause = f"{quote(column.name)} {column.type.compile(engine.dialect)}"
    default = _literal_default(column)
    if not column.nullable:
        clause += f" NOT NULL DEFAULT {default}"
    elif default is not None:
        clause += f" DEFAULT {default}"

    # Every identifier and literal here comes from our own model metadata, never from a request,
    # and identifiers go through the dialect's quoter. There is no bind-parameter form of DDL.
    statement = f"ALTER TABLE {quote(table)} ADD COLUMN {clause}"
    with engine.begin() as connection:
        connection.execute(text(statement))


def _log(report: SchemaReport) -> None:
    """Say what happened, at a level that matches how surprising it is."""
    for column in report.added:
        logger.warning("schema: added missing column %s", column)
    for column in report.blocked:
        logger.error("schema: cannot add %s", column)
    for column in report.unknown:
        logger.info("schema: %s is in the database and in no model; left alone", column)
    for column in report.conflicts:
        logger.error(
            "schema: %s is NOT NULL, in no model, and will reject every insert into that table. "
            "Run: python -m polgate.schema --drop-orphan-columns",
            column,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Report what the database and the models disagree about, and optionally fix the leftovers.

    Returns:
        0 when the database is usable, 1 when something still needs a human.
    """
    parser = argparse.ArgumentParser(prog="polgate.schema", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--drop-orphan-columns",
        action="store_true",
        help="drop columns no model declares. Destructive: their contents are lost.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    from polgate import models  # noqa: F401
    from polgate.db import get_engine

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    report = reconcile(engine, log=False)

    if args.drop_orphan_columns:
        orphans = orphan_columns(engine)
        if not orphans:
            print("Nothing to drop: every column in the database is declared by a model.")
            return 0
        for table, columns in orphans.items():
            for column in columns:
                held = _value_count(engine, table, column)
                print(f"dropping {table}.{column}, discarding {held} non-empty values")
        drop_orphan_columns(engine)
        print("Done. Re-run without the flag to confirm.")
        return 0

    if report.is_clean:
        print("Schema matches the models.")
        return 0
    for label, entries in (
        ("added", report.added),
        ("cannot add", report.blocked),
        ("in the database only", report.unknown),
        ("blocking inserts", report.conflicts),
    ):
        for entry in entries:
            print(f"{label}: {entry}")
    if report.conflicts:
        print("\nFix with: python -m polgate.schema --drop-orphan-columns")
    return 1 if report.needs_attention else 0


def _value_count(engine: Engine, table: str, column: str) -> int:
    """Count the rows where a column holds something, so a drop states what it discards."""
    quote = engine.dialect.identifier_preparer.quote
    # Identifiers come from the database catalogue, quoted by the dialect; nothing here is a literal
    # from a request, and an identifier cannot be a bind parameter.
    statement = f"SELECT count(*) FROM {quote(table)} WHERE {quote(column)} != ''"  # noqa: S608
    with engine.begin() as connection:
        return int(connection.execute(text(statement)).scalar_one())


if __name__ == "__main__":
    raise SystemExit(main())
