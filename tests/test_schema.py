"""Bringing an older database up to the current models.

`create_all` never alters a table that already exists, so every field added to a model after the
first run is a column some database is missing. These tests describe what happens then.
"""

from enum import StrEnum

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

import polgate.models  # noqa: F401  (populates SQLModel.metadata)
from polgate.schema import drop_orphan_columns, main, orphan_columns, reconcile


class _Mode(StrEnum):
    """An enum whose value and name differ, as polgate's own do."""

    AUTO = "auto"


@pytest.fixture
def engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'schema.db'}")


def _execute(engine, *statements: str) -> None:
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _columns(engine, table: str) -> list[str]:
    return [column["name"] for column in inspect(engine).get_columns(table)]


def test_a_database_built_from_the_models_needs_nothing(engine):
    SQLModel.metadata.create_all(engine)
    assert reconcile(engine).is_clean


def test_a_missing_column_is_added_and_existing_rows_get_the_default(engine):
    """The case that breaks a running hub: a field added to a model after the first start."""
    _execute(
        engine,
        "CREATE TABLE pendingturn ("
        "id INTEGER NOT NULL PRIMARY KEY, conversation_id INTEGER NOT NULL,"
        "user_id INTEGER NOT NULL, text_enc VARCHAR NOT NULL, created_at DATETIME NOT NULL)",
        "INSERT INTO pendingturn VALUES (1, 1, 1, 'szyfr', '2026-09-01 10:00:00')",
    )

    report = reconcile(engine)

    assert report.added == ["pendingturn.extra_terms_enc"]
    assert "extra_terms_enc" in _columns(engine, "pendingturn")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT extra_terms_enc FROM pendingturn")).scalar() == ""


def test_a_second_pass_changes_nothing(engine):
    _execute(
        engine,
        "CREATE TABLE pendingturn ("
        "id INTEGER NOT NULL PRIMARY KEY, conversation_id INTEGER NOT NULL,"
        "user_id INTEGER NOT NULL, text_enc VARCHAR NOT NULL, created_at DATETIME NOT NULL)",
    )
    assert reconcile(engine).added == ["pendingturn.extra_terms_enc"]
    assert reconcile(engine).added == []


def test_a_column_that_cannot_be_added_is_reported_rather_than_guessed(engine):
    """`content_original_enc` is NOT NULL with no default: there is no value to backfill with."""
    _execute(
        engine,
        "CREATE TABLE message ("
        "id INTEGER NOT NULL PRIMARY KEY, conversation_id INTEGER NOT NULL,"
        "role VARCHAR NOT NULL, content_sent VARCHAR NOT NULL, entity_count INTEGER NOT NULL,"
        "judge_hits INTEGER NOT NULL, created_at DATETIME NOT NULL)",
    )

    report = reconcile(engine)

    assert report.blocked == [
        "message.content_original_enc: NOT NULL with no default; "
        "give the field a default or migrate by hand"
    ]
    assert "content_original_enc" not in _columns(engine, "message")
    assert report.needs_attention


def test_a_leftover_nullable_column_is_noted_and_left_alone(engine):
    SQLModel.metadata.create_all(engine)
    _execute(engine, "ALTER TABLE conversation ADD COLUMN stara_notatka VARCHAR")

    report = reconcile(engine)

    assert report.unknown == ["conversation.stara_notatka"]
    assert report.conflicts == []
    assert not report.needs_attention
    assert "stara_notatka" in _columns(engine, "conversation")


def test_a_leftover_not_null_column_is_a_conflict_because_it_blocks_every_insert(engine):
    """This is what a renamed column leaves behind, and it is not harmless."""
    SQLModel.metadata.create_all(engine)
    _execute(
        engine, "ALTER TABLE conversation ADD COLUMN stara_kolumna VARCHAR NOT NULL DEFAULT ''"
    )
    # Re-create without the default, the way an older model would have written it.
    _execute(
        engine,
        "DROP TABLE conversation",
        "CREATE TABLE conversation ("
        "id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL, title VARCHAR NOT NULL,"
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,"
        "stara_kolumna VARCHAR NOT NULL)",
    )

    report = reconcile(engine)

    assert report.conflicts == ["conversation.stara_kolumna"]
    assert report.needs_attention


def test_dropping_the_leftovers_makes_the_table_usable_again(engine):
    _execute(
        engine,
        "CREATE TABLE conversation ("
        "id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL, title VARCHAR NOT NULL,"
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,"
        "stara_kolumna VARCHAR NOT NULL)",
    )
    assert orphan_columns(engine) == {"conversation": ["stara_kolumna"]}

    assert drop_orphan_columns(engine) == ["conversation.stara_kolumna"]

    assert orphan_columns(engine) == {}
    assert reconcile(engine).is_clean


def test_a_table_the_database_does_not_have_yet_is_left_to_create_all(engine):
    """reconcile only touches tables that exist; create_all owns the rest."""
    assert reconcile(engine).is_clean
    assert inspect(engine).get_table_names() == []


