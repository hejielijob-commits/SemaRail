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


class RowPolicyTests(unittest.TestCase):
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
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT amount FROM public.payroll", region_policy("CN-JIA"))
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT customer_phone FROM public.sales", region_policy("CN-JIA"))
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT * FROM public.sales", region_policy("CN-JIA"))
        malformed = region_policy("CN-JIA")
        malformed["tables"]["public.sales"]["rowFilter"]["conditions"][0]["conditions"][1]["values"] = []
        with self.assertRaises(RowPolicyError):
            apply_row_policy("SELECT amount FROM public.sales", malformed)


if __name__ == "__main__":
    unittest.main()
