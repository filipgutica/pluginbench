import assert from 'node:assert/strict';
import { it } from 'node:test';
import { queryWorktrees } from '../src/query.ts';
import { InvalidFilterError } from '../src/filter.ts';
import { makeWorktree } from './helpers.ts';

it('combines list filters, keeps main context, sorts without mutating input', () => {
  const main = makeWorktree({ path: '/repo', isMain: true, branch: 'main' });
  const feature = makeWorktree({ path: '/z', branch: 'feat/z', dirty: true });
  const unrelated = makeWorktree({ path: '/a', branch: 'fix/a', dirty: true });
  const input = [feature, main, unrelated];
  assert.deepEqual(queryWorktrees({ worktrees: input }), [main, unrelated, feature]);
  assert.deepEqual(queryWorktrees({ worktrees: input, options: {
    filters: { branch: 'feat/*', dirty: true }, includeMain: false,
  }}), [feature]);
  assert.deepEqual(input, [feature, main, unrelated]);
});

it('normalizes --done and reports conflicting and malformed options', () => {
  const merged = makeWorktree({ pr: { status: 'found', number: 1, state: 'MERGED',
    title: 'merged', url: 'local:1', mergedAt: null, closedAt: null,
    updatedAt: '2025-01-01', isCrossRepository: false } });
  assert.deepEqual(queryWorktrees({ worktrees: [merged], options: {
    filters: { done: true }, includeMain: false,
  }}), [merged]);
  assert.throws(() => queryWorktrees({ worktrees: [], options: {
    filters: { done: true, prState: 'open' },
  }}), InvalidFilterError);
  assert.throws(() => queryWorktrees({ worktrees: [], options: {
    filters: { olderThan: 'invalid' },
  }}), InvalidFilterError);
  assert.deepEqual(queryWorktrees({ worktrees: [] }), []);
});
