import { SelectionState } from './selection-state.ts';

export const focusSelectedPath = (state: SelectionState, path: string): string | null =>
  state.has(path) ? path : null;
