"""Launch the installed runner with private host credentials for one UI-run check.

Uses the existing isolated HR browser fixture and the owned synthetic PostgreSQL
cluster. No credentials enter the browser except its existing scoped admin key.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
HIDDEN=getattr(subprocess,'CREATE_NO_WINDOW',0)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--fixture',type=Path,required=True)
    parser.add_argument('--worker',type=Path,required=True)
    parser.add_argument('--modules',type=Path,required=True)
    parser.add_argument('--model',required=True)
    args=parser.parse_args()
    source=json.loads(args.fixture.read_text())
    endpoint=urlsplit(source['url'])
    if endpoint.hostname not in {'127.0.0.1','localhost','::1'} or endpoint.scheme not in {'http','https'} or endpoint.username:
        raise RuntimeError('acceptance_requires_loopback_fixture')
    token_path=Path(source['tokenPath'])
    bootstrap=(token_path.parent/'bootstrap.token').read_text().strip()
    directory=ROOT/'.benchmark-data/evaluation-oneclick'/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True,exist_ok=False)
    def admin(route,body=None,method=None):
        req=Request(source['url']+route,data=None if body is None else json.dumps(body).encode(),method=method,headers={'Authorization':'Bearer '+bootstrap,'Content-Type':'application/json'})
        return json.load(urlopen(req,timeout=10))
    policy=next(p for p in admin('/api/v1/access/policies')['items'] if p['name']=='BTV hr_director')
    def identity(name,policy_id):
        account=admin('/api/v1/access/service-accounts',{'name':name})
        admin('/api/v1/access/policy-bindings',{'subjectId':account['id'],'policyId':policy_id})
        return admin('/api/v1/access/service-accounts/'+account['id']+'/keys',{'label':'one-click acceptance'})['apiKey']
    env=os.environ.copy()
    env['SEMARAIL_ONECLICK_ROLE']=identity('One-click read-only analyst',policy['id'])
    env['SEMARAIL_ONECLICK_SCORER']=identity('One-click independent scorer',policy['id'])
    worker_policy=admin('/api/v1/access/policies',{'name':'One-click worker','document':{'schemaVersion':1,'projects':['semarail_hr_enterprise'],'tools':['eval:worker']}})
    env['SEMARAIL_ONECLICK_WORKER']=identity('One-click installed worker',worker_policy['id'])
    sys.path.insert(0,str(ROOT/'benchmarks/business-trust-v1'))
    import native_postgres as native
    native.owned()
    from psycopg.conninfo import make_conninfo
    env['SEMARAIL_ONECLICK_DSN']=make_conninfo(host='127.0.0.1',port=native.PORT,dbname=native.DATABASE,user='hr_benchmark_reader',password=(native.CLUSTER/'.reader-password').read_text().strip())
    dataset=admin('/api/v1/evaluations/datasets')['items'][0]['id']
    scorer_request=Request(source['url']+'/api/v1/auth/me',headers={'Authorization':'Bearer '+env['SEMARAIL_ONECLICK_SCORER']})
    scorer_subject=json.load(urlopen(scorer_request,timeout=10))['subject']['id']
    admin('/api/v1/evaluations/datasets/'+dataset+'/scorer',{'subjectId':scorer_subject},method='PUT')
    worker_id='oneclick-real-'+directory.name
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));mcp_port=sock.getsockname()[1]
    cfg=directory/'runner.toml'
    cfg.write_text('schema_version=1\nworker_id='+json.dumps(worker_id)+'\ncore_url='+json.dumps(source['url'])+'\nmcp_url="http://127.0.0.1:'+str(mcp_port)+'/mcp"\nworker_token_env="SEMARAIL_ONECLICK_WORKER"\nscorer_token_env="SEMARAIL_ONECLICK_SCORER"\ncodex='+json.dumps(shutil.which('codex'))+'\n[roles]\nbrowser_analyst="SEMARAIL_ONECLICK_ROLE"\n[datasets.'+dataset+']\ndatabase_url_env="SEMARAIL_ONECLICK_DSN"\nschema="hr"\ntables='+json.dumps(list(native.TABLES))+'\n',encoding='utf-8')
    fixture_project=token_path.parents[1]
    env.update(SEMARAIL_API_TOKEN=bootstrap,PYTHONPATH=os.pathsep.join([str(ROOT/'apps/semantic-console'),str(ROOT/'python/sidecar')]),SEMARAIL_EMBEDDING_PROVIDER='disabled')
    processes=[]
    def launch(command,name):
        with (directory/name).open('wb') as log:
            process=subprocess.Popen(command,env=env,stdout=log,stderr=log,stdin=subprocess.DEVNULL,creationflags=HIDDEN)
        processes.append(process);return process
    try:
        launch([sys.executable,'-m','server.remote_mcp','--project',str(fixture_project/'project'),'--state-dir',str(fixture_project/'state'),'--port',str(mcp_port)],'mcp.log')
        deadline=time.monotonic()+30
        while True:
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1',mcp_port))==0:break
            if time.monotonic()>deadline:raise RuntimeError('oneclick_mcp_start_timeout')
            time.sleep(.2)
        check=subprocess.run([str(args.worker.resolve()),'check','--config',str(cfg)],env=env,capture_output=True,timeout=120,creationflags=HIDDEN)
        if check.returncode:raise RuntimeError('installed_worker_preflight_failed')
        launch([str(args.worker.resolve()),'run','--config',str(cfg)],'worker.log')
        deadline=time.monotonic()+120
        while not any(w['id']==worker_id and w['online'] for w in admin('/api/v1/evaluations/workers')['items']):
            if time.monotonic()>deadline:raise RuntimeError('installed_worker_registration_timeout')
            time.sleep(.5)
        # Earlier controlled browser workers exit without a daemon. Let their
        # advertised leases expire before starting the independent reference run.
        deadline=time.monotonic()+130
        while any(w['id']!=worker_id and w['online'] for w in admin('/api/v1/evaluations/workers')['items']):
            if time.monotonic()>deadline:raise RuntimeError('prior_fixture_worker_still_online')
            time.sleep(1)
        browser=directory/'browser.json';browser.write_text(json.dumps({**source,'datasetId':dataset,'model':args.model}))
        result=subprocess.run([shutil.which('node'),str(ROOT/'scripts/acceptance-evaluation-oneclick.cjs'),str(browser),str(args.modules.resolve())],capture_output=True,timeout=480,creationflags=HIDDEN)
        if result.returncode:
            diagnostic=result.stderr.decode(errors='replace')[-4000:]
            for secret in [bootstrap,token_path.read_text().strip(),*[v for k,v in env.items() if k.startswith('SEMARAIL_ONECLICK_')]]:
                diagnostic=diagnostic.replace(secret,'[redacted]')
            (directory/'browser-error.txt').write_text(diagnostic,encoding='utf-8')
        print(json.dumps({'directory':str(directory),'exitCode':result.returncode}))
        if result.returncode:raise RuntimeError('oneclick_browser_check_failed')
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=HIDDEN,timeout=30)
                else:process.terminate()
                process.wait(timeout=30)

if __name__=='__main__':main()
