#!/usr/bin/env python3
"""Credential-free end-to-end acceptance for WrenAI's native MCP server.

The gate creates an isolated Wren project, WREN_HOME, and DuckDB database in a
temporary directory.  It then drives ``wren serve mcp`` through the official
Python MCP stdio client and verifies the real context -> plan -> validate ->
query workflow.  No user profile, database, or project file is read or changed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any


class AcceptanceFailure(RuntimeError):
    """A concise, credential-free acceptance failure."""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _wren_command() -> Path:
    scripts = Path(sysconfig.get_path("scripts"))
    candidates = [scripts / "wren.exe", scripts / "wren"]
    discovered = shutil.which("wren")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise AcceptanceFailure(
        "wren executable is unavailable; install python/sidecar with the mcp extra"
    )


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    if completed.returncode:
        detail = completed.stdout.strip()[-2000:]
        raise AcceptanceFailure(
            f"command failed ({Path(command[0]).name} {command[1]}): {detail}"
        )


def _tool_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    raise AcceptanceFailure("MCP tool returned no readable content")


def _prepare(root: Path) -> tuple[Path, Path, Path]:
    try:
        import duckdb
    except ImportError as exc:
        raise AcceptanceFailure("duckdb is unavailable") from exc

    project = root / "project"
    data = root / "data"
    wren_home = root / "wren-home"
    data.mkdir(parents=True)
    wren_home.mkdir(parents=True)

    database = data / "dsh_mcp_e2e.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "CREATE TABLE orders("
            "id BIGINT PRIMARY KEY, customer_name VARCHAR NOT NULL, "
            "amount DECIMAL(12,2) NOT NULL, ordered_at TIMESTAMP NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            [
                (1, "Acme North", 1280.50, "2026-08-15 09:30:00"),
                (2, "Blue River", 860.00, "2026-08-16 14:15:00"),
                (3, "Acme North", 3100.25, "2026-08-16 16:10:00"),
                (4, "Cedar Labs", 725.75, "2026-08-18 08:45:00"),
            ],
        )
    finally:
        connection.close()

    _write(
        project / "wren_project.yml",
        """schema_version: 5
name: dsh_mcp_e2e
version: "1.0"
catalog: wren
schema: public
data_source: duckdb
profile: dsh-mcp-e2e
""",
    )
    _write(project / "relationships.yml", "relationships: []\n")
    _write(
        project / "models" / "orders" / "metadata.yml",
        """name: orders
properties:
  description: One row per customer order, with revenue recorded in USD.
table_reference:
  catalog: dsh_mcp_e2e
  schema: main
  table: orders
columns:
  - name: id
    type: BIGINT
    not_null: true
    is_primary_key: true
  - name: customer_name
    type: VARCHAR
    not_null: true
  - name: amount
    type: DECIMAL
    not_null: true
  - name: ordered_at
    type: TIMESTAMP
    not_null: true
primary_key: id
""",
    )
    _write(
        project / "knowledge" / "rules" / "business-rules.md",
        """# Business rules

- Revenue is the sum of `orders.amount`.
- Order amounts are recorded in USD.
""",
    )
    _write(
        project / "knowledge" / "sql" / "daily-revenue.md",
        """---
nl: What is daily revenue?
sql: |
  SELECT DATE_TRUNC('day', ordered_at) AS order_day, SUM(amount) AS revenue
  FROM orders
  GROUP BY 1
  ORDER BY 1
source: seed
tags:
  - revenue
