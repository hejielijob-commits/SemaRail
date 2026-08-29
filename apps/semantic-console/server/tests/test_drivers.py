from __future__ import annotations

import unittest
from unittest.mock import patch

from server.drivers import datasource_types


class DatasourceTypeTests(unittest.TestCase):
    @staticmethod
    def _available(name: str) -> bool:
        return name in {"psycopg", "mysql.connector"}

    def test_only_configured_available_drivers_are_returned(self) -> None:
        with patch("server.drivers._module_available", side_effect=self._available):
            result = datasource_types()

        self.assertEqual([item["type"] for item in result], ["postgres", "mysql"])
        self.assertEqual([item["label"] for item in result], ["PostgreSQL", "MySQL"])
        self.assertTrue(all(item["available"] for item in result))
        self.assertTrue(all(item["supportsSchemaBrowse"] for item in result))
        self.assertTrue(all(isinstance(item["fields"], list) for item in result))

    def test_uninstalled_driver_is_not_a_selectable_placeholder(self) -> None:
        with patch("server.drivers._module_available", side_effect=lambda name: name == "psycopg"):
            result = datasource_types()

        self.assertEqual([item["type"] for item in result], ["postgres"])


if __name__ == "__main__":
    unittest.main()
