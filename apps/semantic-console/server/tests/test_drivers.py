from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.drivers import BaseDriver, ClickhouseDriver, DuckdbDriver, SqliteDriver, datasource_types, driver_for


class FakeCursor:
    description = [("SCHEMA_NAME",)]

    @staticmethod
    def fetchall() -> list[tuple[str]]:
        return [("dsh_data_agent_e2e",)]


class DatasourceTypeTests(unittest.TestCase):
    @staticmethod
    def _available(name: str) -> bool:
        return name in {"psycopg", "mysql.connector", "sqlite3", "clickhouse_connect", "duckdb"}

    def test_only_configured_available_drivers_are_returned(self) -> None:
        with patch("server.drivers._module_available", side_effect=self._available):
            result = datasource_types()

        self.assertEqual([item["type"] for item in result], ["postgres", "mysql", "sqlite", "clickhouse", "duckdb"])
        self.assertEqual([item["label"] for item in result], ["PostgreSQL", "MySQL", "SQLite", "ClickHouse", "DuckDB"])
        self.assertTrue(all(item["available"] for item in result))
        self.assertTrue(all(item["supportsSchemaBrowse"] for item in result))
        self.assertTrue(all(isinstance(item["fields"], list) for item in result))

    def test_uninstalled_driver_is_not_a_selectable_placeholder(self) -> None:
        with patch("server.drivers._module_available", side_effect=lambda name: name == "psycopg"):
            result = datasource_types()

        self.assertEqual([item["type"] for item in result], ["postgres", "sqlite"])

    def test_dbapi_column_names_are_normalized_for_mysql_metadata(self) -> None:
        self.assertEqual(
            BaseDriver._fetch_rows(FakeCursor()),
            [{"schema_name": "dsh_data_agent_e2e"}],
        )

    def test_driver_factory_exposes_new_adapters(self) -> None:
        self.assertIsInstance(driver_for("sqlite"), SqliteDriver)
        self.assertIsInstance(driver_for("duckdb"), DuckdbDriver)
        self.assertIsInstance(driver_for("clickhouse"), ClickhouseDriver)

    def test_sqlite_browses_an_existing_file_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semarail-sqlite-") as directory:
            database = Path(directory) / "analytics.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL);"
                "CREATE VIEW order_totals AS SELECT SUM(amount) AS total FROM orders;"
            )
            connection.close()

            driver = SqliteDriver()
            values = {"path": str(database)}
            self.assertEqual(driver.test_connection(values)["driver"], "sqlite")
            self.assertEqual(driver.schemas(values), [{"name": "main"}])
            self.assertEqual([item["name"] for item in driver.tables(values, "main")], ["order_totals", "orders"])
            columns = driver.columns(values, "main", "orders")
            self.assertEqual(columns[0]["name"], "id")
            self.assertTrue(columns[0]["primaryKey"])
            self.assertFalse(columns[1]["nullable"])

    @unittest.skipUnless(importlib.util.find_spec("duckdb") is not None, "duckdb is not installed")
    def test_duckdb_browses_an_existing_file_read_only(self) -> None:
        import duckdb

        with tempfile.TemporaryDirectory(prefix="semarail-duckdb-") as directory:
            database = Path(directory) / "analytics.duckdb"
            connection = duckdb.connect(str(database))
            connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount DECIMAL(12, 2) NOT NULL)")
            connection.close()

            driver = DuckdbDriver()
            values = {"path": str(database)}
            self.assertEqual(driver.test_connection(values)["driver"], "duckdb")
            self.assertIn({"name": "main"}, driver.schemas(values))
            self.assertEqual(driver.tables(values, "main")[0]["name"], "orders")
            columns = driver.columns(values, "main", "orders")
            self.assertTrue(columns[0]["primaryKey"])
            self.assertFalse(columns[1]["nullable"])

    def test_clickhouse_uses_bound_server_parameters(self) -> None:
        class Result:
            column_names = ("name", "engine")
            result_rows = (("orders", "MergeTree"),)

        class Client:
            def __init__(self) -> None:
                self.calls = []

            def query(self, sql, parameters=None):
                self.calls.append((sql, parameters))
                return Result()

            def close(self):
                return None

        client = Client()
        driver = ClickhouseDriver(lambda _values: client)
        self.assertEqual(driver.tables({}, "analytics"), [{"name": "orders", "type": "BASE TABLE"}])
        self.assertIn("{schema:String}", client.calls[0][0])
        self.assertEqual(client.calls[0][1], {"schema": "analytics"})


if __name__ == "__main__":
    unittest.main()
