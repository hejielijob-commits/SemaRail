from __future__ import annotations

import unittest

from server.access_control import Subject
from server.authorization import PolicyEngine


def policy(policy_id: str, region_attribute: str = "regionCodes") -> dict:
    return {
        "id": policy_id,
        "organizationId": "org-sales",
        "name": policy_id,
        "version": 1,
        "document": {
            "schemaVersion": 1,
            "projects": ["sales"],
            "tools": ["semantic:read", "query:execute", "query:cancel"],
            "tables": {
                "sales.orders": {
                    "effect": "allow",
                    "tenantField": "organization_id",
                    "columns": {"allow": ["order_id", "region_code", "amount"], "deny": ["customer_phone"]},
                    "rows": [
                        {"field": "region_code", "operator": "in", "valueFrom": f"subject.attributes.{region_attribute}"}
                    ],
                }
            },
            "limits": {"maxRows": 500, "timeoutMs": 10000},
        },
    }


class PolicyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PolicyEngine()
        self.user_a = Subject("user-a", "org-sales", "user", "A", {"regionCodes": ["CN-JIA"]})
        self.user_b = Subject("user-b", "org-sales", "user", "B", {"regionCodes": ["CN-YI"]})

    def test_sales_region_values_are_derived_from_each_subject(self) -> None:
        decision_a = self.engine.authorize_table(self.user_a, "sales.orders", [policy("pol-sales")])
        decision_b = self.engine.authorize_table(self.user_b, "sales.orders", [policy("pol-sales")])

        self.assertTrue(decision_a.allowed)
        self.assertEqual(self.engine.allowed_values(decision_a, "region_code"), ("CN-JIA",))
        self.assertEqual(self.engine.allowed_values(decision_b, "region_code"), ("CN-YI",))
        self.assertIn("customer_phone", decision_a.denied_columns)
        self.assertNotIn("customer_phone", decision_a.allowed_columns or ())

    def test_requested_region_can_only_intersect_with_authorized_values(self) -> None:
        decision = self.engine.authorize_table(self.user_a, "sales.orders", [policy("pol-sales")])
        allowed = set(self.engine.allowed_values(decision, "region_code"))
        self.assertEqual(allowed.intersection({"CN-JIA"}), {"CN-JIA"})
        self.assertEqual(allowed.intersection({"CN-YI"}), set())

    def test_multiple_bound_policies_union_allowed_region_scopes(self) -> None:
        user = Subject("manager", "org-sales", "user", "Manager", {"regionCodes": ["CN-JIA", "CN-YI"]})
        decision = self.engine.authorize_table(user, "sales.orders", [policy("pol-manager")])
        self.assertEqual(self.engine.allowed_values(decision, "region_code"), ("CN-JIA", "CN-YI"))

    def test_unrestricted_allow_grant_widens_row_union_without_invalid_empty_group(self) -> None:
        unrestricted = policy("pol-all")
        unrestricted["document"]["tables"]["sales.orders"].pop("tenantField")
        unrestricted["document"]["tables"]["sales.orders"]["rows"] = []

        decision = self.engine.authorize_table(
            self.user_a, "sales.orders", [policy("pol-region"), unrestricted]
        )

        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.row_filter)

    def test_missing_subject_attribute_and_malformed_policy_fail_closed(self) -> None:
        missing = Subject("user-c", "org-sales", "user", "C", {})
        self.assertFalse(self.engine.authorize_table(missing, "sales.orders", [policy("pol-sales")]).allowed)
        malformed = policy("pol-bad")
        malformed["document"]["tables"]["sales.orders"]["rows"][0]["operator"] = "raw_sql"
        self.assertFalse(self.engine.authorize_table(self.user_a, "sales.orders", [malformed]).allowed)

    def test_tool_scope_defaults_to_deny_and_explicit_deny_wins(self) -> None:
        self.assertTrue(self.engine.authorize_method(self.user_a, "context.ask", [policy("pol-sales")]).allowed)
        self.assertTrue(self.engine.authorize_method(self.user_a, "project.describe", [policy("pol-sales")]).allowed)
        self.assertFalse(self.engine.authorize_method(self.user_a, "query.dryPlan", [policy("pol-sales")]).allowed)
        self.assertFalse(self.engine.authorize_method(self.user_a, "project.validate", [policy("pol-sales")]).allowed)
        denied = policy("pol-denied")
        denied["document"]["denyTools"] = ["query:execute"]
        self.assertFalse(self.engine.authorize_method(self.user_a, "query.run", [policy("pol-sales"), denied]).allowed)

    def test_project_scope_is_required_when_runtime_supplies_a_project(self) -> None:
        self.assertTrue(
            self.engine.authorize_method(self.user_a, "context.ask", [policy("pol-sales")], project_id="sales").allowed
        )
        self.assertFalse(
            self.engine.authorize_method(self.user_a, "context.ask", [policy("pol-sales")], project_id="finance").allowed
        )

    def test_foreign_project_policy_cannot_widen_current_project_rows(self) -> None:
        current = policy("pol-current")
        foreign = policy("pol-foreign")
        foreign["document"]["projects"] = ["finance"]
        foreign_rule = foreign["document"]["tables"]["sales.orders"]
        foreign_rule.pop("tenantField")
        foreign_rule["rows"] = []

        compiled = self.engine.compile_data_policy(
            self.user_a, [current, foreign], project_id="sales"
        )

        row_filter = compiled["tables"]["sales.orders"]["rowFilter"]
        self.assertIsNotNone(row_filter)
        self.assertEqual(
            row_filter["conditions"][0]["conditions"][1]["values"], ["CN-JIA"]
        )
        self.assertEqual(compiled["policyVersions"], ["pol-current:1"])


if __name__ == "__main__":
    unittest.main()
