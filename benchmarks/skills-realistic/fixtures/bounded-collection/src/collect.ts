import { mapLimit } from './concurrency.ts';
import type { PrInfo, RawWorktree, Worktree } from './types.ts';

export type LocalState = Pick<Worktree,
  'missing' | 'adminDir' | 'lastCommitAt' | 'checkoutAt' | 'createdAt' |
  'dirty' | 'unpushed' | 'mergedIntoDefault' | 'sizeKb'>;

export type PrIndex =
  | { available: false; reason: string }
  | { available: true; byBranch: ReadonlyMap<string, PrInfo> };

/** In-memory boundary replacing Git, stat, du, and GitHub adapters. */
export interface CollectionSource {
  list(): Promise<RawWorktree[]>;
  inspect(entry: RawWorktree, index: number): Promise<LocalState>;
  loadPrIndex(branches: string[]): Promise<PrIndex>;
}

export interface CollectOptions {
  cwd: string;
  source: CollectionSource;
  noPr?: boolean;
  concurrency?: number;
}

export interface Collection {
  worktrees: Worktree[];
  prIndex: PrIndex;
}

const prFor = (index: PrIndex, branch: string | null): PrInfo => {
  if (!index.available) return { status: 'unknown', reason: index.reason };
  return branch === null ? { status: 'none' } : index.byBranch.get(branch) ?? { status: 'none' };
};

/** Collect local metadata concurrently while one bulk PR index loads. */
export const collect = async ({
  cwd, source, noPr = false, concurrency = 8,
}: CollectOptions): Promise<Collection> => {
  const raw = await source.list();
  const prIndexPromise = noPr
    ? Promise.resolve({ available: false, reason: '--no-pr' } satisfies PrIndex)
    : source.loadPrIndex(raw.map((entry) => entry.branch).filter((b): b is string => b !== null));
  const localWorktreesPromise = mapLimit(raw, concurrency, async (entry, index) => {
    const local = await source.inspect(entry, index);
    return {
      ...entry,
      ...local,
      isMain: index === 0,
      isCurrent: !local.missing && (cwd === entry.path || cwd.startsWith(`${entry.path}/`)),
    };
  });
  const [prIndex, localWorktrees] = await Promise.all([prIndexPromise, localWorktreesPromise]);
  return {
    prIndex,
    worktrees: localWorktrees.map((wt): Worktree => ({ ...wt, pr: prFor(prIndex, wt.branch) })),
  };
};
