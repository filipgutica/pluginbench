import type { Worktree } from './types.ts';
import { formatWorktreeSummary } from './worktree-summary.ts';

export const renderPreview = ({ worktree, now }: { worktree: Worktree; now: Date }): string =>
  `Worktree: ${formatWorktreeSummary({ worktree, now })}`;
