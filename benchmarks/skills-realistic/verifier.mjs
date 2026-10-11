import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { stripTypeScriptTypes } from 'node:module';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const benchmark = dirname(fileURLToPath(import.meta.url));
const task = process.env.PLUGINBENCH_TASK_ID;
const tests = [];
const record = (name, action) => {
  try { action(); tests.push({ name, pass: true }); }
  catch (error) { tests.push({ name, pass: false, error: String(error) }); }
};
const files = (root, prefix = '') => readdirSync(resolve(root, prefix), { withFileTypes: true })
  .filter((entry) => !['.git', '.agents'].includes(entry.name))
  .flatMap((entry) => {
    assert.ok(!entry.isSymbolicLink(), `unsupported symlink: ${prefix}/${entry.name}`);
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    return entry.isDirectory() ? files(root, path) : [path];
  }).sort();
const snapshot = (root) => Object.fromEntries(files(root).map((file) => [file,
  createHash('sha256').update(readFileSync(resolve(root, file))).digest('hex')]));
const runNode = (source) => {
  const receipt = `completed-${randomUUID()}`;
  const result = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e',
    `import assert from 'node:assert/strict';\nimport {after as completedAfter} from 'node:test';\n${source}\ncompletedAfter(() => console.log(${JSON.stringify(receipt)}));`],
  { cwd: process.cwd(), encoding: 'utf8', timeout: 15_000 });
  assert.equal(result.error, undefined, String(result.error));
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.ok(result.stdout.includes(receipt), 'behavioral probe did not finish');
};
const commonWorktrees = `
  import { makeWorktree } from ${JSON.stringify(pathToFileURL(resolve(benchmark, 'fixtures/branch-exclusions/test/helpers.ts')).href)};
  const found = (state) => ({status:'found',number:7,state,title:'test',url:'local:7',mergedAt:null,
    closedAt:null,updatedAt:'2025-01-01',isCrossRepository:false});
`;
const featureProbes = {
  'exclusions compose across option, filter and query boundaries': `
    import {queryWorktrees} from './src/query.ts';
    import {applyFilters} from './src/filter.ts';
    import {buildFilters,normalizeDone} from './src/options.ts';
    ${commonWorktrees}
    const main=makeWorktree({isMain:true,path:'/main',branch:'main'});
    const keep=makeWorktree({path:'/keep',branch:'feat/keep',dirty:true,pr:found('MERGED')});
    const skip=makeWorktree({path:'/skip',branch:'feat/wip-a',dirty:true,pr:found('MERGED')});
    const clean=makeWorktree({path:'/clean',branch:'feat/clean',dirty:false,pr:found('MERGED')});
    const open=makeWorktree({path:'/open',branch:'feat/open',dirty:true,pr:found('OPEN')});
    const input=[skip,keep,main,clean,open];
    const opts={filters:{excludeBranch:' feat/wip-* , release/? ',branch:'feat/*',dirty:true,done:true},
      includeMain:false,sort:'path'};
    assert.deepEqual(queryWorktrees({worktrees:input,options:opts}),[keep]);
    const filters=buildFilters({options:normalizeDone(opts.filters),ageBasis:'commit',includeMain:false});
    assert.deepEqual(applyFilters(input,filters),[keep]);
    assert.deepEqual(filters.excludeBranches,['feat/wip-*','release/?']);
    assert.deepEqual(input,[skip,keep,main,clean,open]);
  `,
  'exclusion globs use literal, case-sensitive whole-branch matching': `
    import {queryWorktrees} from './src/query.ts';
    ${commonWorktrees}
    const names=['a.b','axb','feat/x','x/feat/x','feat/x/y','Feat/x','release/a','release/ab'];
    const entries=names.map((branch,i)=>makeWorktree({branch,path:'/p'+i}));
    const result=queryWorktrees({worktrees:entries,options:{filters:{excludeBranch:'a.b,feat/*,release/?'}}});
    assert.deepEqual(result.map(w=>w.branch),['axb','x/feat/x','Feat/x','release/ab']);
    const detached=makeWorktree({branch:null,path:'/detached'});
    const main=makeWorktree({isMain:true,branch:'main',path:'/main'});
    assert.deepEqual(queryWorktrees({worktrees:[detached,main],options:{filters:{excludeBranch:'main'}}}),[detached]);
    assert.deepEqual(queryWorktrees({worktrees:entries,options:{filters:{excludeBranch:'*'}}}),[]);
  `,
  'exclusions intersect age and main-worktree controls without disturbing sorting': `
    import {queryWorktrees} from './src/query.ts';
    ${commonWorktrees}
    const main=makeWorktree({isMain:true,path:'/main',branch:'main',lastCommitAt:'2025-01-01'});
    const old=makeWorktree({path:'/old',branch:'feat/old',lastCommitAt:'2025-01-01'});
    const fresh=makeWorktree({path:'/fresh',branch:'feat/fresh',lastCommitAt:'2025-08-01'});
    const unknown=makeWorktree({path:'/unknown',branch:'feat/unknown',lastCommitAt:null,checkoutAt:null,createdAt:null});
    const wip=makeWorktree({path:'/wip',branch:'wip/old',lastCommitAt:'2025-01-01'});
    const input=[fresh,old,main,unknown,wip];
    assert.deepEqual(queryWorktrees({worktrees:input,options:{includeMain:false,
      filters:{olderThan:'2025-06-01',excludeBranch:'wip/*'}}}),[old]);
    assert.deepEqual(queryWorktrees({worktrees:input,options:{sort:'path',reverse:true,
      filters:{excludeBranch:'wip/*'}}}),[main,unknown,old,fresh]);
    assert.deepEqual(queryWorktrees({worktrees:input,options:{
      filters:{excludeBranch:'main,wip/*'}}}),[fresh,old,unknown]);
  `,
  'empty exclusion tokens are rejected even with no worktrees': `
    import {queryWorktrees} from './src/query.ts';
    import {InvalidFilterError} from './src/filter.ts';
    for(const excludeBranch of ['', '  ', ',', 'foo,', ',foo', 'foo, ,bar']) {
      assert.throws(()=>queryWorktrees({worktrees:[],options:{filters:{excludeBranch}}}),InvalidFilterError);
    }
    assert.deepEqual(queryWorktrees({worktrees:[],options:{filters:{excludeBranch:'foo'}}}),[]);
  `,
};
const deferred = `
  const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});
    return {promise,resolve,reject};};
  const tick=()=>new Promise(resolve=>setImmediate(resolve));
`;
const concurrencyProbes = {
  'invalid concurrency rejects before invoking the mapper including empty input': `
    import {mapLimit} from './src/concurrency.ts';
    for(const limit of [0,-1,1.5,NaN,Infinity,-Infinity]) {
      let calls=0;
      const fn=async()=>{calls++;return 1;};
      await assert.rejects(mapLimit([1,2],limit,fn),RangeError);
      await assert.rejects(mapLimit([],limit,fn),RangeError);
      assert.equal(calls,0);
    }
  `,
  'undefined items and large valid limits retain every input position': `
    import {mapLimit} from './src/concurrency.ts';
    const calls=[];
    assert.deepEqual(await mapLimit([undefined,'x',undefined],20,async(value,index)=>{
      calls.push(index);return value===undefined?'missing:'+index:value;
    }),['missing:0','x','missing:2']);
    assert.deepEqual(calls,[0,1,2]);
    assert.deepEqual(await mapLimit([],1,async()=>{throw new Error('not called');}),[]);
  `,
  'bounded workers preserve order despite deliberately reversed completion': `
    import {mapLimit} from './src/concurrency.ts';
    ${deferred}
    const gates=Array.from({length:5},deferred);
    let active=0,maximum=0;const starts=[];
    const result=mapLimit([0,1,2,3,4],2,async(value,index)=>{
      active++;maximum=Math.max(maximum,active);starts.push(index);
      await gates[index].promise;active--;return value*10;
    });
    await tick();assert.deepEqual(starts,[0,1]);
    gates[1].resolve();await tick();assert.deepEqual(starts,[0,1,2]);
    gates[2].resolve();await tick();assert.deepEqual(starts,[0,1,2,3]);
    gates[0].resolve();await tick();assert.deepEqual(starts,[0,1,2,3,4]);
    gates[4].resolve();gates[3].resolve();
    assert.deepEqual(await result,[0,10,20,30,40]);assert.equal(maximum,2);assert.equal(active,0);
  `,
  'first observed mapper failure stops new work and drains already started work': `
    import {mapLimit} from './src/concurrency.ts';
    ${deferred}
    const gates=Array.from({length:5},deferred);const starts=[];
    const first=new Error('first failure'),second=new Error('second failure');let settled=false;
    const result=mapLimit([0,1,2,3,4],2,async(value,index)=>{
      starts.push(index);await gates[index].promise;return value;
    });
    const observed=result.then(()=>{settled=true;throw new Error('expected rejection');},
      error=>{settled=true;return error;});
    await tick();assert.deepEqual(starts,[0,1]);
    gates[1].reject(first);await tick();assert.equal(settled,false,'must drain started work before rejection');
    assert.deepEqual(starts,[0,1]);gates[0].reject(second);
    assert.equal(await observed,first);await tick();assert.deepEqual(starts,[0,1]);
    const sync=new Error('synchronous throw');
    await assert.rejects(mapLimit([1],1,()=>{throw sync;}),error=>error===sync);
  `,
  'collection and catalog preserve PR, metadata and caller filters under concurrency': `
    import {loadCatalog} from './src/catalog.ts';
    import {collect} from './src/collect.ts';
    ${commonWorktrees}
    ${deferred}
    const entries=[makeWorktree({path:'/main',branch:'main'}),makeWorktree({path:'/feature',branch:'feat/a'}),
      makeWorktree({path:'/closed',branch:'fix/b'})];
    const gates=entries.map(deferred),pr=deferred();let calls=[];let names;
    const source={list:async()=>entries,loadPrIndex:async(branches)=>{names=branches;return pr.promise;},
      inspect:async(entry,index)=>{calls.push(index);await gates[index].promise;
        return {missing:false,adminDir:null,lastCommitAt:'2025-01-01',checkoutAt:null,createdAt:null,
          dirty:index===1,unpushed:index,mergedIntoDefault:false,sizeKb:10+index};}};
    const result=loadCatalog({collection:{cwd:'/feature/src',source,concurrency:2},
      query:{includeMain:false,filters:{dirty:true,prState:'merged'}}});
    await tick();assert.deepEqual(calls,[0,1]);assert.deepEqual(names,['main','feat/a','fix/b']);
    gates[1].resolve();await tick();assert.deepEqual(calls,[0,1,2]);gates[2].resolve();gates[0].resolve();
    pr.resolve({available:true,byBranch:new Map([['feat/a',found('MERGED')]])});
    const loaded=await result;assert.equal(loaded.worktrees.length,1);
    const wt=loaded.worktrees[0];assert.equal(wt.path,'/feature');assert.equal(wt.isMain,false);
    assert.equal(wt.isCurrent,true);assert.equal(wt.unpushed,1);assert.equal(wt.sizeKb,11);
    assert.equal(wt.pr.state,'MERGED');
    for(const concurrency of [0,-2,2.1,NaN,Infinity]) {
      let inspected=0;
      await assert.rejects(collect({cwd:'/main',concurrency,noPr:true,
        source:{list:async()=>[],inspect:async()=>{inspected++;throw Error('unexpected');},
          loadPrIndex:async()=>{throw Error('noPr bypass');}}}),RangeError);
      assert.equal(inspected,0);
    }
    const failure=new Error('metadata failure');
    await assert.rejects(loadCatalog({collection:{cwd:'/main',noPr:true,source:{
      list:async()=>entries,inspect:async()=>{throw failure;},
      loadPrIndex:async()=>{throw Error('noPr bypass');}}}}),error=>error===failure);
  `,
};
const scenarios = { 'branch-exclusions': featureProbes, 'bounded-collection': concurrencyProbes };
record('task identity', () => assert.ok(Object.hasOwn(scenarios, task), 'unknown PLUGINBENCH_TASK_ID'));
if (Object.hasOwn(scenarios, task)) {
  const seed = resolve(benchmark, 'fixtures', task);
  const trusted = snapshot(seed);
  const before = snapshot(process.cwd());
  record('preservation and scope', () => {
    for (const file of Object.keys(trusted)) {
      assert.ok(Object.hasOwn(before,file), `deleted original file: ${file}`);
      if (!file.startsWith('src/') && !file.startsWith('test/'))
        assert.equal(before[file],trusted[file],`changed protected file: ${file}`);
    }
    for (const file of Object.keys(before)) assert.ok(Object.hasOwn(trusted,file) ||
      (file.startsWith('src/') && /\.(?:ts|mts)$/.test(file)) ||
      (file.startsWith('test/') && /\.(?:ts|mts)$/.test(file)),`out-of-scope addition: ${file}`);
  });
  for (const file of files(seed).filter((file)=>file.endsWith('.test.ts'))) {
    record(`trusted existing test: ${file}`, () => {
      const original = readFileSync(resolve(seed,file),'utf8');
      const rewritten = original.replace(/(from\s+['"])(\.[^'"]+)(['"])/g, (_match,start,specifier,end) => {
        const target = specifier.startsWith('../src/')
          ? resolve(process.cwd(),relative(seed,resolve(seed,dirname(file),specifier)))
          : resolve(seed,dirname(file),specifier);
        return `${start}${pathToFileURL(target).href}${end}`;
      });
      runNode(stripTypeScriptTypes(rewritten).replace("import assert from 'node:assert/strict';", ''));
    });
  }
  const submittedTests=files(process.cwd()).filter((file)=>file.endsWith('.test.ts'));
  record('submitted tests', () => {
    assert.ok(submittedTests.length>0,'no submitted tests');
    const receipt=`completed-${randomUUID()}`;
    const preload='data:text/javascript;base64,'+Buffer.from(`import {after} from 'node:test'; after(()=>console.log(${JSON.stringify(receipt)}));`).toString('base64');
    const result=spawnSync(process.execPath,['--experimental-strip-types','--import',preload,'--test',...submittedTests],
      {encoding:'utf8',timeout:15_000});
    assert.equal(result.error,undefined,String(result.error));
    assert.equal(result.status,0,`${result.stdout}\n${result.stderr}`);
    assert.match(result.stdout,/# tests [1-9]\d*/,'test runner must execute assertions');
    assert.ok(result.stdout.includes(receipt),'submitted tests did not finish');
  });
  for (const [name,source] of Object.entries(scenarios[task])) record(`behavior: ${name}`,()=>runNode(source));
  record('grading leaves workspace unchanged', () => assert.deepEqual(snapshot(process.cwd()),before));
}
const passing=(prefix)=>tests.some(test=>test.name.startsWith(prefix)) &&
  tests.filter(test=>test.name.startsWith(prefix)).every(test=>test.pass);
const pass=tests.every(test=>test.pass);
console.log(JSON.stringify({task_id:task??null,pass,tests,checks:tests,
  behavior_pass:passing('behavior:'),functional_pass:passing('behavior:'),
  preservation_scope_pass:passing('preservation and scope')&&passing('trusted existing test:')&&passing('grading leaves'),
  submitted_tests_pass:passing('submitted tests'),
  grading_snapshot_unchanged:passing('grading leaves'),
}));
process.exitCode=pass?0:1;