---
""",
    )
    profile = root / "profile.yml"
    normalized_data = data.resolve().as_posix()
    _write(
        profile,
        f"datasource: duckdb\nurl: {normalized_data}\nformat: duckdb\n",
    )
    return project, wren_home, profile


async def _verify(root: Path) -> dict[str, Any]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise AcceptanceFailure(
            "MCP client is unavailable; install python/sidecar with the mcp extra"
        ) from exc

    wren = _wren_command()
    project, wren_home, profile = _prepare(root)
    env = dict(os.environ)
    env["WREN_HOME"] = str(wren_home)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    _run(
        [str(wren), "profile", "add", "dsh-mcp-e2e", "--from-file", str(profile), "--activate"],
        cwd=root,
        env=env,
    )
    _run(
        [str(wren), "context", "build", "--path", str(project)],
        cwd=root,
        env=env,
    )

    params = StdioServerParameters(
        command=str(wren),
        args=[
            "serve",
            "mcp",
            "--project",
            str(project),
            "--profile",
            "dsh-mcp-e2e",
            "--quiet",
        ],
        cwd=str(project),
        env=env,
    )

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            required = {"get_context", "list_models", "dry_plan", "dry_run", "run_sql"}
            missing = required - tool_names
            if missing:
                raise AcceptanceFailure(f"native MCP tools missing: {sorted(missing)}")
            if "store_query" in tool_names:
                raise AcceptanceFailure("write tool was enabled without --allow-write")

            context = _tool_payload(
                await session.call_tool(
                    "get_context", {"question": "What is daily order revenue?"}
                )
            )
            if "orders" not in json.dumps(context).lower():
                raise AcceptanceFailure("semantic context did not include orders")

            sql = (
                "SELECT DATE_TRUNC('day', ordered_at) AS order_day, "
                'SUM(amount) AS revenue FROM "orders" GROUP BY 1 ORDER BY 1'
            )
            planned = _tool_payload(await session.call_tool("dry_plan", {"sql": sql}))
            if "orders" not in json.dumps(planned).lower():
                raise AcceptanceFailure("dry plan did not resolve the orders model")
            if _tool_payload(await session.call_tool("dry_run", {"sql": sql})) != {"ok": True}:
                raise AcceptanceFailure("native MCP dry run did not pass")

            query = _tool_payload(
                await session.call_tool("run_sql", {"sql": sql, "limit": 10})
            )
            if query.get("columns") != ["order_day", "revenue"]:
                raise AcceptanceFailure("native MCP returned unexpected columns")
            if query.get("row_count") != 3 or query.get("truncated") is not False:
                raise AcceptanceFailure("native MCP returned unexpected row metadata")
            revenues = [round(float(row["revenue"]), 2) for row in query.get("rows", [])]
            if revenues != [1280.50, 3960.25, 725.75]:
                raise AcceptanceFailure(f"native MCP returned unexpected revenues: {revenues}")

            resources = await session.list_resources()
            templates = await session.list_resource_templates()
            prompts = await session.list_prompts()
            prompt_names = [prompt.name for prompt in prompts.prompts]
            if "wren_workflow" not in prompt_names:
                raise AcceptanceFailure("native MCP workflow prompt is unavailable")

            native_result = {
                "status": "passed",
                "server": initialized.serverInfo.name,
                "toolCount": len(tool_names),
                "requiredTools": sorted(required),
                "resourceCount": len(resources.resources),
                "resourceTemplateCount": len(templates.resourceTemplates),
                "promptNames": prompt_names,
                "query": query,
            }

    semantic_only_params = StdioServerParameters(
        command=str(wren),
        args=[
            "serve",
            "mcp",
            "--project",
            str(project),
            "--no-connect",
            "--quiet",
        ],
        cwd=str(project),
        env=env,
    )
    async with stdio_client(semantic_only_params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            semantic_tool_names = {tool.name for tool in tools.tools}
            required_semantic = {"get_context", "list_models", "dry_plan"}
            forbidden_execution = {"run_sql", "dry_run", "query_cube", "store_query"}
            if not required_semantic.issubset(semantic_tool_names):
                raise AcceptanceFailure("native --no-connect omitted semantic tools")
            exposed = forbidden_execution & semantic_tool_names
            if exposed:
                raise AcceptanceFailure(
                    f"native --no-connect exposed execution tools: {sorted(exposed)}"
                )
            semantic_context = _tool_payload(
                await session.call_tool(
                    "get_context", {"question": "What is daily order revenue?"}
                )
            )
            if "orders" not in json.dumps(semantic_context).lower():
                raise AcceptanceFailure("native --no-connect context omitted orders")
            native_result["semanticOnly"] = {
                "requiredTools": sorted(required_semantic),
                "executionToolsAbsent": sorted(forbidden_execution),
            }

    repository = Path(__file__).resolve().parents[1]
    governed_env = dict(env)
    sidecar_root = repository / "python" / "sidecar"
    existing_pythonpath = governed_env.get("PYTHONPATH", "")
    governed_env["PYTHONPATH"] = (
        str(sidecar_root)
        if not existing_pythonpath
        else str(sidecar_root) + os.pathsep + existing_pythonpath
    )
    governed_params = StdioServerParameters(
        command=sys.executable,
        args=[
            str(repository / "scripts" / "fixtures" / "governed-mcp-server.py"),
            "--project",
            str(project),
        ],
        cwd=str(repository),
        env=governed_env,
    )
    async with stdio_client(governed_params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            if initialized.serverInfo.name != "dsh-governed-query":
                raise AcceptanceFailure("governed MCP returned an unexpected identity")
            tools = await session.list_tools()
            if [tool.name for tool in tools.tools] != ["dsh_governed_query"]:
                raise AcceptanceFailure("governed MCP exposed an unexpected tool surface")
            properties = tools.tools[0].inputSchema.get("properties", {})
            if "project_dir" in properties or "database_dsn_env" in properties:
                raise AcceptanceFailure("governed MCP exposed operator policy as tool input")

            governed_query = await session.call_tool(
                "dsh_governed_query",
                {
                    "question": "List order identifiers",
                    "semantic_sql": "SELECT orders.id FROM orders ORDER BY orders.id",
                    "chart_intent": "table",
                    "max_rows": 2,
                    "preview_rows": 1,
                },
            )
            if getattr(governed_query, "isError", False):
                raise AcceptanceFailure("governed MCP query returned a tool error")
            governed_payload = _tool_payload(governed_query)
            stats = governed_payload.get("stats", {})
            if (
                governed_payload.get("schemaVersion") != 1
                or governed_payload.get("status") != "success"
                or stats.get("returnedRows") != 2
                or stats.get("truncated") is not True
                or len(governed_payload.get("previewRows", [])) != 1
            ):
                raise AcceptanceFailure("governed MCP did not preserve DSH bounds")

            denied = await session.call_tool(
                "dsh_governed_query",
                {
                    "question": "policy probe",
                    "semantic_sql": "SELECT pg_sleep(1) AS waited",
                },
            )
            diagnostic = " ".join(
                str(getattr(content, "text", ""))
                for content in getattr(denied, "content", [])
            )
            if not getattr(denied, "isError", False) or "POLICY_DENIED" not in diagnostic:
                raise AcceptanceFailure("governed MCP did not enforce DSH SQL policy")

    native_result["governedGateway"] = {
        "server": "dsh-governed-query",
        "tools": ["dsh_governed_query"],
        "operatorPolicyPinned": True,
        "bounds": "passed",
        "policyDenial": "passed",
    }
    return native_result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run credential-free native and governed MCP acceptance"
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="retain the isolated fixture directory for debugging",
    )
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="dsh-wren-mcp-e2e-"))
    try:
        result = asyncio.run(_verify(root))
        if args.keep_temp:
            result["tempDirectory"] = str(root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AcceptanceFailure, OSError, subprocess.SubprocessError) as exc:
        print(f"WREN_MCP_E2E_FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_temp:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
