from __future__ import annotations

import unittest

from sidecar.row_policy import RowPolicyError, apply_row_policy


def region_policy(region: str) -> dict:
    return {
        "schemaVersion": 1,
        "defaultEffect": "deny",
        "policyVersions": ["pol-sales:1"],
        "tables": {
            "public.sales": {
                "rowFilter": {
                    "op": "or",
                    "conditions": [{
                        "op": "and",
                        "conditions": [
                            {"field": "organization_id", "operator": "eq", "values": ["org-sales"]},
                            {"field": "region_code", "operator": "in", "values": [region]},
                        ],
                    }],
                },
                "allowedColumns": ["order_id", "region_code", "amount", "organization_id"],
                "deniedColumns": ["customer_phone"],
            }
        },
    }


def permission_policy(*, include_self: bool | None = None, schema_version: int = 2) -> dict:
    condition = {
        "field": "employee_id",
        "operator": "permissionLookup",
        "values": ["user-a"],
        "lookup": {
            "table": "auth.employee_permissions",
            "principalField": "principal_id",
            "targetField": "employee_id",
            "organizationField": "organization_id",
        },
        "organizationValue": "org-sales",
    }
    if include_self is not None:
        condition["includeSelf"] = include_self
    return {
        "schemaVersion": schema_version,
        "defaultEffect": "deny",
        "policyVersions": ["pol-sales:2"],
        "tables": {
            "public.employees": {
                "rowFilter": condition,
                "allowedColumns": ["employee_id", "region_code"],
                "deniedColumns": [],
            },
        },
    }


