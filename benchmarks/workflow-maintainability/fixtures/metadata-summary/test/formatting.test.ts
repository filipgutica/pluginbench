import assert from 'node:assert/strict';
import { it } from 'node:test';
import { formatAge, formatSize } from '../src/formatting.ts';

it('formats elapsed age using established boundaries', () => {
  const now = new Date('2026-10-09T12:00:00Z');
  assert.equal(formatAge(null, now), 'unknown');
  assert.equal(formatAge('2026-10-09T12:01:00Z', now), 'just now');
  assert.equal(formatAge('2026-10-09T11:59:01Z', now), 'just now');
  assert.equal(formatAge('2026-10-09T11:59:00Z', now), '1m');
  assert.equal(formatAge('2026-10-09T11:00:00Z', now), '1h');
  assert.equal(formatAge('2026-10-08T12:00:00Z', now), '1d');
});

it('formats disk sizes using established boundaries', () => {
  assert.equal(formatSize(null), 'unknown');
  assert.equal(formatSize(0), '0B');
  assert.equal(formatSize(1023), '1023B');
  assert.equal(formatSize(1024), '1.0KiB');
  assert.equal(formatSize(1024 ** 2), '1.0MiB');
});
