"""Verify evaluation storage on an owned PostgreSQL 17 scratch database.

Uses only the existing synthetic HR cluster's private connection helper. Creates
and drops one randomly named control database; never changes the HR dataset.
Exercises real control-plane APIs with a controlled runner, not a live model.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote
import uuid

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/semantic-console'))
from server.access_control import AccessControlStore
from server.diagnostics import DiagnosticError, DiagnosticStore
from server.evaluations import EvaluationStore
from server.app import create_app
from server.models import DatasourceRecord
from server.project import ProjectStore
from server.runtime_rpc import RuntimeRpcGateway
from server.service import SemanticConsoleService
from server.tests.test_runtime_rpc import FakeValidator, RecordingDispatcher


def verify_api(database_url: str, directory: Path, now: list[datetime], database_name: str) -> list[str]:
    """Run the actual EvaluationApi against PostgreSQL, without model calls."""
    project_dir = directory / 'api-project'
    project_dir.mkdir()
    (project_dir / 'wren_project.yml').write_text('schema_version: 5\nname: pg-evaluation-api\ndata_source: postgres\n', encoding='utf-8')
    state_dir = directory / 'api-state'

    def application():
        access = AccessControlStore.from_config(directory / 'must-not-exist.sqlite3', database_url=database_url, clock=lambda: now[0])
        assert access.path is None, 'PostgreSQL control store silently fell back to SQLite'
        assert type(access).__name__ == 'PostgreSQLAccessControlStore'
        with access._connect() as connection:
            assert connection.execute('SELECT current_database() AS name').fetchone()['name'] == database_name
        project = ProjectStore(project_dir, state_dir=state_dir, validator=FakeValidator())
        if not project.active_datasource_identifier():
            datasource = DatasourceRecord('pg-fixture', 'Synthetic test fixture', 'postgres', {'database': 'controlled-runner-fixture'})
            project.datasource_records()[datasource.id] = datasource
            project.active_datasource_id = datasource.id
            project.save_datasources()
        gateway = RuntimeRpcGateway(project, RecordingDispatcher(), access_control=access)
        app = create_app(SemanticConsoleService(project), runtime_rpc=gateway)
        assert app.evaluation_api.store is not None
        assert app.evaluation_api.store.access is access
        assert not (directory / 'must-not-exist.sqlite3').exists()
        return app

    app = application()
    access = app.runtime_rpc.access_control
    identities = {}
    for name, scopes in (('administrator', ['console:admin']), ('worker', ['eval:worker']), ('employee', ['query:execute', 'semantic:read']), ('scorer', ['query:execute', 'semantic:read'])):
        subject = access.create_service_account('PostgreSQL API ' + name)
        document = {'schemaVersion': 1, 'projects': ['pg-evaluation-api'], 'tools': scopes}
        if name in {'employee', 'scorer'}:
            document['datasourceId'] = app.runtime_rpc.project.active_datasource_identifier()
        policy = access.create_policy(name, document)
        access.bind_policy(subject.id, policy['id'])
        identities[name] = (subject.id, 'Bearer ' + access.issue_api_key(subject.id)['apiKey'])
    assert len({subject for subject, _ in identities.values()}) == 4

    def call(method: str, path: str, body=None, *, actor='administrator', expected=200):
        status, result = app.request(method, '/api/v1/evaluations/' + path, body, authorization=identities[actor][1])
        assert status == expected, {'operation': method + ' ' + path, 'expected': expected, 'status': status, 'code': result.get('code')}
        return result

    scope = (access.subject(identities['administrator'][0]).organization_id, 'pg-evaluation-api')
    app.regression_api.store.save_dataset(scope, {'id': 'api-fixture', 'version': 'controlled-v1', 'datasourceId': app.runtime_rpc.project.active_datasource_identifier()})
    call('PUT', 'datasets/api-fixture/scorer', {'subjectId': identities['scorer'][0]})
    call('PUT', 'settings', {'enabled': True, 'testDataOnly': True})
    case = {'id': 'count', 'question': 'Count the synthetic fixture', 'role': 'employee', 'expectedBehavior': 'answer', 'referenceSql': 'SELECT 1 AS n', 'expectedResult': {'columns': ['n'], 'rows': [[1]]}}
    suite = call('POST', 'suites', {'name': 'PostgreSQL API acceptance', 'testDatasetId': 'api-fixture', 'cases': [case]}, expected=201)
    profile = call('POST', 'profiles', {'name': 'Controlled runner', 'model': 'simulated-no-model-invoked', 'prompt': 'Use the fixture MCP tools', 'rounds': 1, 'concurrency': 2}, expected=201)
    call('POST', 'suites/' + suite['id'] + '/publish', {})
    call('POST', 'profiles/' + profile['id'] + '/publish', {})
    for kind, record, envelope in (('suites', suite, 'suite'), ('profiles', profile, 'profile')):
        exported = call('GET', kind + '/' + record['id'] + '/export')
        imported = call('POST', kind + '/import', exported, expected=201)
        assert imported['status'] == 'draft'
        assert imported['name'] == exported[envelope]['name']
        call('POST', kind + '/import', {**exported, 'schemaVersion': 99}, expected=400)
    registration = {'id': 'controlled-worker', 'version': 'acceptance-v1', 'cliVersion': 'simulated-no-cli-invoked', 'roles': ['employee'], 'capabilities': {'datasetFingerprints': {'api-fixture': '0' * 64}, 'roleSubjects': {'employee': identities['employee'][0]}, 'scorerSubject': identities['scorer'][0]}}
    call('POST', 'workers/register', registration, actor='worker')
    preview = call('GET', 'preflight?suiteId=' + suite['id'] + '&profileId=' + profile['id'])
    assert preview['ready'] and preview['total'] == 1

    def execute(value: int):
        run = call('POST', 'runs', {'suiteId': suite['id'], 'profileId': profile['id']}, expected=202)
        status, _ = app.request('POST', '/api/project/publish', {}, authorization=identities['administrator'][1])
        assert status == 409
        job = call('POST', 'workers/controlled-worker/claim', {'datasetFingerprints': {'api-fixture': '0' * 64}}, actor='worker')['job']
        assert job['runId'] == run['id'] and job['environment']['scorerSubject'] == identities['scorer'][0]
        lease = {'workerId': registration['id'], 'leaseToken': job['leaseToken']}
        heartbeat = call('POST', 'attempts/' + job['attemptId'] + '/heartbeat', {**lease, 'started': True, 'datasetFingerprint': '0' * 64}, actor='worker')
        assert not heartbeat['cancelRequested']
        call('POST', 'attempts/' + job['attemptId'] + '/events', {**lease, 'events': [{'type': 'controlled-tool-observation'}]}, actor='worker')
        body = {**lease, 'status': 'completed', 'evidence': {'finalAnswer': str(value) + ' fixture row', 'firstResult': {'columns': ['n'], 'rows': [[value]], 'complete': True, 'truncated': False}, 'finalResult': {'columns': ['n'], 'rows': [[value]], 'complete': True, 'truncated': False}}}
        completed = call('POST', 'attempts/' + job['attemptId'] + '/complete', body, actor='worker')
        assert completed['score']['status'] == ('passed' if value == 1 else 'failed')
        assert call('POST', 'attempts/' + job['attemptId'] + '/complete', body, actor='worker') == completed
        call('POST', 'attempts/' + job['attemptId'] + '/review', {'status': 'passed' if value == 1 else 'failed', 'reason': 'Controlled answer review'})
        final = call('GET', 'runs/' + run['id'])
        assert final['status'] == 'completed' and final['summary']['total'] == 1
        assert final['summary']['started'] == 1
        return run, job, body

    baseline, baseline_job, baseline_body = execute(1)
    call('POST', 'runs/' + baseline['id'] + '/baseline', {})
    candidate, _, _ = execute(0)
    comparison = call('GET', 'compare?baselineId=' + baseline['id'] + '&candidateId=' + candidate['id'])
    assert comparison['strict'] and comparison['items'][0]['category'] == 'regression'
    old_app = app
    app = application()
    assert app.runtime_rpc.access_control is not old_app.runtime_rpc.access_control
    assert app.evaluation_api.store is not old_app.evaluation_api.store
    retained = call('GET', 'runs/' + baseline['id'])
    assert retained['baseline'] and retained['summary']['passed'] == 1
    assert retained['attempts'][0]['review']['history'][0]['status'] == 'passed'
    evidence_path = 'attempts/' + baseline_job['attemptId'] + '/evidence'
    evidence = call('GET', evidence_path)
    assert evidence['evidence']['events'] == [{'type': 'controlled-tool-observation'}]
    retried = call('POST', 'attempts/' + baseline_job['attemptId'] + '/complete', baseline_body, actor='worker')
    assert retried['score']['status'] == 'passed' and len(retried['review']['history']) == 1
    call('POST', 'attempts/' + baseline_job['attemptId'] + '/complete', {**baseline_body, 'status': 'timeout'}, actor='worker', expected=409)
    now[0] += timedelta(days=31)
    app.evaluation_api.store.cleanup_expired()
    call('GET', evidence_path, expected=410)
    assert call('GET', 'runs/' + baseline['id'])['summary']['passed'] == 1
    return ['postgresql-control-api-no-sqlite-fallback', 'independent-managed-identities', 'suite-profile-api-publish-import-export', 'api-preflight', 'api-project-lock', 'api-claim-heartbeat-events', 'api-completion-idempotency', 'api-review-history', 'api-baseline-strict-regression', 'api-reopen-same-postgres-db', 'api-idempotency-after-reopen', 'api-evidence-expiry-keeps-scores']


def main() -> None:
    spec = importlib.util.spec_from_file_location('owned_hr_postgres', ROOT / 'benchmarks/business-trust-v1/native_postgres.py')
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    admin = native.connect('postgres')
    native.check_server(admin)
    admin.rollback()
    admin.autocommit = True
    database = 'evaluation_accept_' + uuid.uuid4().hex[:16]
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    try:
        password = (native.CLUSTER / '.owner-password').read_text(encoding='utf-8').strip()
        url = f'postgresql://{native.OWNER}:{quote(password, safe="")}@127.0.0.1:{native.PORT}/{database}'
        now = [datetime.now(UTC)]
        with tempfile.TemporaryDirectory(prefix='semarail-eval-pg-') as directory:
            access = AccessControlStore.from_config(Path(directory) / 'unused.sqlite3', database_url=url, clock=lambda: now[0])
            assert access.path is None
            store = EvaluationStore(DiagnosticStore(access))
            scope = ('evaluation-acceptance', 'hr-isolated-test')
            case = {'id': 'one', 'question': 'Count employees', 'role': 'hr_director', 'expectedBehavior': 'answer',
                    'expectedResult': {'columns': ['n'], 'rows': [[1]]}}
            suite = {'name': 'PostgreSQL acceptance', 'testDatasetId': 'fixture', 'cases': [case]}
            with store.transaction() as connection:
                draft = store.save_asset(connection, scope, 'suites', suite)
                published = store.publish(connection, scope, 'suites', draft['id'])
                revised = store.save_asset(connection, scope, 'suites', {**suite, 'name': 'Edited'}, draft['id'])
                assert revised['revision'] == 2 and published['revision'] == 1
                old = store.get(connection, scope, 'suites_versions', draft['id'] + ':1')
                assert old['name'] == suite['name']
                try:
                    store.get(connection, ('another-org', scope[1]), 'suites', draft['id'])
                except DiagnosticError as error:
                    assert error.status == 404
                else:
                    raise AssertionError('cross-organization record was visible')
                attempt = {'id': 'attempt-one', 'runId': 'run-one', 'caseId': 'one', 'round': 1, 'status': 'running', 'startedAt': store.now()}
                evidence = {'finalAnswer': 'One employee', 'finalResult': {'columns': ['n'], 'rows': [[1]], 'complete': True, 'truncated': False}}
                body = {'status': 'completed', 'evidence': evidence}
                completed = store.complete(connection, scope, attempt, body, case)
                assert completed['score']['status'] == 'passed'
                assert store.complete(connection, scope, attempt, body, case) == completed
            reopened = EvaluationStore(DiagnosticStore(access))
            with reopened.transaction() as connection:
                assert reopened.get(connection, scope, 'attempts', 'attempt-one')['score']['status'] == 'passed'
                assert connection.execute('SELECT body_json FROM evaluation_evidence WHERE attempt_id=?', ('attempt-one',)).fetchone() is not None
            now[0] += timedelta(days=31)
            with reopened.transaction() as connection:
                reopened.recover(connection, scope)
                assert connection.execute('SELECT body_json FROM evaluation_evidence WHERE attempt_id=?', ('attempt-one',)).fetchone() is None
                assert reopened.get(connection, scope, 'attempts', 'attempt-one')['score']['status'] == 'passed'
            api_checks = verify_api(url, Path(directory), now, database)
        with native.connect(native.DATABASE) as hr:
            native.check_server(hr)
            count = hr.execute('SELECT COUNT(*) FROM hr.employees').fetchone()[0]
            assert count == 100000
        print(json.dumps({'status': 'passed', 'serverMajor': 17, 'checks': ['immutable-version', 'scope-isolation', 'terminal-idempotency', 'reopen', 'evidence-expiry', 'retained-score', 'synthetic-hr-readable'] + api_checks, 'liveAgentAcceptance': False, 'runner': 'controlled-no-model-calls'}))
    finally:
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))
        admin.close()


if __name__ == '__main__':
    main()
