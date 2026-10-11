import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { it } from 'node:test';

it('keeps the analytics app as an indirect grid consumer', () => {
  const manifest = JSON.parse(readFileSync('packages/analytics-app/package.json', 'utf8'));
  assert.equal(manifest.dependencies['@fixture/grid-widget'], '1.0.0');
  assert.equal(manifest.dependencies['@kong-ui-public/table-data-grid'], undefined);
});
