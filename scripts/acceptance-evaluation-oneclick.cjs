/* One real browser click drives the installed external CLI worker. No mocks. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {createRequire}=require('node:module');
const [fixtureFile,moduleDirectory]=process.argv.slice(2);
const existingRunId=process.argv[4]; // UI-only verification never invokes the model again.
const fixture=JSON.parse(fs.readFileSync(fixtureFile)),token=fs.readFileSync(fixture.tokenPath,'utf8').trim();
const {chromium}=createRequire(path.resolve(moduleDirectory,'package.json'))('playwright');
(async()=>{
 const request=async(route,body)=>{
  const response=await fetch(fixture.url+'/api/v1/evaluations'+route,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  assert(response.ok,'fixture API status '+response.status);return response.json();
 };
 let suite,profile;
 if(!existingRunId){
  suite=await request('/suites',{name:'Real one-click installed worker',testDatasetId:fixture.datasetId,cases:[{id:'regions',question:'Execute SELECT COUNT(*) AS n FROM regions with SemaRail tools and return the actual result.',role:'browser_analyst',expectedBehavior:'answer',referenceSql:'SELECT COUNT(*) AS n FROM regions',expectedResult:{columns:['n'],rows:[[6]]}}]});
  profile=await request('/profiles',{name:'Real installed CLI worker',model:fixture.model,reasoningEffort:'low',prompt:'Use actual SemaRail MCP tools to answer; never invent a result.',timeoutSeconds:300,rounds:1,concurrency:1,contextVersion:1});
  await request('/suites/'+suite.id+'/publish',{});await request('/profiles/'+profile.id+'/publish',{});
 }
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  await page.goto(fixture.url);await page.getByRole('button',{name:'Unlock Console',exact:true}).click();
  await page.getByLabel('Administrator credential').fill(token);await page.getByRole('button',{name:'Unlock',exact:true}).click();
  await page.getByRole('button',{name:'Evaluation center',exact:true}).click();
  let run;
  if(existingRunId){
   await page.getByRole('button',{name:new RegExp(existingRunId)}).click();run={id:existingRunId};
  }else{
  await page.getByLabel('Published suite',{exact:true}).selectOption(suite.id);
  await page.getByLabel('Published Agent profile',{exact:true}).selectOption(profile.id);
  await page.getByRole('button',{name:'Preview evaluation',exact:true}).click();
  await page.getByText('Review execution plan',{exact:true}).waitFor();
  const created=page.waitForResponse(r=>r.url().endsWith('/api/v1/evaluations/runs')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Run evaluation',exact:true}).click();run=await(await created).json();
  }
  let result;const deadline=Date.now()+420000;
  do{result=await request('/runs/'+run.id);if(result.status==='completed'||result.attempts.every(a=>['completed','timeout','process_error','interrupted','cancelled','unsupported_interaction'].includes(a.status)))break;
   await new Promise(resolve=>setTimeout(resolve,2000));}while(Date.now()<deadline);
  const reportFile=path.join(path.dirname(fixtureFile),existingRunId?'oneclick-followup-report.json':'oneclick-report.json');
  const report={schemaVersion:1,runId:run.id,status:'failed',actualInstalledWorker:true,actualModel:fixture.model,uiOnlyFollowup:!!existingRunId,summary:result.summary,attemptStatuses:result.attempts.map(a=>a.status),datasetScorerBinding:result.environment?.datasetScorerBinding,scorerSubject:result.environment?.scorerSubject};
  fs.writeFileSync(reportFile,JSON.stringify(report,null,2));
  assert.equal(result.summary.passed,1,'real one-click query must score passed');
  assert.equal(result.attempts[0].status,'completed');assert(result.attempts[0].coreTraceIds.length);
  if(!existingRunId){
   assert(result.environment.datasetScorerBindingValid);
   assert.equal(result.environment.datasetScorerBinding.subjectId,result.environment.scorerSubject);
   assert(!Object.values(result.environment.roleSubjects).includes(result.environment.scorerSubject));
  }
  const refreshed=page.waitForResponse(r=>r.url().endsWith('/api/v1/evaluations/runs/'+run.id)&&r.request().method()==='GET');
  await page.getByRole('button',{name:'Refresh run',exact:true}).click();await refreshed;
  await page.getByRole('cell',{name:'completed',exact:true}).waitFor();
  await page.getByRole('button',{name:'Inspect',exact:true}).first().click();
  await page.locator('pre').filter({hasText:'toolCalls'}).waitFor();
  await page.getByRole('button',{name:/^Core diagnostic:/}).first().click();
  await page.getByRole('heading',{name:'Core diagnostic detail',exact:true}).waitFor();
  await page.getByRole('heading',{name:/^regions/}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(path.dirname(fixtureFile),'oneclick-actual.png')});
  report.status='passed';fs.writeFileSync(reportFile,JSON.stringify(report,null,2));
  console.log(JSON.stringify({status:'passed',runId:run.id,summary:result.summary}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message.replaceAll(token,'[redacted]'));process.exitCode=1;});
