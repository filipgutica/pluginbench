export type PrState = 'OPEN' | 'MERGED' | 'CLOSED';

/** PR status for a worktree branch. `unknown` means we could not ask GitHub. */
export type PrInfo =
  | { status: 'unknown'; reason: string }
  | { status: 'none' }
  | {
      status: 'found';
      number: number;
      state: PrState;
      title: string;
      url: string;
      mergedAt: string | null;
      closedAt: string | null;
      updatedAt: string;
      isCrossRepository: boolean;
    };

/** One entry of `git worktree list --porcelain`. */
export interface RawWorktree {
  path: string;
  head: string | null;
  branch: string | null;
  bare: boolean;
  detached: boolean;
  locked: boolean;
  lockReason: string | null;
  prunable: boolean;
  prunableReason: string | null;
}

export interface Worktree extends RawWorktree {
  /** First entry of the porcelain list: the worktree that owns the common git dir. */
  isMain: boolean;
  /** True when the process cwd is inside this worktree. */
  isCurrent: boolean;
  /** Directory is absent on disk. */
  missing: boolean;
  /** This worktree's record under `.git/worktrees/<id>`, null for the main worktree. */
  adminDir: string | null;
  /** Author date of HEAD, ISO 8601. */
  lastCommitAt: string | null;
  /** mtime of the worktree's admin `index` file: when git last touched it. */
  checkoutAt: string | null;
  /** Directory birth time. */
  createdAt: string | null;
  /** Uncommitted changes present. */
  dirty: boolean;
  /** Commits on HEAD not present on its upstream. Null when no upstream. */
  unpushed: number | null;
  /** HEAD is an ancestor of the default branch (catches real merges, not squashes). */
  mergedIntoDefault: boolean;
  /** Disk usage in kibibytes, only when `--size` was requested. */
  sizeKb: number | null;
  pr: PrInfo;
}

export type AgeBasis = 'commit' | 'checkout' | 'created';
