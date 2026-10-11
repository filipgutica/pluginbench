import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

// Trusted seed inventories are part of this fingerprinted external verifier.
const seeds = {
  "grid-version-bump": {
    "hashes": {
      "notes.md": "037a2bbdc14b179ea09c7c65b42a9fc37ae908673b75d1352acfb577c696512c",
      "package-lock.json": "dc0cc6203e6483d1ebb1c784b3b03fbad9c3f626a5b60f36a8f3e3dbe7b90608",
      "package.json": "515ccb9fad6883c85de6bb33cbd6825efbac4e576f3348009d9aa7bd3d02abe4",
      "packages/analytics-app/package.json": "637da98525c491a53793206b3497e59814ccdff46e50a3549d60c86eae3b154f",
      "packages/analytics-app/src/dashboard.ts": "b455c2fa263d4aedd416dd636cf994e29c384ce944df0314b0753c690503a958",
      "packages/direct-app/package.json": "c2ee7a1cd84ff18c643872d3d5c5c203912456569378e9d604cecd4b331f9a56",
      "packages/direct-app/src/dashboard.ts": "9d82358f2f84f12f5184d85fb766d1761671eda31a01b90405b7bf913778bfb1",
      "packages/grid-widget/package.json": "84011c5df7f1b32ab8ca301fd295ac0dc8267e2d632d6f6e5847592ca38ef1ac",
      "packages/grid-widget/src/grid.ts": "d87e1d00b8909f661adbf211296745948cf672235e23bb9aac11bbf7292bfe90",
      "packages/grid-widget/src/theme.css": "011c3f2e9a3b3a1c0fbe2b4128307fdda84b8a6156d84ba9ea4b7a4ef52dd151",
      "test/workspaces.test.ts": "039fb7f85415fbb32d8b98f3292102577568981e108eeaf5206fefbe988259ef"
    },
    "json": {
      "packages/grid-widget/package.json": {
        "name": "@fixture/grid-widget",
        "version": "1.0.0",
        "type": "module",
        "exports": "./src/grid.ts",
        "peerDependencies": {
          "@kong-ui-public/table-data-grid": "^1.4.0"
        },
        "devDependencies": {
          "@kong-ui-public/table-data-grid": "1.4.0"
        }
      },
      "packages/direct-app/package.json": {
        "name": "@fixture/direct-app",
        "version": "1.0.0",
        "type": "module",
        "dependencies": {
          "@fixture/grid-widget": "1.0.0",
          "@kong-ui-public/table-data-grid": "1.4.0"
        }
      },
      "package-lock.json": {
        "name": "dashboard-workspaces",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": true,
        "packages": {
          "": {
            "name": "dashboard-workspaces",
            "version": "1.0.0",
            "workspaces": [
              "packages/*"
            ]
          },
          "packages/grid-widget": {
            "name": "@fixture/grid-widget",
            "version": "1.0.0",
            "peerDependencies": {
              "@kong-ui-public/table-data-grid": "^1.4.0"
            },
            "devDependencies": {
              "@kong-ui-public/table-data-grid": "1.4.0"
            }
          },
          "packages/direct-app": {
            "name": "@fixture/direct-app",
            "version": "1.0.0",
            "dependencies": {
              "@fixture/grid-widget": "1.0.0",
              "@kong-ui-public/table-data-grid": "1.4.0"
            }
          },
          "packages/analytics-app": {
            "name": "@fixture/analytics-app",
            "version": "1.0.0",
            "dependencies": {
              "@fixture/grid-widget": "1.0.0"
            }
          },
          "node_modules/@fixture/grid-widget": {
            "resolved": "packages/grid-widget",
            "link": true
          },
          "node_modules/@fixture/direct-app": {
            "resolved": "packages/direct-app",
            "link": true
          },
          "node_modules/@fixture/analytics-app": {
            "resolved": "packages/analytics-app",
            "link": true
          },
          "node_modules/@kong-ui-public/table-data-grid": {
            "version": "1.4.0",
            "resolved": "https://registry.npmjs.org/@kong-ui-public/table-data-grid/-/table-data-grid-1.4.0.tgz"
          }
        }
      }
    }
  },
  "metadata-summary": {
    "hashes": {
      "notes.md": "037a2bbdc14b179ea09c7c65b42a9fc37ae908673b75d1352acfb577c696512c",
      "package.json": "e7556d82bd7d2d08377ee293d333a25d726388c9fc2f775b72970609ba1b2dfa",
      "src/formatting.ts": "9c5500120c0c02ae8f5d79db13f7950905de942f69db26bf096a3b592ea639a0",
      "src/preview.ts": "c53b1cbbb70cf7c99c6fafcc64a97effb72d74a43204695267de8dd95522db12",
      "src/types.ts": "81ae7d42ff7e8c2fead473862e09182371247e3b2b06d7156661c1eda5b9d634",
      "src/worktree-summary.ts": "36c157e3394540c7e3cbd8a1f11fb61570ca809037eb7d8a893c558064a9e871",
      "test/formatting.test.ts": "a3d26d4c543abc77f0ec3737928131bd0bf091671339edc682d5cdfba2cb9c61"
    }
  },
  "selection-count": {
    "hashes": {
      "notes.md": "037a2bbdc14b179ea09c7c65b42a9fc37ae908673b75d1352acfb577c696512c",
      "package.json": "6cd65c164136bd5ffbd2526c77b6b1347f899cb1a673433541abbd56af503bca",
      "src/picker.ts": "a114ce30a25d81b08f042796f075c8504274b4aaa5998d84e787401d15909de3",
      "src/selection-state.ts": "e00c1c41ccd8fe78e53f023537a74da248f07bc2f6a3ecc1bf1ca169b844b4dc",
      "test/selection-state.test.ts": "702db912284beef9e9010efbf575d69f7eda4aac1d19a29fffb2292726e99426"
    }
  }
};
const task = process.env.PLUGINBENCH_TASK_ID;
const checks = [];
const record = (name, action) => {
  try { action(); checks.push({ name, pass: true }); }
  catch (error) { checks.push({ name, pass: false, error: String(error) }); }
};
const hash = (file) => createHash('sha256').update(readFileSync(file)).digest('hex');
const json = (file) => JSON.parse(readFileSync(file, 'utf8'));
const walk = (directory) => readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
  const file = `${directory}/${entry.name}`;
  assert.ok(!entry.isSymbolicLink(), `unsupported symlink: ${file}`);
  return entry.isDirectory() ? walk(file) : [file.replace(/^\.\//, '')];
});
const workspaceSnapshot = () => {
  const entries = {};
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      if (directory === '.' && (entry.name === '.git' || entry.name === '.agents')) continue;
      const path = `${directory}/${entry.name}`.replace(/^\.\//, '');
      assert.ok(!entry.isSymbolicLink(), `unsupported symlink: ${path}`);
      entries[path] = entry.isDirectory() ? { kind: 'directory' } : { kind: 'file', hash: hash(path) };
      if (entry.isDirectory()) visit(path);
    }
  };
  visit('.');
  return entries;
};
const preservationAndScope = (scenario) => {
  const inventory = seeds[task].hashes;
  const submitted = workspaceSnapshot();
  for (const [file, expected] of Object.entries(inventory)) {
    assert.equal(submitted[file]?.kind, 'file', `deleted original file: ${file}`);
    if (!scenario.mutable.includes(file)) assert.equal(submitted[file].hash, expected, `changed protected file: ${file}`);
  }
  for (const [file, entry] of Object.entries(submitted)) {
    if (entry.kind !== 'file') continue;
    assert.ok(Object.hasOwn(inventory, file) || (file.startsWith('test/') && file.endsWith('.test.ts')),
      `out-of-scope addition: ${file}`);
  }
  assert.ok(!existsSync('.pilot-scratch'), 'temporary scratch was not removed');
};

