import { collect, type CollectOptions, type Collection } from './collect.ts';
import { queryWorktrees, type QueryOptions } from './query.ts';

/** Public caller used by list/refresh: collection is complete before querying. */
export const loadCatalog = async ({
  collection,
  query = {},
}: { collection: CollectOptions; query?: QueryOptions }): Promise<Collection> => {
  const loaded = await collect(collection);
  return { ...loaded, worktrees: queryWorktrees({ worktrees: loaded.worktrees, options: query }) };
};
