import assert from 'node:assert/strict';
import { it } from 'node:test';
import { mapLimit } from '../src/concurrency.ts';
import { collect, type CollectionSource, type LocalState } from '../src/collect.ts';
import { loadCatalog } from '../src/catalog.ts';
import { makeWorktree } from './helpers.ts';

it('maps in input order and passes input indexes', async () => {
  assert.deepEqual(await mapLimit(['c', 'a', 'b'], 2, async (item, index) => `${index}:${item}`),
    ['0:c', '1:a', '2:b']);
  assert.deepEqual(await mapLimit([], 2, async (item) => item), []);
});

it('collects metadata and merges one PR index before the query caller runs', async () => {
  const entries = [makeWorktree({ path: '/repo', branch: 'main' }),
    makeWorktree({ path: '/repo-feature', branch: 'feat/a' })];
  const branches: string[][] = [];
  const local: LocalState = { missing: false, adminDir: null,
    lastCommitAt: '2025-01-01', checkoutAt: null, createdAt: null, dirty: true,
    unpushed: 2, mergedIntoDefault: false, sizeKb: 12 };
  const source: CollectionSource = {
    list: async () => entries,
    inspect: async () => local,
    loadPrIndex: async (names) => {
      branches.push(names);
      return { available: true, byBranch: new Map() };
    },
  };
  const result = await loadCatalog({ collection: { cwd: '/repo-feature/src', source },
    query: { includeMain: false, filters: { branch: 'feat/*', dirty: true } } });
  assert.deepEqual(branches, [['main', 'feat/a']]);
  assert.equal(result.worktrees.length, 1);
  assert.equal(result.worktrees[0]?.isCurrent, true);
  assert.equal(result.worktrees[0]?.sizeKb, 12);
  assert.deepEqual(result.worktrees[0]?.pr, { status: 'none' });
  const withoutPr = await collect({ cwd: '/repo', source, noPr: true });
  assert.equal(branches.length, 1);
  assert.deepEqual(withoutPr.prIndex, { available: false, reason: '--no-pr' });
  assert.deepEqual(withoutPr.worktrees[0]?.pr, { status: 'unknown', reason: '--no-pr' });
  assert.equal(withoutPr.worktrees[0]?.isMain, true);
});