const checkedNode = (argv, receipt) => {
  const result = spawnSync(process.execPath, argv, { encoding: 'utf8', timeout: 10_000 });
  assert.equal(result.error, undefined, String(result.error));
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.ok(result.stdout.includes(receipt), `missing completion receipt\n${result.stdout}\n${result.stderr}`);
};
const behavior = (source) => {
  const receipt = `completed-${randomUUID()}`;
  checkedNode(['--experimental-strip-types', '--input-type=module', '-e',
    `import assert from 'node:assert/strict';\n${source}\nconsole.log(${JSON.stringify(receipt)});`], receipt);
};

const scenarios = {
  'metadata-summary': {
    mutable: ['src/worktree-summary.ts'],
    behavior: () => behavior(`
      import { renderPreview } from './src/preview.ts';
      const now = new Date('2026-10-09T12:00:00Z');
      const cases = [
        [{branch:'feature/a',updatedAt:'2026-10-09T09:30:00Z',bytes:2560}, 'feature/a · 2h · 2.5KiB'],
        [{branch:'main',updatedAt:null,bytes:null}, 'main · unknown · unknown'],
        [{branch:'',updatedAt:'2026-10-09T12:10:00Z',bytes:768}, ' · just now · 768B'],
        [{branch:'release',updatedAt:'2026-10-06T12:00:00Z',bytes:2621440}, 'release · 3d · 2.5MiB'],
      ];
      for (const [worktree, expected] of cases) {
        assert.equal(renderPreview({worktree, now}), 'Worktree: ' + expected);
      }
      assert.equal(renderPreview({worktree:{branch:'later',updatedAt:'2026-10-09T11:30:00Z',bytes:0}, now}),
        'Worktree: later · 30m · 0B');
    `),
  },
  'selection-count': {
    mutable: ['src/selection-state.ts'],
    behavior: () => behavior(`
      import { SelectionState } from './src/selection-state.ts';
      import { focusSelectedPath } from './src/picker.ts';
      const state = new SelectionState();
      assert.equal(state.count, 0);
      assert.equal(Reflect.set(state, 'count', 99), false, 'count must be readonly at runtime');
      assert.equal(state.count, 0);
      state.toggle('/a'); state.toggle('/b');
      assert.equal(state.count, 2); assert.equal(focusSelectedPath(state, '/a'), '/a');
      state.toggle('/a'); assert.equal(state.count, 1);
      state.replace(['/c','/c','/d']); assert.equal(state.count, 2); assert.equal(state.has('/b'), false);
      state.replace([]); assert.equal(state.count, 0);
      state.replace(new Set(['/e','/f'])); assert.equal(state.count, 2);
      state.clear(); assert.equal(state.count, 0);
      state.toggle('/z'); assert.equal(state.count, 1);
      const other = new SelectionState(); assert.equal(other.count, 0);
      assert.equal(Reflect.set(state, 'count', -1), false); assert.equal(state.count, 1);
    `),
  },
  'grid-version-bump': {
    mutable: ['packages/grid-widget/package.json', 'packages/direct-app/package.json', 'package-lock.json'],
    behavior: () => {
      const grid = '@kong-ui-public/table-data-grid';
      const widget = seeds['grid-version-bump'].json['packages/grid-widget/package.json'];
      const direct = seeds['grid-version-bump'].json['packages/direct-app/package.json'];
      const lock = structuredClone(seeds['grid-version-bump'].json['package-lock.json']);
      const expectedWidget = structuredClone(widget);
      expectedWidget.peerDependencies[grid] = '^1.4.1';
      expectedWidget.devDependencies[grid] = '1.4.1';
      const expectedDirect = structuredClone(direct);
      expectedDirect.dependencies[grid] = '1.4.1';
      lock.packages['packages/grid-widget'].peerDependencies[grid] = '^1.4.1';
      lock.packages['packages/grid-widget'].devDependencies[grid] = '1.4.1';
      lock.packages['packages/direct-app'].dependencies[grid] = '1.4.1';
      lock.packages[`node_modules/${grid}`].version = '1.4.1';
      lock.packages[`node_modules/${grid}`].resolved =
        'https://registry.npmjs.org/@kong-ui-public/table-data-grid/-/table-data-grid-1.4.1.tgz';
      assert.deepEqual(json('packages/grid-widget/package.json'), expectedWidget);
      assert.deepEqual(json('packages/direct-app/package.json'), expectedDirect);
      assert.deepEqual(json('package-lock.json'), lock);
    },
  },
};

