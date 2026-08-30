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


class DispatchTests(unittest.TestCase):
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