class RowPolicyTests(unittest.TestCase):
    def test_permission_lookup_is_parameterized_tenant_scoped_and_non_recursive(self) -> None:
        query = apply_row_policy(
            "SELECT e.employee_id FROM public.employees e",
            permission_policy(),
        )
        self.assertIn(
            "EXISTS(SELECT 1 FROM auth.employee_permissions AS __srp_lookup_0",
            query.sql,
        )
        self.assertIn("__srp_lookup_0.principal_id = %(srp_0)s", query.sql)
        self.assertIn("__srp_lookup_0.organization_id = %(srp_1)s", query.sql)
        self.assertIn("__srp_lookup_0.employee_id = __srp_source_0.employee_id", query.sql)
        self.assertNotIn("user-a", query.sql)
        self.assertNotIn("org-sales", query.sql)
        self.assertEqual(query.parameters, {"srp_0": "user-a", "srp_1": "org-sales"})
        self.assertEqual(query.applied_tables, ("public.employees",))
        self.assertEqual(len(query.lookup_tables), 1)
        self.assertEqual(query.lookup_tables[0].normalized(), (None, "auth", "employee_permissions"))
        # The lookup relation is generated after the source walk and is not
        # recursively wrapped even when it is itself policy-addressable.
        self.assertEqual(query.sql.count("FROM auth.employee_permissions"), 1)
        self.assertNotIn("SELECT * FROM auth.employee_permissions", query.sql)

    def test_permission_lookup_defaults_to_excluding_self_and_can_include_self(self) -> None:
        without_self = apply_row_policy(
            "SELECT employee_id FROM public.employees",
            permission_policy(),
        )
        self.assertNotIn(" OR __srp_source_0.employee_id =", without_self.sql)

        with_self = apply_row_policy(
            "SELECT employee_id FROM public.employees",
            permission_policy(include_self=True),
        )
        self.assertIn(" OR __srp_source_0.employee_id = %(srp_0)s", with_self.sql)
        self.assertEqual(with_self.parameters, without_self.parameters)

    def test_permission_lookup_alias_avoids_user_aliases_across_join_and_cte(self) -> None:
        policy = permission_policy()
        policy["tables"]["public.employee_assignments"] = {
            "rowFilter": {
                "op": "and",
                "conditions": [
                    {
                        "field": "employee_id",
                        "operator": "permissionLookup",
                        "values": ["user-a"],
                        "lookup": policy["tables"]["public.employees"]["rowFilter"]["lookup"],
                        "organizationValue": "org-sales",
                    },
                    {"field": "region_code", "operator": "eq", "values": ["CN-JIA"]},
                ],
            },
            "allowedColumns": ["employee_id", "region_code"],
            "deniedColumns": [],
        }
        query = apply_row_policy(
            "WITH employee_rows AS ("
            "SELECT e.employee_id FROM public.employees AS __srp_lookup_0) "
            "SELECT employee_rows.employee_id FROM employee_rows "
            "JOIN public.employee_assignments AS __srp_source_0 "
            "ON employee_rows.employee_id = __srp_source_0.employee_id",
            policy,
        )
        self.assertIn("FROM auth.employee_permissions AS __srp_lookup_1", query.sql)
        self.assertIn("FROM auth.employee_permissions AS __srp_lookup_2", query.sql)
        self.assertIn("AS __srp_source_1", query.sql)
        self.assertIn("AS __srp_source_2", query.sql)
        self.assertEqual(query.sql.count("FROM auth.employee_permissions"), 2)
        self.assertEqual(set(query.applied_tables), {"public.employees", "public.employee_assignments"})

    def test_permission_lookup_is_v2_only(self) -> None:
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT employee_id FROM public.employees", permission_policy(schema_version=1))

    def test_user_a_and_b_compile_to_different_bound_region_values(self) -> None:
        sql = "SELECT sales.region_code, SUM(sales.amount) AS revenue FROM public.sales GROUP BY sales.region_code"
        query_a = apply_row_policy(sql, region_policy("CN-JIA"))
        query_b = apply_row_policy(sql, region_policy("CN-YI"))
        self.assertIn("FROM (SELECT * FROM public.sales WHERE", query_a.sql)
        self.assertEqual(set(query_a.parameters.values()), {"org-sales", "CN-JIA"})
        self.assertEqual(set(query_b.parameters.values()), {"org-sales", "CN-YI"})
        self.assertNotIn("CN-JIA", query_a.sql)
        self.assertEqual(query_a.applied_tables, ("public.sales",))

    def test_outer_user_predicate_cannot_remove_inner_mandatory_filter(self) -> None:
        query = apply_row_policy(
            "SELECT order_id FROM public.sales WHERE region_code = 'CN-YI' OR 1 = 1",
            region_policy("CN-JIA"),
        )
        self.assertIn("FROM (SELECT * FROM public.sales WHERE", query.sql)
        self.assertIn("WHERE region_code = 'CN-YI' OR 1 = 1", query.sql)
        self.assertEqual(set(query.parameters.values()), {"org-sales", "CN-JIA"})

    def test_join_wraps_each_protected_source_without_changing_outer_join(self) -> None:
        policy = region_policy("CN-JIA")
        policy["tables"]["public.targets"] = {
            "rowFilter": {
                "op": "or",
                "conditions": [{"op": "and", "conditions": [
                    {"field": "region_code", "operator": "in", "values": ["CN-JIA"]}
                ]}],
            },
            "allowedColumns": ["region_code", "target"],
            "deniedColumns": [],
        }
        query = apply_row_policy(
            "SELECT sales.region_code, targets.target FROM public.sales LEFT JOIN public.targets ON sales.region_code = targets.region_code",
            policy,
        )
        self.assertIn("LEFT JOIN (SELECT * FROM public.targets WHERE", query.sql)
        self.assertEqual(set(query.applied_tables), {"public.sales", "public.targets"})

    def test_same_named_cte_cannot_hide_its_physical_source(self) -> None:
        query = apply_row_policy(
            "WITH sales AS (SELECT region_code, amount FROM public.sales) SELECT region_code, amount FROM sales",
            region_policy("CN-JIA"),
        )
        self.assertIn("FROM (SELECT * FROM public.sales WHERE", query.sql)
        self.assertEqual(query.applied_tables, ("public.sales",))
        self.assertEqual(set(query.parameters.values()), {"org-sales", "CN-JIA"})

    def test_nested_alias_shadow_cannot_replace_outer_column_policy(self) -> None:
        policy = region_policy("CN-JIA")
        policy["tables"]["public.targets"] = {
            "rowFilter": None,
            "allowedColumns": ["customer_phone"],
            "deniedColumns": [],
        }
        with self.assertRaises(RowPolicyError):
            apply_row_policy(
                "SELECT s.customer_phone FROM public.sales s "
                "WHERE EXISTS (SELECT 1 FROM public.targets s)",
                policy,
            )

    def test_self_join_and_correlated_subquery_keep_each_lexical_source_policy(self) -> None:
        policy = region_policy("CN-JIA")
        query = apply_row_policy(
            "SELECT current.order_id FROM public.sales current "
            "JOIN public.sales previous ON current.order_id = previous.order_id "
            "WHERE EXISTS (SELECT 1 FROM public.sales nested "
            "WHERE nested.region_code = current.region_code)",
            policy,
        )
        self.assertEqual(query.sql.count("SELECT * FROM public.sales WHERE"), 3)
        self.assertEqual(query.applied_tables, ("public.sales",))

    def test_unqualified_column_in_multi_source_scope_must_be_allowed_by_every_protected_source(self) -> None:
        policy = region_policy("CN-JIA")
        policy["tables"]["public.targets"] = {
            "rowFilter": None,
            "allowedColumns": ["target"],
            "deniedColumns": [],
        }
        with self.assertRaises(RowPolicyError):
            apply_row_policy(
                "SELECT amount FROM public.sales s JOIN public.targets t "
                "ON s.region_code = t.target",
                policy,
            )

    def test_unlisted_table_denied_column_wildcard_and_missing_attribute_fail_closed(self) -> None:
        with self.assertRaises(RowPolicyError) as table_error:
            apply_row_policy("SELECT amount FROM public.payroll", region_policy("CN-JIA"))
        self.assertEqual(table_error.exception.reason_code, "TABLE_PERMISSION_REQUIRED")
        self.assertEqual(table_error.exception.resource_name, "public.payroll")
        with self.assertRaises(RowPolicyError) as column_error:
            apply_row_policy("SELECT customer_phone FROM public.sales", region_policy("CN-JIA"))
        self.assertEqual(column_error.exception.reason_code, "COLUMN_PERMISSION_REQUIRED")
        self.assertEqual(column_error.exception.resource_name, "public.sales.customer_phone")
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT * FROM public.sales", region_policy("CN-JIA"))
        malformed = region_policy("CN-JIA")
        malformed["tables"]["public.sales"]["rowFilter"]["conditions"][0]["conditions"][1]["values"] = []
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT amount FROM public.sales", malformed)

    def test_unquoted_identifier_case_cannot_bypass_column_policy(self) -> None:
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT CUSTOMER_PHONE FROM public.sales", region_policy("CN-JIA"))
        allowed = apply_row_policy(
            "SELECT ORDER_ID FROM public.sales",
            region_policy("CN-JIA"),
        )
        self.assertEqual(allowed.applied_tables, ("public.sales",))


if __name__ == "__main__":
    unittest.main()
