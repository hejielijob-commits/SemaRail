/* Actual local Console browser checks. Requires a scoped test fixture service
 * JSON {url,tokenPath}, and Playwright installed in the supplied module folder.
 * Credentials remain in memory; screenshots exclude the unlock dialog.
 * No model is launched. Reviews are explicitly marked pending UI test records. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const [fixtureFile, moduleDirectory, outputDirectory] = process.argv.slice(2);
const {chromium} = createRequire(path.resolve(moduleDirectory, 'package.json'))('playwright');
const fixture = JSON.parse(fs.readFileSync(fixtureFile));
const token = fs.readFileSync(fixture.tokenPath, 'utf8').trim();
const output = path.resolve(outputDirectory);
fs.mkdirSync(output, {recursive:true});

(async () => {
 const browser = await chromium.launch({channel:'msedge', headless:true});
 const context = await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
 const page = await context.newPage();
 const checks=[];
 const request = async (route) => {
  const response=await fetch(fixture.url+'/api/v1/evaluations'+route,{headers:{Authorization:'Bearer '+token}});
  assert(response.ok);return response.json();
 };
 const unlock = async () => {
  await page.getByRole('button',{name:'Unlock Console',exact:true}).click();
  await page.getByLabel('Administrator credential').fill(token);
  await page.getByRole('button',{name:'Unlock',exact:true}).click();
  await page.getByRole('button',{name:'Evaluation center',exact:true}).click();
  await page.getByRole('heading',{name:'Evaluation center',exact:true}).waitFor();
 };
 try {
  await page.goto(fixture.url);await unlock();
  const history=await request('/runs');assert(history.items.length);
  const run=history.items.find(r=>r.status==='completed'&&r.summary?.total===4&&r.environment?.roleSubjects?.employee);assert(run);
  assert(run.environment.scorerSubject);
  await page.getByLabel('Dataset for reference scoring',{exact:true}).selectOption(run.environment.datasetId);
  await page.getByLabel('Managed scorer subject ID',{exact:true}).fill(run.environment.scorerSubject);
  const registered=page.waitForResponse(r=>r.url().endsWith('/datasets/'+run.environment.datasetId+'/scorer')&&r.request().method()==='PUT');
  await page.getByRole('button',{name:'Register scorer identity',exact:true}).click();
  assert((await registered).ok());
  await page.getByText('Scorer identity registered',{exact:true}).waitFor();
  const datasets=await request('/datasets');
  assert(datasets.items.some(d=>d.id===run.environment.datasetId&&d.scorerSubject===run.environment.scorerSubject&&d.scorerBindingValid));
  checks.push('dataset_scorer_registration_through_real_page');
  await page.getByRole('button',{name:new RegExp(run.id)}).click();
  await page.getByText('Frozen environment',{exact:true}).waitFor();
  let evidenceResponse=page.waitForResponse(r=>r.url().includes('/evidence')&&r.request().method()==='GET');
  await page.getByRole('button',{name:'Inspect',exact:true}).first().click();
  if((await evidenceResponse).ok()) await page.locator('pre').filter({hasText:'toolCalls'}).waitFor();
  await page.getByLabel('Human review',{exact:true}).selectOption('pending');
  const reason='Browser acceptance only; business answer is not adjudicated.';
  await page.getByLabel('Review reason',{exact:true}).fill(reason);
  await page.getByRole('button',{name:'Save review',exact:true}).click();
  await page.getByText(reason,{exact:true}).first().waitFor();
  checks.push('persisted_real_run_and_independent_pending_review');
  await page.screenshot({path:path.join(output,'evaluation-desktop.png'),fullPage:true});
  await page.reload();await unlock();
  await page.getByRole('button',{name:new RegExp(run.id)}).click();
  evidenceResponse=page.waitForResponse(r=>r.url().includes('/evidence')&&r.request().method()==='GET');
  await page.getByRole('button',{name:'Inspect',exact:true}).first().click();
  if((await evidenceResponse).ok()) await page.locator('pre').filter({hasText:'toolCalls'}).waitFor();
  await page.getByText(reason,{exact:true}).first().waitFor();
  checks.push('reload_reauth_and_review_reconstruction');
  await page.setViewportSize({width:390,height:844});
  if(await page.locator('.sidebar.sidebar-open').count()) await page.locator('.sidebar-close').click();
  await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right <= 0.5);
  await page.getByRole('heading',{name:'Evaluation center',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(output,'evaluation-mobile.png')});
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole('button',{name:'Agent profiles',exact:true}).click();
  await page.getByText('Import Agent profile',{exact:true}).click();
  const name='Browser profile '+Date.now();
  const bundle={schemaVersion:1,profile:{name,model:'gpt-6-luna',prompt:'UI test fixture; no model is started.',reasoningEffort:'low',contextVersion:1,rounds:1,concurrency:2,timeoutSeconds:300}};
  await page.getByLabel('Profile JSON',{exact:true}).fill(JSON.stringify(bundle));
  await page.getByRole('button',{name:'Import profile as draft',exact:true}).click();
  await page.getByRole('button',{name:new RegExp(name)}).click();
  assert.equal(await page.getByLabel('Profile name',{exact:true}).inputValue(),name);
  const profiles=await request('/profiles');const imported=profiles.items.find(p=>p.name===name);assert(imported);assert.equal(imported.status,'draft');
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('button',{name:'Export Agent profile',exact:true}).click();
  const download=await downloadPromise;const file=path.join(output,'profile-export.json');await download.saveAs(file);
  const exported=JSON.parse(fs.readFileSync(file));assert.equal(exported.schemaVersion,1);assert.equal(exported.profile.name,name);
  checks.push('profile_import_draft_and_versioned_download');
  // This section uses a controlled worker, as distinct from paid CLI acceptance.
  // Browser actions hit the real HTTP API and durable store throughout.
  const bootstrap=fs.readFileSync(path.join(path.dirname(fixture.tokenPath),'bootstrap.token'),'utf8').trim();
  const api=async(route,body,credential=token)=>{
   const res=await fetch(fixture.url+route,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+credential,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
   if(!res.ok) throw new Error('Fixture API status '+res.status+' for '+route);
   return res.json();
  };
  const frozen=await request('/runs/'+run.id), workerId='browser-'+Date.now();
  const account=await api('/api/v1/access/service-accounts',{name:'Controlled browser test worker'},bootstrap);
  const policy=await api('/api/v1/access/policies',{name:'Controlled browser worker',document:{schemaVersion:1,projects:['semarail_hr_enterprise'],tools:['eval:worker']}},bootstrap);
  await api('/api/v1/access/policy-bindings',{subjectId:account.id,policyId:policy.id},bootstrap);
  const workerKey=(await api('/api/v1/access/service-accounts/'+account.id+'/keys',{label:'UI test worker'},bootstrap)).apiKey;
  const datasetId=frozen.environment.datasetId,fingerprint=frozen.environment.datasetFingerprint;
  const worker=(route,body)=>api('/api/v1/evaluations'+route,body,workerKey);
  await worker('/workers/register',{id:workerId,version:'controlled-browser-fixture-v1',cliVersion:'no-model-fixture',roles:['employee'],capabilities:{datasetFingerprints:{[datasetId]:fingerprint},roleSubjects:{employee:frozen.environment.roleSubjects.employee}}});
  await page.getByRole('button',{name:'Publish saved profile',exact:true}).click();
  await page.getByRole('button',{name:'Test suites',exact:true}).click();
  await page.getByText('Import suite',{exact:true}).click();
  const suiteName='Controlled UI suite '+Date.now();
  const suiteBundle={schemaVersion:1,suite:{name:suiteName,testDatasetId:datasetId,cases:[{id:'browser-count',question:'Controlled browser fixture; no model invocation.',role:'employee',expectedBehavior:'answer',expectedResult:{columns:['n'],rows:[[2]]}}]}};
  await page.getByLabel('Suite JSON',{exact:true}).fill(JSON.stringify(suiteBundle));
  await page.getByRole('button',{name:'Import as draft',exact:true}).click();
  await page.getByRole('button',{name:new RegExp(suiteName)}).click();
  await page.getByRole('button',{name:'Publish saved suite',exact:true}).click();
  await page.getByText('Saving changes creates a new draft revision.',{exact:false}).waitFor();
  let all=await request('/suites');const published=all.items.find(s=>s.name===suiteName);assert.equal(published.status,'published');
  await page.getByRole('button',{name:'Copy suite',exact:true}).click();
  const copied=page.getByLabel('Suite name',{exact:false});await copied.fill(suiteName+' copy');
  await page.getByRole('button',{name:'Save suite draft',exact:true}).click();
  await page.getByRole('button',{name:new RegExp(suiteName+' copy')}).click();
  assert.equal((await request('/suites/'+published.id)).revision,1);
  checks.push('suite_import_publish_copy_keeps_original_revision');
  await page.getByRole('button',{name:'Runs',exact:true}).click();
  await page.getByLabel('Published suite',{exact:true}).selectOption(published.id);
  await page.getByLabel('Published Agent profile',{exact:true}).selectOption(imported.id);
  const start=async()=>{
   await worker('/workers/'+workerId+'/heartbeat',{});
   await page.getByRole('button',{name:'Preview evaluation',exact:true}).click();
   await page.getByText('Review execution plan',{exact:true}).waitFor();
   const response=page.waitForResponse(r=>r.url().endsWith('/api/v1/evaluations/runs')&&r.request().method()==='POST');
   await page.getByRole('button',{name:'Run evaluation',exact:true}).click();
   return (await response).json();
  };
  const finish=async(created,value)=>{
   const job=(await worker('/workers/'+workerId+'/claim',{datasetFingerprints:{[datasetId]:fingerprint}})).job;
   assert.equal(job.runId,created.id);
   const identity={workerId,leaseToken:job.leaseToken};
   await worker('/attempts/'+job.attemptId+'/heartbeat',{...identity,started:true,datasetFingerprint:fingerprint});
   const result={columns:['n'],rows:[[value]],complete:true,truncated:false};
   await worker('/attempts/'+job.attemptId+'/complete',{...identity,status:'completed',evidence:{firstResult:result,finalResult:result,finalAnswer:'Controlled fixture output',toolCalls:[],durationMs:1}});
   await page.getByRole('button',{name:'Refresh run',exact:true}).click();
  };
  const baseline=await start();await finish(baseline,2);
  await page.getByRole('button',{name:'Set baseline',exact:true}).click();
  await page.getByText('Baseline saved',{exact:true}).waitFor();
  const candidate=await start();await finish(candidate,3);
  await page.getByRole('button',{name:'Baseline comparison',exact:true}).click();
  await page.getByLabel('Baseline run',{exact:true}).selectOption(baseline.id);
  await page.getByLabel('Candidate run',{exact:true}).selectOption(candidate.id);
  await page.getByRole('button',{name:'Compare runs',exact:true}).click();
  await page.getByRole('heading',{name:'Strict comparison',exact:true}).waitFor();
  await page.getByRole('cell',{name:'regression',exact:true}).waitFor();
  await page.screenshot({path:path.join(output,'controlled-comparison.png'),fullPage:true});
  checks.push('browser_one_click_and_strict_comparison_with_controlled_worker');
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({schemaVersion:1,status:'passed',checks,runId:run.id},null,2));
  console.log(JSON.stringify({status:'passed',checks,output}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error.message.replaceAll(token,'[redacted]'));process.exitCode=1;});
