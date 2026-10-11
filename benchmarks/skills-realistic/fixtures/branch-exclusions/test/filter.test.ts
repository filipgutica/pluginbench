import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
  ageDays,
  ageTimestamp,
  applyFilters,
  globToRegExp,
  InvalidFilterError,
  parseCutoff,
  type Filters,
} from '../src/filter.ts';
import type { AgeBasis, Worktree } from '../src/types.ts';
import { makeWorktree } from './helpers.ts';

const NOW = new Date('2025-06-15T00:00:00.000Z');

const withDefaults = (overrides: Partial<Filters> = {}): Filters => ({
  ageBasis: 'commit',
  ...overrides,
});

describe('parseCutoff', () => {
  it('resolves relative durations using the supplied clock', () => {
    const expected: Array<[string, string]> = [
      ['30d', '2025-05-16T00:00:00.000Z'],
      ['3mo', '2025-03-17T00:00:00.000Z'],
      ['1y', '2024-06-15T00:00:00.000Z'],
      ['12h', '2025-06-14T12:00:00.000Z'],
      ['6w', '2025-05-04T00:00:00.000Z'],
      ['3m', '2025-03-17T00:00:00.000Z'],
    ];

    for (const [expression, iso] of expected) {
      assert.equal(parseCutoff(expression, NOW).toISOString(), iso);
    }
  });

  it('preserves an absolute ISO instant', () => {
    const iso = '2025-01-31T12:34:56.000Z';
    assert.equal(parseCutoff(iso, NOW).toISOString(), iso);
  });

  it('rejects garbage input with InvalidFilterError', () => {
    assert.throws(() => parseCutoff('nope', NOW), InvalidFilterError);
  });
});

describe('ageTimestamp and ageDays', () => {
  const timestamps = {
    lastCommitAt: '2025-06-10T00:00:00.000Z',
    checkoutAt: '2025-06-11T00:00:00.000Z',
    createdAt: '2025-06-12T00:00:00.000Z',
  } as const;

  const orders: Record<AgeBasis, readonly [keyof typeof timestamps, keyof typeof timestamps, keyof typeof timestamps]> = {
    commit: ['lastCommitAt', 'checkoutAt', 'createdAt'],
    checkout: ['checkoutAt', 'createdAt', 'lastCommitAt'],
    created: ['createdAt', 'checkoutAt', 'lastCommitAt'],
  };

  it('uses each basis order and falls through null timestamps', () => {
    for (const basis of Object.keys(orders) as AgeBasis[]) {
      const [preferred, fallback, final] = orders[basis];
      assert.equal(ageTimestamp(makeWorktree(timestamps), basis), timestamps[preferred]);
      assert.equal(
        ageTimestamp(makeWorktree({ ...timestamps, [preferred]: null }), basis),
        timestamps[fallback],
      );
      assert.equal(
        ageTimestamp(makeWorktree({ ...timestamps, [preferred]: null, [fallback]: null }), basis),
        timestamps[final],
      );
      assert.equal(
        ageTimestamp(
          makeWorktree({ lastCommitAt: null, checkoutAt: null, createdAt: null }),
          basis,
        ),
        null,
      );
    }
  });

  it('returns the numeric age in days for every basis and null when unknown', () => {
    for (const basis of Object.keys(orders) as AgeBasis[]) {
      const [preferred] = orders[basis];
      const wt = makeWorktree({
        lastCommitAt: null,
        checkoutAt: null,
        createdAt: null,
        [preferred]: '2025-06-13T00:00:00.000Z',
      });
      assert.equal(ageDays(wt, basis, NOW), 2);
    }
    assert.equal(
      ageDays(
        makeWorktree({ lastCommitAt: null, checkoutAt: null, createdAt: null }),
        'commit',
        NOW,
      ),
      null,
    );
  });
});

describe('applyFilters', () => {
  it('does not treat an unknown PR as merged or closed', () => {
    const wt = makeWorktree({ pr: { status: 'unknown', reason: 'x' } });
    const result = applyFilters(
      [wt],
      withDefaults({ prState: new Set(['merged', 'closed']) }),
    );
    assert.deepEqual(result, []);
  });

  it('excludes the main worktree by default and includes it when requested', () => {
    const main = makeWorktree({ isMain: true, path: '/tmp/repo/main' });
    const linked = makeWorktree({ path: '/tmp/repo/wt-1' });

    assert.deepEqual(applyFilters([main, linked], withDefaults()), [linked]);
    assert.deepEqual(applyFilters([main, linked], withDefaults({ includeMain: true })), [main, linked]);
  });

  it('fails closed when olderThan cannot resolve a timestamp', () => {
    const unknownAge = makeWorktree({ lastCommitAt: null, checkoutAt: null, createdAt: null });
    const result = applyFilters(
      [unknownAge],
      withDefaults({ olderThan: new Date('2025-06-01T00:00:00.000Z') }),
    );
    assert.deepEqual(result, []);
  });

  it('matches a branch glob against the whole branch name', () => {
    const matching = makeWorktree({ branch: 'feat/a', path: '/tmp/repo/feat-a' });
    const nested = makeWorktree({ branch: 'feat/a/b', path: '/tmp/repo/feat-a-b' });
    const outside = makeWorktree({ branch: 'x/feat/a', path: '/tmp/repo/outside' });

    assert.deepEqual(
      applyFilters([matching, nested, outside], withDefaults({ branch: 'feat/*' })),
      [matching, nested],
    );
  });

  for (const field of ['dirty', 'prunable', 'locked'] as const) {
    it(`filters ${field} independently`, () => {
      const matching = makeWorktree({ [field]: true, path: `/tmp/repo/${field}` });
      const nonMatching = makeWorktree({ path: `/tmp/repo/not-${field}` });

      assert.deepEqual(
        applyFilters([matching, nonMatching], withDefaults({ [field]: true })),
        [matching],
      );
    });
  }
});

describe('globToRegExp', () => {
  it('matches nested feature branches but not a prefixed path', () => {
    const re = globToRegExp('feat/*');
    assert.equal(re.test('feat/a'), true);
    assert.equal(re.test('feat/a/b'), true);
    assert.equal(re.test('x/feat/a'), false);
  });

  it('escapes literal regex metacharacters', () => {
    const re = globToRegExp('a.b');
    assert.equal(re.test('a.b'), true);
    assert.equal(re.test('axb'), false);
  });

  it('uses the implementation\'s single-character wildcard behavior for ?', () => {
    const re = globToRegExp('a?b');
    assert.equal(re.test('a.b'), true);
    assert.equal(re.test('axb'), true);
    assert.equal(re.test('ab'), false);
  });
});
