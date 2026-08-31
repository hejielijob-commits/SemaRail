from __future__ import annotations

import unittest

from sidecar.dispatch import Dispatcher, SidecarDependencies, dispatch_request
from sidecar.errors import RpcFault


def request(method: str, params: object | None = None, request_id: str = "req-1") -> dict[str, object]:
    return {
        "protocolVersion": "1",
        "id": request_id,
        "method": method,
        "params": {} if params is None else params,
    }


class FakeValidator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def validate(self, params: object) -> dict[str, object]:
        assert isinstance(params, dict)
        self.calls.append(params)
        return {"valid": True, "warningCount": 0}


class FakeContextProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def describe(self, params: object) -> dict[str, object]:
        assert isinstance(params, dict)
        self.calls.append(params)
        return {"schemaVersion": 1, "models": [{"name": "orders"}], "relationships": []}

    def ask(self, _params: object) -> dict[str, object]:
        return {"schemaVersion": 1, "models": [], "relationships": []}


class DispatchTests(unittest.TestCase):
    def test_restricted_semantic_output_hides_denied_tables_columns_and_sql_plan(self) -> None:
        class Provider:
            def describe(self, _params: object) -> dict[str, object]:
                return {
                    "schemaVersion": 1,
                    "projectRevision": "sha256:test",
                    "models": [
                        {"name": "orders", "table": "public.orders", "columns": [
                            {"name": "order_id", "type": "INTEGER"},
                            {"name": "secret", "type": "TEXT"},
                        ]},
                        {"name": "payroll", "table": "audit.payroll", "columns": [
                            {"name": "salary", "type": "DECIMAL"},
                        ]},
                        {"name": "customers", "table": "public.customers", "columns": [
                            {"name": "customer_id", "type": "INTEGER"},
                            {"name": "private_note", "type": "TEXT"},
                        ]},
                    ],
                    "relationships": [
                        {"name": "orders_payroll", "models": ["orders", "payroll"], "joinType": "ONE_TO_ONE", "condition": "orders.secret = payroll.salary"},
                        {"name": "orders_customers", "models": ["orders", "customers"], "joinType": "MANY_TO_ONE", "condition": "orders.order_id = customers.customer_id"},
                        {"name": "private_customers", "models": ["orders", "customers"], "joinType": "MANY_TO_ONE", "condition": "orders.order_id = customers.private_note"},
                    ],
                    "views": [{"name": "all_data", "statement": "SELECT * FROM audit.payroll"}],
                    "summary": "audit.payroll secret",
                }

            def ask(self, params: object) -> dict[str, object]:
                return self.describe(params)

        class Planner:
            def dry_plan(self, _params: object) -> dict[str, object]:
                return {
                    "semanticSql": "SELECT order_id FROM orders",
                    "nativeSql": "SELECT order_id FROM public.orders",
                    "projectRevision": "sha256:test",
                    "allowedPhysical": {
                        "catalogs": [], "schemas": ["public", "audit"],
                        "tables": [{"schema": "public", "table": "orders"}, {"schema": "audit", "table": "payroll"}],
                    },
                }

        policy = {
            "schemaVersion": 1,
            "defaultEffect": "deny",
            "tables": {
                "public.orders": {"rowFilter": None, "allowedColumns": ["order_id"], "deniedColumns": ["secret"]},
                "public.customers": {"rowFilter": None, "allowedColumns": ["customer_id"], "deniedColumns": ["private_note"]},
            },
            "policyVersions": ["policy:1"],
        }
        dispatcher = Dispatcher(SidecarDependencies(context_provider=Provider(), query_planner=Planner()))

        for method, params in (
            ("project.describe", {"projectDir": "project", "authorizationPolicy": policy}),
            ("context.ask", {"projectDir": "project", "question": "show orders", "authorizationPolicy": policy}),
        ):
            with self.subTest(method=method):
                response = dispatcher.dispatch(request(method, params))
                self.assertTrue(response["ok"])
                result = response["result"]
                self.assertEqual(result["models"], [
                    {"name": "orders", "table": "public.orders", "columns": [{"name": "order_id", "type": "INTEGER"}]},
                    {"name": "customers", "table": "public.customers", "columns": [{"name": "customer_id", "type": "INTEGER"}]},
                ])
                self.assertEqual(result["relationships"], [{
                    "name": "orders_customers",
                    "models": ["orders", "customers"],
                    "joinType": "MANY_TO_ONE",
                    "condition": "orders.order_id = customers.customer_id",
                }])
                self.assertNotIn("views", result)
                self.assertNotIn("summary", result)

        plan = dispatcher.dispatch(request("query.dryPlan", {"projectDir": "project", "semanticSql": "SELECT order_id FROM orders", "authorizationPolicy": policy}))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["result"]["allowedPhysical"]["tables"], [{"schema": "public", "table": "orders"}])

    def test_restricted_dry_plan_and_missing_policy_fail_closed(self) -> None:
        policy = {"schemaVersion": 1, "defaultEffect": "deny", "tables": {}, "policyVersions": []}
        denied = Dispatcher(query_planner=lambda _: {
            "semanticSql": "SELECT * FROM payroll",
            "nativeSql": "SELECT salary FROM audit.payroll",
            "projectRevision": "sha256:test",
            "allowedPhysical": {"catalogs": [], "schemas": ["audit"], "tables": [{"schema": "audit", "table": "payroll"}]},
        }).dispatch(request("query.dryPlan", {"projectDir": "project", "semanticSql": "SELECT * FROM payroll", "authorizationPolicy": policy}))
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "POLICY_DENIED")

        missing = Dispatcher(context_provider=FakeContextProvider()).dispatch(request("project.describe", {
            "projectDir": "project", "authorizationPolicy": {"schemaVersion": 1, "defaultEffect": "deny"},
        }))
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["code"], "POLICY_DENIED")

    def test_restricted_model_without_physical_table_uses_unique_policy_suffix(self) -> None:
        class Provider:
            @staticmethod
            def describe(_: object) -> dict[str, object]:
                return {
                    "schemaVersion": 1,
                    "projectRevision": "sha256:test",
                    "models": [{"name": "orders", "columns": [{"name": "ORDER_ID", "type": "INTEGER"}]}],
                    "relationships": [],
                }

        response = Dispatcher(context_provider=Provider()).dispatch(request("project.describe", {
            "projectDir": "project",
            "authorizationPolicy": {
                "schemaVersion": 1,
                "defaultEffect": "deny",
                "tables": {"public.orders": {"rowFilter": None, "allowedColumns": ["order_id"], "deniedColumns": []}},
                "policyVersions": ["policy:1"],
            },
        }))
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["models"][0]["columns"][0]["name"], "ORDER_ID")

    def test_explicit_bootstrap_allow_policy_leaves_semantic_response_unchanged(self) -> None:
        provider = FakeContextProvider()
        response = Dispatcher(context_provider=provider).dispatch(request("project.describe", {
            "projectDir": "project",
            "authorizationPolicy": {"schemaVersion": 1, "defaultEffect": "allow", "tables": {}, "policyVersions": []},
        }))
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["models"], [{"name": "orders"}])

    def test_health_does_not_need_wren(self) -> None:
        response = Dispatcher().dispatch(request("health"))
        self.assertEqual(response, {
            "protocolVersion": "1",
            "id": "req-1",
            "ok": True,
            "result": {"status": "ok", "protocolVersion": "1"},
        })

        null_params = request("health")
        null_params["params"] = None
        invalid = Dispatcher().dispatch(null_params)
        self.assertEqual(invalid["error"]["code"], "INVALID_PARAMS")

    def test_project_validate_uses_injected_dependency(self) -> None:
        validator = FakeValidator()
        dispatcher = Dispatcher(SidecarDependencies(project_validator=validator))
        params = {"projectDir": "./example-project"}
        response = dispatcher.dispatch(request("project.validate", params))
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], {"valid": True, "warningCount": 0})
        self.assertEqual(validator.calls, [params])

    def test_callable_validator_is_also_supported(self) -> None:
        seen: list[object] = []
        dispatcher = Dispatcher(project_validator=lambda params: seen.append(params) or {"valid": True})
        response = dispatcher.dispatch(request("project.validate", {"projectDir": "x"}))
        self.assertEqual(response["result"], {"valid": True})
        self.assertEqual(seen, [{"projectDir": "x"}])

    def test_project_describe_uses_context_provider_without_a_question(self) -> None:
        provider = FakeContextProvider()
        dispatcher = Dispatcher(SidecarDependencies(context_provider=provider))
        response = dispatcher.dispatch(request("project.describe", {"projectDir": "project"}))
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["models"][0]["name"], "orders")
        self.assertEqual(provider.calls, [{"projectDir": "project"}])

    def test_missing_wren_dependency_is_stable(self) -> None:
        response = dispatch_request(request("project.validate", {"projectDir": "x"}))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], {
            "code": "WREN_UNAVAILABLE",
            "phase": "project.validate",
            "message": "SemaRail project validator is unavailable",
            "retryable": True,
        })

    def test_unknown_method_and_invalid_params_are_structured(self) -> None:
        unknown = Dispatcher().dispatch(request("query.run"))
        self.assertEqual(unknown["error"]["code"], "INVALID_PARAMS")

        invalid = Dispatcher().dispatch(request("project.validate", ["not", "object"]))
        self.assertEqual(invalid["error"], {
            "code": "INVALID_PARAMS",
            "phase": "validation",
            "message": "params must be an object",
            "retryable": False,
        })

    def test_protocol_and_request_validation_fail_closed(self) -> None:
        unsupported = Dispatcher().dispatch({
            "protocolVersion": "2",
            "id": "req-2",
            "method": "health",
            "params": {},
        })
        self.assertEqual(unsupported["error"]["code"], "UNSUPPORTED_PROTOCOL")
        self.assertEqual(unsupported["id"], "req-2")

        missing_id = Dispatcher().dispatch({
            "protocolVersion": "1",
            "method": "health",
            "params": {},
        })
        self.assertEqual(missing_id["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(missing_id["id"], "")

    def test_request_envelope_matches_typescript_contract(self) -> None:
        missing_params = Dispatcher().dispatch({
            "protocolVersion": "1",
            "id": "missing-params",
            "method": "health",
        })
        self.assertEqual(missing_params["error"]["code"], "INVALID_REQUEST")

        unknown_field = Dispatcher().dispatch({
            **request("health"),
            "extra": True,
        })
        self.assertEqual(unknown_field["error"]["code"], "INVALID_REQUEST")

        for invalid_deadline in (-1, 1.5, True):
            invalid = Dispatcher().dispatch({
                **request("health"),
                "deadlineMs": invalid_deadline,
            })
            self.assertEqual(invalid["error"]["code"], "INVALID_REQUEST")
        accepted = Dispatcher().dispatch({
            **request("health"),
            "deadlineMs": 0,
        })
        self.assertTrue(accepted["ok"])

        not_json = Dispatcher().dispatch({
            **request("health"),
            "params": {"bad": {1, 2}},
        })
        self.assertEqual(not_json["error"]["code"], "INVALID_REQUEST")
        non_finite = Dispatcher().dispatch({
            **request("health"),
            "params": {"bad": float("nan")},
        })
        self.assertEqual(non_finite["error"]["code"], "INVALID_REQUEST")

        # Envelope params may be any JSON-safe value. Method validation is
        # where health rejects a scalar because it requires an object.
        scalar = Dispatcher().dispatch({
            **request("health"),
            "params": 7,
        })
        self.assertEqual(scalar["error"]["code"], "INVALID_PARAMS")

        cancel = Dispatcher().dispatch(request("query.cancel"))
        self.assertEqual(cancel["error"]["code"], "INVALID_PARAMS")

    def test_rpc_fault_and_unexpected_validator_failure_are_safe(self) -> None:
        expected = Dispatcher(project_validator=lambda _: (_ for _ in ()).throw(
            RpcFault("PROJECT_INVALID", "project.validate", "project is invalid", False)
        )).dispatch(request("project.validate", {"projectDir": "x"}))
        self.assertEqual(expected["error"], {
            "code": "INTERNAL_ERROR",
            "phase": "dispatch",
            "message": "internal sidecar error",
            "retryable": False,
        })
        self.assertNotIn("PROJECT_INVALID", str(expected))
        self.assertNotIn("project is invalid", str(expected))

        unexpected = Dispatcher(project_validator=lambda _: 1 / 0).dispatch(
            request("project.validate", {"projectDir": "x"})
        )
        self.assertEqual(unexpected["error"], {
            "code": "PROJECT_VALIDATION_FAILED",
            "phase": "project.validate",
            "message": "project validation failed",
            "retryable": False,
        })


if __name__ == "__main__":
    unittest.main()
