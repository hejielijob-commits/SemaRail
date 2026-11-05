/* Read-only rendered inspection of actual CLI runs and caller-owned Core links. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {createRequire}=require('node:module');
const [fixtureDirectory,moduleDirectory,outputDirectory]=process.argv.slice(2);
const {chromium}=createRequire(path.resolve(moduleDirectory,'package.json'))('playwright');
const fixture=path.resolve(fixtureDirectory),output=path.resolve(outputDirectory);
const oneclick=!fs.existsSync(path.join(fixture,'service.json'));
const service=JSON.parse(fs.readFileSync(path.join(fixture,oneclick?'browser.json':'service.json')));
const report=JSON.parse(fs.readFileSync(path.join(fixture,oneclick?'oneclick-report.json':'acceptance-summary.json')));
report.baselineRunId=report.baselineRunId??report.runId;
const token=fs.readFileSync(service.tokenPath??path.join(fixture,'private/ui-admin.token'),'utf8').trim();
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  await page.goto(service.coreUrl??service.url);
  await page.getByRole('button',{name:'Unlock Console',exact:true}).click();
  await page.getByLabel('Administrator credential').fill(token);
  await page.getByRole('button',{name:'Unlock',exact:true}).click();
  await page.getByRole('button',{name:'Evaluation center',exact:true}).click();
  await page.getByRole('button',{name:new RegExp(report.baselineRunId)}).click();
  const row=page.getByRole('row').filter({has:page.getByRole('cell',{name:oneclick?/^regions/:/^query/})});
  await row.getByRole('button',{name:'Inspect',exact:true}).click();
  await page.locator('pre').filter({hasText:'toolCalls'}).waitFor();
  await page.getByRole('button',{name:/^Core diagnostic:/}).first().click();
  await page.getByRole('heading',{name:'Core diagnostic detail',exact:true}).waitFor();
  await page.getByText('Full Agent trace not collected',{exact:true}).waitFor();
  await page.getByRole('heading',{name:'Core diagnostic detail',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(output,'actual-core-diagnostic.png')});
  const checks=['real_cli_query_evidence','owned_core_diagnostic','agent_trace_absence_explicit'];
  if(report.candidateRunId){
   await page.getByRole('button',{name:'Baseline comparison',exact:true}).click();
   await page.getByLabel('Baseline run',{exact:true}).selectOption(report.baselineRunId);
   await page.getByLabel('Candidate run',{exact:true}).selectOption(report.candidateRunId);
   await page.getByRole('button',{name:'Compare runs',exact:true}).click();
   await page.getByRole('heading',{name:'Strict comparison',exact:true}).waitFor();
   const queryRow=page.getByRole('row').filter({has:page.getByRole('cell',{name:'query',exact:true})});
   assert.equal(await queryRow.getByRole('cell').nth(2).innerText(),'regression');
   await page.screenshot({path:path.join(output,'actual-semantic-regression.png'),fullPage:true});
   checks.push('real_semantic_change_strict_regression');
  }
  fs.writeFileSync(path.join(output,'live-browser-report.json'),JSON.stringify({schemaVersion:1,status:'passed',checks,baselineRunId:report.baselineRunId,candidateRunId:report.candidateRunId},null,2));
  console.log(JSON.stringify({status:'passed',checks,output}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message.replaceAll(token,'[redacted]'));process.exitCode=1;});
