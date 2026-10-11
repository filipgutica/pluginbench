import { InvalidFilterError, parseCutoff, parsePrStates, type Filters } from './filter.ts';
import type { AgeBasis } from './types.ts';

export interface FilterOptions {
  done?: boolean;
  prState?: string;
  olderThan?: string;
  newerThan?: string;
  branch?: string;
  dirty?: boolean;
  prunable?: boolean;
  missing?: boolean;
  merged?: boolean;
  locked?: boolean;
}

export const normalizeDone = <T extends FilterOptions>(f: T): T => {
  if (!f.done) return f;
  if (f.prState !== undefined) {
    throw new InvalidFilterError('--done means --pr-state merged,closed; pass one or the other.');
  }
  return { ...f, prState: 'merged,closed' };
};

export const buildFilters = ({
  options: f, ageBasis, includeMain,
}: { options: FilterOptions; ageBasis: AgeBasis; includeMain: boolean }): Filters => ({
  ageBasis,
  includeMain,
  ...(f.prState !== undefined ? { prState: parsePrStates(f.prState) } : {}),
  ...(f.olderThan !== undefined ? { olderThan: parseCutoff(f.olderThan) } : {}),
  ...(f.newerThan !== undefined ? { newerThan: parseCutoff(f.newerThan) } : {}),
  ...(f.branch !== undefined ? { branch: f.branch } : {}),
  ...(f.dirty !== undefined ? { dirty: f.dirty } : {}),
  ...(f.prunable !== undefined ? { prunable: f.prunable } : {}),
  ...(f.missing !== undefined ? { missing: f.missing } : {}),
  ...(f.merged !== undefined ? { mergedIntoDefault: f.merged } : {}),
  ...(f.locked !== undefined ? { locked: f.locked } : {}),
});
