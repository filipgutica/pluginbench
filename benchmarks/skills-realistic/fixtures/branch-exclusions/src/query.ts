import { applyFilters, sortWorktrees, type SortKey } from './filter.ts';
import { buildFilters, normalizeDone, type FilterOptions } from './options.ts';
import type { AgeBasis, Worktree } from './types.ts';

export interface QueryOptions {
  filters?: FilterOptions;
  ageBasis?: AgeBasis;
  includeMain?: boolean;
  sort?: SortKey;
  reverse?: boolean;
}

/** The list caller: normalize options once, filter, then sort a fresh result. */
export const queryWorktrees = ({
  worktrees,
  options = {},
}: { worktrees: Worktree[]; options?: QueryOptions }): Worktree[] => {
  const basis = options.ageBasis ?? 'commit';
  const filters = buildFilters({
    options: normalizeDone(options.filters ?? {}),
    ageBasis: basis,
    includeMain: options.includeMain ?? true,
  });
  return sortWorktrees(
    applyFilters(worktrees, filters), options.sort ?? 'path', basis, options.reverse ?? false,
  );
};
