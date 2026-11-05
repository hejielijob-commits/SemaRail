"""Independent physical SQL oracle for the fixed synthetic HR actors.

This bypasses Wren planning but applies explicit fixture row scopes to every
physical table reference. It never receives an Agent-generated statement.
Only checked-in canonical SQL is accepted by the coordinator.
"""
from __future__ import annotations

import datetime
import decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from sqlglot import exp, parse_one

TABLES = {'employees','departments','regions','compensation_history','performance_reviews','attendance_monthly'}


def physical_sql(canonical_sql: str, actor: str) -> str:
    root = parse_one(canonical_sql, read='postgres')
    if not isinstance(root, exp.Select):
        raise ValueError('native oracle only supports reviewed SELECT statements')
    for table in list(root.find_all(exp.Table)):
        name = table.name
        if name not in TABLES or table.db:
            raise ValueError('native oracle references an unknown table')
        alias = table.alias_or_name
        qualified = exp.Table(this=exp.to_identifier(name), db=exp.to_identifier('hr'))
        scope = None
        if actor in ('hrbp','compensation_admin'):
            scope = exp.column('region_code').eq(exp.Literal.string('APAC'))
        elif actor == 'employee' and name not in ('regions','departments'):
            scope = exp.column('employee_id').eq(exp.Literal.number(1))
        elif actor == 'department_manager' and name not in ('regions','departments'):
            scope = exp.column('manager_id').eq(exp.Literal.number(627))
        elif actor not in ('employee','department_manager','hr_director'):
            raise ValueError('unknown fixture actor')
        query = exp.select('*').from_(qualified)
        if scope is not None:
            query = query.where(scope)
        table.replace(query.subquery(alias=alias))
    return root.sql(dialect='postgres')


def normalize(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
        return value.isoformat()
    return value


def execute(question: dict[str, Any], *, database_url: str) -> dict[str, Any]:
    if question['oracle']['type'] != 'rows':
        raise ValueError('only row-result cases have an independent physical oracle')
    query = physical_sql(question['canonical']['semanticSql'], question['actor'])
    if not isinstance(database_url, str) or not database_url:
        raise ValueError('benchmark database connection is unavailable')
    with psycopg.connect(database_url, connect_timeout=3, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute("SET LOCAL statement_timeout = '10s'")
            cursor.execute(query)
            rows = cursor.fetchmany(51)
            columns = [c.name for c in cursor.description]
            numeric_columns = [c.name for c in cursor.description if c.type_code in (20,21,23,700,701,1700)]
    if len(rows) > 50:
        raise ValueError('oracle must fit in a complete inline result')
    return {'schemaVersion':1,'status':'success','columns':columns,
            'numericColumns':numeric_columns,
            'previewRows':[{k:normalize(v) for k,v in row.items()} for row in rows],
            'stats':{'returnedRows':len(rows),'truncated':False},'delivery':'inline'}