class TestCommandLine:
    """`python -m polgate.schema`, which is what run.sh calls and what the error message names."""

    @pytest.fixture(autouse=True)
    def _engine(self, engine, monkeypatch):
        import polgate.db as db

        monkeypatch.setattr(db, "_engine", engine)
        self.engine = engine

    def test_a_matching_database_reports_success(self, capsys):
        SQLModel.metadata.create_all(self.engine)
        assert main([]) == 0
        assert "Schema matches the models" in capsys.readouterr().out

    def test_a_conflict_is_reported_with_the_command_that_fixes_it(self, capsys):
        SQLModel.metadata.create_all(self.engine)
        _execute(
            self.engine,
            "DROP TABLE conversation",
            "CREATE TABLE conversation ("
            "id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL, title VARCHAR NOT NULL,"
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,"
            "stara_kolumna VARCHAR NOT NULL)",
        )

        assert main([]) == 1

        output = capsys.readouterr().out
        assert "blocking inserts: conversation.stara_kolumna" in output
        assert "--drop-orphan-columns" in output

    def test_dropping_says_what_it_discards(self, capsys):
        SQLModel.metadata.create_all(self.engine)
        _execute(
            self.engine,
            "ALTER TABLE conversation ADD COLUMN stara_kolumna VARCHAR",
            "INSERT INTO conversation VALUES "
            "(1, 1, 'x', '2026-09-01 10:00:00', '2026-09-01 10:00:00', 'cos')",
        )

        assert main(["--drop-orphan-columns"]) == 0

        output = capsys.readouterr().out
        assert "discarding 1 non-empty values" in output
        assert "stara_kolumna" not in _columns(self.engine, "conversation")

    def test_dropping_nothing_says_so(self, capsys):
        SQLModel.metadata.create_all(self.engine)
        assert main(["--drop-orphan-columns"]) == 0
        assert "Nothing to drop" in capsys.readouterr().out


class TestWhatCanBeAdded:
    """The two helpers that decide whether an ALTER TABLE is safe, and what to backfill with."""

    @staticmethod
    def _column(**kwargs):
        from sqlalchemy import Column, String

        return Column("nowa", String, **kwargs)

    def test_a_primary_key_column_is_refused(self):
        from polgate.schema import _cannot_add

        assert _cannot_add(self._column(primary_key=True)) == "part of the primary key"

    def test_a_unique_column_is_refused(self):
        from polgate.schema import _cannot_add

        assert _cannot_add(self._column(unique=True)) == "carries a unique constraint"

    def test_a_nullable_column_needs_no_default(self):
        from polgate.schema import _cannot_add

        assert _cannot_add(self._column(nullable=True)) is None

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"default": ""}, "''"),
            ({"default": "Kowalski's"}, "'Kowalski''s'"),
            ({"default": 7}, "7"),
            ({"default": 1.5}, "1.5"),
            ({"default": True}, "1"),
            ({"default": False}, "0"),
            ({"server_default": "x"}, "x"),
            ({}, None),
            ({"default": None}, None),
            ({"default": _Mode.AUTO}, "'AUTO'"),
        ],
    )
    def test_defaults_are_rendered_as_sql_literals(self, kwargs, expected):
        from polgate.schema import _literal_default

        assert _literal_default(self._column(**kwargs)) == expected

    def test_a_callable_default_cannot_backfill_existing_rows(self):
        """utcnow() would stamp every old row with the moment of the migration, which is a lie."""
        from datetime import UTC, datetime

        from polgate.schema import _literal_default

        assert _literal_default(self._column(default=lambda: datetime.now(UTC))) is None


def test_a_nullable_column_with_a_default_keeps_it(engine):
    """The DEFAULT clause is worth writing even when NULL would be allowed."""
    from sqlalchemy import Column, String

    from polgate.schema import _add_column

    _execute(engine, "CREATE TABLE proba (id INTEGER NOT NULL PRIMARY KEY)")
    _add_column(engine, "proba", Column("notatka", String, nullable=True, default="brak"))

    with engine.begin() as connection:
        connection.execute(text("INSERT INTO proba (id) VALUES (1)"))
        assert connection.execute(text("SELECT notatka FROM proba")).scalar() == "brak"


def test_an_indexed_leftover_column_is_dropped_with_its_index(engine):
    """SQLite refuses DROP COLUMN on an indexed column, the very leftover a rename leaves."""
    _execute(
        engine,
        "CREATE TABLE conversation ("
        "id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL, title VARCHAR NOT NULL,"
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,"
        "stary_wlasciciel INTEGER NOT NULL)",
        "CREATE INDEX ix_conversation_stary_wlasciciel ON conversation (stary_wlasciciel)",
    )

    assert drop_orphan_columns(engine) == ["conversation.stary_wlasciciel"]

    assert orphan_columns(engine) == {}
    assert inspect(engine).get_indexes("conversation") == []


def test_a_backfilled_enum_column_reads_back(engine):
    """The value went in where SQLModel reads names, and every later query of the table failed."""
    from sqlmodel import Session, select

    from polgate.models import HilMode, Policy

    SQLModel.metadata.create_all(engine)
    _execute(engine, "ALTER TABLE policy DROP COLUMN hil_chat")
    _execute(
        engine,
        "INSERT INTO policy (id, hil_files, judge_enabled, judge_fallback, vault_ttl_days,"
        " enabled_entities, entity_types_known, denylist, allowlist, updated_at,"
        " well_known_names_known, attachment_retrieval, retrieval_top_k)"
        " VALUES (1, 'REVIEW', 1, 'SEND_STAGE1', 30, 'PESEL', '', '', '',"
        " '2026-11-13 00:00:00', '', 1, 6)",
    )

    assert reconcile(engine).added == ["policy.hil_chat"]

    with Session(engine) as session:
        assert session.exec(select(Policy)).one().hil_chat is HilMode.AUTO
