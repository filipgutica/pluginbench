import assert from 'node:assert/strict';
import { it } from 'node:test';
import { SelectionState } from '../src/selection-state.ts';
import { focusSelectedPath } from '../src/picker.ts';

it('preserves membership through toggle, replacement, and clear', () => {
  const state = new SelectionState();
  assert.equal(focusSelectedPath(state, '/a'), null);
  state.toggle('/a');
  assert.equal(focusSelectedPath(state, '/a'), '/a');
  state.toggle('/a');
  assert.equal(state.has('/a'), false);
  state.replace(['/b', '/b', '/c']);
  assert.equal(state.has('/a'), false);
  assert.equal(state.has('/b'), true);
  assert.equal(state.has('/c'), true);
  state.clear();
  assert.equal(state.has('/b'), false);
});