record('task identity', () => assert.ok(Object.hasOwn(scenarios, task), 'unknown or missing PLUGINBENCH_TASK_ID'));
if (Object.hasOwn(scenarios, task)) {
  const scenario = scenarios[task];
  record('native type stripping runtime', () => {
    const [major, minor] = process.versions.node.split('.').map(Number);
    assert.ok(major > 22 || (major === 22 && minor >= 20), 'Node 22.20+ required');
  });
  record('preservation and scope', () => preservationAndScope(scenario));
  let submitted;
  record('submitted workspace snapshot', () => { submitted = workspaceSnapshot(); });
  record('test discovery', () => assert.ok(existsSync('test'), 'test directory missing'));
  let tests = [];
  record('recursive test inventory', () => {
    tests = walk('test').filter((file) => file.endsWith('.test.ts')).sort();
    assert.ok(tests.length > 0, 'no test files found');
  });
  for (const file of tests) {
    record(`repository test: ${file}`, () => {
      const receipt = `completed-${randomUUID()}`;
      const bootstrap = `import {after} from 'node:test'; after(() => console.log(${JSON.stringify(receipt)}));`;
      const preload = `data:text/javascript;base64,${Buffer.from(bootstrap).toString('base64')}`;
      checkedNode(['--experimental-strip-types', '--import', preload, '--test', '--test-reporter=tap', file], receipt);
    });
  }
  record('hidden behavioral contract', scenario.behavior);
  record('preservation and scope after grading', () => preservationAndScope(scenario));
  record('grading leaves submitted workspace unchanged', () => {
    assert.ok(submitted, 'submitted snapshot unavailable');
    assert.deepEqual(workspaceSnapshot(), submitted, 'grading changed submitted files or inventory');
  });
}
const pass = checks.every((check) => check.pass);
const behavioral = checks.filter((check) =>
  check.name === 'hidden behavioral contract' || check.name.startsWith('repository test:'));
const preservationChecks = ['preservation and scope', 'submitted workspace snapshot',
  'preservation and scope after grading', 'grading leaves submitted workspace unchanged'];
console.log(JSON.stringify({
  task_id: task ?? null, pass, checks,
  functional_pass: behavioral.length > 1 && behavioral.every((check) => check.pass),
  preservation_scope_pass: preservationChecks.every((name) => checks.some((check) => check.name === name && check.pass)),
  grading_snapshot_unchanged: checks.some((check) => check.name === 'grading leaves submitted workspace unchanged' && check.pass),
  observations: {
    simplicity: 'Not graded by source patterns; review helper reuse, mirror state, and redundant tests independently.',
    type_correctness: 'Not checked: Node strips types without a TypeScript compiler.',
    containment: 'Not established: final file inventory is a preservation/scope check, not a filesystem sandbox.',
  },
}));
process.exitCode = pass ? 0 : 1;
