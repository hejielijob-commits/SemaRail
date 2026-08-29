#!/usr/bin/env python3
"""Credential-free stdio fixture for the real DSH MCP adapter."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from sidecar.mcp_gateway import create_governed_mcp_server
from sidecar.query import QueryLimits, WrenQueryService


class FixturePlanner:
    def dry_plan(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"nativeSql": params["semanticSql"]}


class FixtureExecutor:
    def execute(
        self,
        *,
        query_id: str,
        semantic_sql: str,
        native_sql: str,
        project_dir: str,
        connection_info: Mapping[str, Any] | None,
        limits: QueryLimits,
    ) -> dict[str, Any]:
        del project_dir
        if connection_info != {"datasource": "postgres", "fixture": True}:
            raise RuntimeError("fixture connection policy was not pinned")
        return {
            "schemaVersion": 1,
            "queryId": query_id,
            "status": "success",
            "semanticSql": semantic_sql,
            "nativeSql": native_sql,
            "columns": [
                {"name": "id", "type": "BIGINT", "semanticRole": "measure"}
            ],
            "previewRows": [{"id": "1"}][: limits.preview_rows],
            "stats": {
                "returnedRows": min(2, limits.max_rows),
                "durationMs": 1,
                "truncated": limits.max_rows <= 2 or limits.preview_rows <= 1,
            },
        }

    def cancel(self, query_id: str) -> bool:
        del query_id
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    service = WrenQueryService(
        FixturePlanner(),
        FixtureExecutor(),
        connection_resolver=lambda _project, _env: {
            "datasource": "postgres",
            "fixture": True,
        },
    )
    server = create_governed_mcp_server(
        project=args.project,
        database_dsn_env="FIXTURE_DATABASE_URL",
        query_service=service,
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
