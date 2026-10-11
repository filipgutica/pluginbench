import type { AgeBasis, Worktree } from './types.ts';

export class InvalidFilterError extends Error {}

const UNIT_MS: Record<string, number> = {
  h: 3600_000,
  d: 86_400_000,
  w: 7 * 86_400_000,
  mo: 30 * 86_400_000,
  y: 365 * 86_400_000,
};

/**
 * Resolve an age expression to the cutoff instant it describes.
 * Accepts `12h`, `30d`, `6w`, `3mo`, `1y`, or an absolute ISO date.
 * Months are 30 days and years 365 days: calendar-exact ages are not the point here.
 */
export const parseCutoff = (expr: string, now: Date = new Date()): Date => {
  const trimmed = expr.trim();
  const rel = /^(\d+(?:\.\d+)?)\s*(h|d|w|mo|m|y)$/i.exec(trimmed);
  if (rel) {
    const amount = Number.parseFloat(rel[1] as string);
    const rawUnit = (rel[2] as string).toLowerCase();
    // `m` is ambiguous; treat it as months, which is what people mean beside d/y.
    const unit = rawUnit === 'm' ? 'mo' : rawUnit;
    const ms = UNIT_MS[unit];
    if (ms === undefined) throw new InvalidFilterError(`unknown duration unit: ${rawUnit}`);
    return new Date(now.getTime() - amount * ms);
  }
  const abs = new Date(trimmed);
  if (Number.isNaN(abs.getTime())) {
    throw new InvalidFilterError(
      `could not read "${expr}" as a duration (30d, 3mo, 1y) or a date (2025-01-31)`,
    );
  }
  return abs;
};

/**
 * The timestamp an age filter compares against. `commit` is the default because
 * directory mtime moves whenever a build runs, which makes stale worktrees look active.
 */
export const ageTimestamp = (wt: Worktree, basis: AgeBasis): string | null => {
  const order: Record<AgeBasis, (string | null)[]> = {
    commit: [wt.lastCommitAt, wt.checkoutAt, wt.createdAt],
    checkout: [wt.checkoutAt, wt.createdAt, wt.lastCommitAt],
    created: [wt.createdAt, wt.checkoutAt, wt.lastCommitAt],
  };
  return order[basis].find((v) => v !== null) ?? null;
};

export const ageDays = (wt: Worktree, basis: AgeBasis, now: Date = new Date()): number | null => {
  const ts = ageTimestamp(wt, basis);
  if (!ts) return null;
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return null;
  return (now.getTime() - t) / 86_400_000;
};

export type PrStateFilter = 'open' | 'merged' | 'closed' | 'none' | 'unknown';

export const PR_STATE_FILTERS: readonly PrStateFilter[] = [
  'open',
  'merged',
  'closed',
  'none',
  'unknown',
];

export const prStateOf = (wt: Worktree): PrStateFilter => {
  if (wt.pr.status === 'unknown') return 'unknown';
  if (wt.pr.status === 'none') return 'none';
  return wt.pr.state.toLowerCase() as PrStateFilter;
};

export const parsePrStates = (value: string): Set<PrStateFilter> => {
  const out = new Set<PrStateFilter>();
  for (const part of value.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean)) {
    if (!(PR_STATE_FILTERS as readonly string[]).includes(part)) {
      throw new InvalidFilterError(
        `unknown pr state "${part}" (expected one of: ${PR_STATE_FILTERS.join(', ')})`,
      );
    }
    out.add(part as PrStateFilter);
  }
  if (out.size === 0) throw new InvalidFilterError('--pr-state needs at least one value');
  return out;
};

/** Shell-style glob: `*` and `?` only, anchored to the whole branch name. */
export const globToRegExp = (glob: string): RegExp => {
  const escaped = glob.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${escaped}$`);
};

export interface Filters {
  prState?: Set<PrStateFilter>;
  olderThan?: Date;
  newerThan?: Date;
  ageBasis: AgeBasis;
  branch?: string;
  dirty?: boolean;
  prunable?: boolean;
  missing?: boolean;
  mergedIntoDefault?: boolean;
  locked?: boolean;
  /** Keep the main worktree in the result. Off for destructive commands. */
  includeMain?: boolean;
}

export const applyFilters = (worktrees: Worktree[], filters: Filters): Worktree[] => {
  const branchRe = filters.branch ? globToRegExp(filters.branch) : null;

  return worktrees.filter((wt) => {
    if (!filters.includeMain && wt.isMain) return false;
    if (filters.prState && !filters.prState.has(prStateOf(wt))) return false;
    if (filters.prunable !== undefined && wt.prunable !== filters.prunable) return false;
    if (filters.missing !== undefined && wt.missing !== filters.missing) return false;
    if (filters.locked !== undefined && wt.locked !== filters.locked) return false;
    if (filters.dirty !== undefined && wt.dirty !== filters.dirty) return false;
    if (
      filters.mergedIntoDefault !== undefined &&
      wt.mergedIntoDefault !== filters.mergedIntoDefault
    ) {
      return false;
    }
    if (branchRe && !(wt.branch && branchRe.test(wt.branch))) return false;

    if (filters.olderThan || filters.newerThan) {
      const ts = ageTimestamp(wt, filters.ageBasis);
      // No timestamp means we cannot prove the worktree is old. Fail closed.
      if (!ts) return false;
      const t = new Date(ts).getTime();
      if (Number.isNaN(t)) return false;
      if (filters.olderThan && t >= filters.olderThan.getTime()) return false;
      if (filters.newerThan && t <= filters.newerThan.getTime()) return false;
    }
    return true;
  });
};

export type SortKey = 'age' | 'path' | 'branch' | 'size' | 'pr';

export const sortWorktrees = (
  worktrees: Worktree[],
  key: SortKey,
  basis: AgeBasis,
  reverse = false,
): Worktree[] => {
  const sorted = [...worktrees].sort((a, b) => {
    switch (key) {
      case 'path':
        return a.path.localeCompare(b.path);
      case 'branch':
        return (a.branch ?? '').localeCompare(b.branch ?? '');
      case 'size':
        return (b.sizeKb ?? -1) - (a.sizeKb ?? -1);
      case 'pr':
        return prStateOf(a).localeCompare(prStateOf(b));
      case 'age':
      default: {
        // Oldest first; entries with no timestamp sort last.
        const da = ageDays(a, basis);
        const db = ageDays(b, basis);
        if (da === null && db === null) return a.path.localeCompare(b.path);
        if (da === null) return 1;
        if (db === null) return -1;
        return db - da;
      }
    }
  });
  // The main worktree is context, not a candidate: keep it pinned to the top.
  const main = sorted.filter((w) => w.isMain);
  const rest = reverse ? sorted.filter((w) => !w.isMain).reverse() : sorted.filter((w) => !w.isMain);
  return [...main, ...rest];
};
