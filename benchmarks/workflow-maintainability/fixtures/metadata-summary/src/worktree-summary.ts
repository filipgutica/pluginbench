import type { Worktree } from './types.ts';

export const formatWorktreeSummary = ({ worktree, now }: { worktree: Worktree; now: Date }): string => {
  void now;
  return worktree.branch;
};
