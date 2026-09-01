from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import AbstractContextManager, closing
from pathlib import Path
from unittest.mock import patch

from server.access_control import AccessControlError, AccessControlStore
from server.access_control_storage import (
    PostgreSQLAccessControlStore,
    _connect_postgres,
    _postgres_sql,
)


class _Transaction(AbstractContextManager[None]):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> None:
        self.connection.execute("BEGIN")
        return None

    def __exit__(self, exception_type, _exception, _traceback) -> bool:
        if exception_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        return False


class _SQLitePsycopgConnection:
    """Test double that preserves psycopg's transaction and placeholder API."""

    def __init__(self, path: Path, statements: list[tuple[str, tuple[object, ...]]]) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.statements = statements

    def transaction(self) -> _Transaction:
        return _Transaction(self.connection)

    def execute(self, statement: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        self.statements.append((statement, params))
        if statement.startswith("SELECT pg_advisory_xact_lock("):
            return self.connection.execute("SELECT NULL AS pg_advisory_xact_lock")
        sqlite_statement = statement.replace("%s", "?").replace("BYTEA", "BLOB")
        return self.connection.execute(sqlite_statement, params)

    def close(self) -> None:
        self.connection.close()


class PostgreSQLAccessControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="semarail-postgres-store-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "postgres-compatible.sqlite3"
        self.sqlite_fallback = Path(self.temp.name) / "must-not-exist.sqlite3"
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def factory(self, _database_url: str) -> _SQLitePsycopgConnection:
        return _SQLitePsycopgConnection(self.database, self.statements)

    def store(self) -> PostgreSQLAccessControlStore:
        return PostgreSQLAccessControlStore(
            "postgresql://control.example/semarail",
            bootstrap_token="bootstrap-token-that-is-at-least-thirty-two-characters",
            connection_factory=self.factory,
        )

    def test_database_url_requires_an_explicit_postgresql_uri(self) -> None:
        for invalid in (
            "",
            "mysql://control.example/semarail",
            "postgresql:semarail",
            "postgresql://",
            "postgresql://[invalid/semarail",
            "postgresql://control.example/semarail#unsafe-fragment",
        ):
            with self.subTest(database_url=invalid), self.assertRaises(AccessControlError) as caught:
                PostgreSQLAccessControlStore(invalid, connection_factory=self.factory)
            self.assertEqual((caught.exception.code, caught.exception.status), ("INVALID_REQUEST", 400))

    def test_configured_postgres_failure_is_safe_and_never_falls_back_to_sqlite(self) -> None:
        def unavailable(_database_url: str) -> object:
            raise RuntimeError("password=should-never-be-exposed")

        with patch(
            "server.access_control_storage._connect_postgres", unavailable
        ), self.assertRaises(AccessControlError) as caught:
            AccessControlStore.from_config(
                self.sqlite_fallback,
                database_url="postgresql://admin:secret@control.example/semarail",
            )

        self.assertEqual((caught.exception.code, caught.exception.status), ("STORE_UNAVAILABLE", 503))
        self.assertNotIn("password", caught.exception.safe_message)
        self.assertNotIn("secret", caught.exception.safe_message)
        self.assertFalse(self.sqlite_fallback.exists())

    def test_missing_driver_and_connection_error_have_stable_safe_errors(self) -> None:
        with patch.dict(sys.modules, {"psycopg": None, "psycopg.rows": None}):
            with self.assertRaises(AccessControlError) as missing:
                _connect_postgres("postgresql://admin:secret@control.example/semarail")
        self.assertEqual((missing.exception.code, missing.exception.status), ("STORE_UNAVAILABLE", 503))
        self.assertNotIn("secret", missing.exception.safe_message)

        class _Driver:
            @staticmethod
            def connect(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError("server echoed password=secret")

        class _Rows:
            dict_row = object()

        with patch.dict(sys.modules, {"psycopg": _Driver, "psycopg.rows": _Rows}):
            with self.assertRaises(AccessControlError) as unavailable:
                _connect_postgres("postgresql://admin:secret@control.example/semarail")
        self.assertEqual((unavailable.exception.code, unavailable.exception.status), ("STORE_UNAVAILABLE", 503))
        self.assertNotIn("secret", unavailable.exception.safe_message)

    def test_migrations_are_versioned_idempotent_and_serialized(self) -> None:
        self.store()
        self.store()

        with closing(sqlite3.connect(self.database)) as connection:
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM access_control_schema_migrations ORDER BY version"
            )]
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertEqual(versions, [1, 2])
        self.assertTrue({"organizations", "subjects", "credentials", "policies", "audit_events"} <= tables)
        self.assertEqual(
            sum("CREATE TABLE IF NOT EXISTS organizations" in statement for statement, _ in self.statements),
            1,
        )
        advisory_calls = [params for statement, params in self.statements if "pg_advisory_xact_lock" in statement]
        self.assertEqual(len(advisory_calls), 2)
        self.assertTrue(all(len(params) == 1 and type(params[0]) is int for params in advisory_calls))

    def test_unknown_or_gapped_schema_versions_fail_closed(self) -> None:
        for versions in ((2,), (1, 2, 99)):
            with self.subTest(versions=versions):
                if self.database.exists():
                    self.database.unlink()
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute(
                        "CREATE TABLE access_control_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                    )
                    connection.executemany(
                        "INSERT INTO access_control_schema_migrations(version,applied_at) VALUES(?, 'now')",
                        [(version,) for version in versions],
                    )
                    connection.commit()
                with self.assertRaises(AccessControlError) as caught:
                    self.store()
                self.assertEqual(
                    (caught.exception.code, caught.exception.status),
                    ("STORE_SCHEMA_INCOMPATIBLE", 503),
                )

    def test_inherited_crud_authentication_policy_and_audit_behave_the_same(self) -> None:
        store = self.store()
        account = store.create_service_account(
            "Region A Agent", attributes={"regionCodes": ["CN-JIA"]}
        )
        issued = store.issue_api_key(account.id, label="codex")
        auth = store.authenticate(f"Bearer {issued['apiKey']}")
        policy = store.create_policy(
            "Sales reader",
            {"schemaVersion": 1, "tools": ["query:execute"], "tables": {}},
        )
        store.bind_policy(account.id, policy["id"])
        updated = store.update_policy(
            policy["id"],
            {"schemaVersion": 1, "tools": ["semantic:read"], "tables": {}},
        )
        event_id = store.record_audit(
            action="query.run",
            decision="allowed",
            auth=auth,
            resource="sales",
            policy_version=f"{policy['id']}:2",
        )

        self.assertEqual(auth.subject.attributes, {"regionCodes": ["CN-JIA"]})
        self.assertEqual(updated["version"], 2)
        self.assertEqual(store.policies_for_subject(account.id)[0]["version"], 2)
        self.assertEqual(store.list_service_accounts()[0]["policyIds"], [policy["id"]])
        self.assertEqual(store.list_audit()[0]["id"], event_id)
        self.assertTrue(any("%s" in statement for statement, _ in self.statements))
        self.assertFalse(any("Region A Agent" in statement for statement, _ in self.statements))
        self.assertNotIn(issued["apiKey"].encode(), self.database.read_bytes())

    def test_sql_translation_preserves_bound_values_and_maps_insert_ignore(self) -> None:
        translated = _postgres_sql(
            "INSERT OR IGNORE INTO policy_bindings(subject_id,policy_id,created_at) VALUES(?,?,?)"
        )
        self.assertEqual(
            translated,
            "INSERT INTO policy_bindings(subject_id,policy_id,created_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
        )


if __name__ == "__main__":
    unittest.main()
