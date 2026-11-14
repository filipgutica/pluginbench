// Report M1 mutation candidate sites in JavaScript and TypeScript files as UTF-8 byte spans.
// Parses with the pinned oxc-parser 0.126.0 that PluginBench's syntax observer installs; the
// caller verifies that cache before running this file.
// Usage: node mutation_sites.mjs <oxc node_modules> <snapshot root> <relative path>...
import { readFileSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [modules, snapshot, ...paths] = process.argv.slice(2);
const pkg = JSON.parse(readFileSync(join(modules, 'oxc-parser/package.json')));
if (pkg.version !== '0.126.0') throw new Error('mutation sites require the pinned oxc-parser 0.126.0');
const { parseSync } = await import(pathToFileURL(join(modules, 'oxc-parser/src-js/index.js')));

const OPERATORS = {
  '<': 'relational_boundary', '<=': 'relational_boundary',
  '>': 'relational_boundary', '>=': 'relational_boundary',
  '==': 'equality_negation', '!=': 'equality_negation',
  '===': 'equality_negation', '!==': 'equality_negation',
  '&&': 'boolean_connective', '||': 'boolean_connective',
};
const CONDITIONS = new Set(['IfStatement', 'WhileStatement', 'ConditionalExpression']);

// The operator token between two operands: skip whitespace, closing parentheses and comments.
const operatorStart = (source, from, operator) => {
  let index = from;
  while (index < source.length) {
    let next = index + 1;
    if (source.startsWith('//', index)) next = source.indexOf('\n', index);
    else if (source.startsWith('/*', index)) next = source.indexOf('*/', index) + 2;
    else if (!/\s|\)/.test(source[index])) break;
    if (next <= index) throw new Error('unterminated comment before an operator');
    index = next;
  }
  if (!source.startsWith(operator, index)) throw new Error(`operator ${operator} not found at ${from}`);
  return index;
};

const root = resolve(snapshot);
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
const rows = [];
const failures = [];
for (const path of paths) {
  const absolute = resolve(root, path);
  if (!absolute.startsWith(root + sep)) throw new Error(`path escapes snapshot: ${path}`);
  const source = decoder.decode(readFileSync(absolute));
  const parsed = parseSync(absolute, source);
  if (parsed.errors.length) {
    failures.push({ path, reason: parsed.errors[0].message });
    continue;
  }
  // Oxc spans are UTF-16 offsets; report UTF-8 byte offsets like the Go and Python finders.
  const bytes = new Int32Array(source.length + 1);
  for (let index = 0; index < source.length; index += 1) {
    const code = source.charCodeAt(index);
    bytes[index + 1] = bytes[index] + (code < 0x80 ? 1 : code < 0x800 ? 2 : code >= 0xd800 && code < 0xdc00 ? 4 : code >= 0xdc00 && code < 0xe000 ? 0 : 3);
  }
  const site = (operator, start, end) => rows.push({ path, operator, start: bytes[start], end: bytes[end] });
  const visit = node => {
    if (Array.isArray(node)) return node.forEach(visit);
    if (!node || typeof node !== 'object') return;
    if ((node.type === 'BinaryExpression' || node.type === 'LogicalExpression') && OPERATORS[node.operator]) {
      const start = operatorStart(source, node.left.end, node.operator);
      site(OPERATORS[node.operator], start, start + node.operator.length);
    }
    if (CONDITIONS.has(node.type)) site('condition_negation', node.test.start, node.test.end);
    for (const [key, value] of Object.entries(node)) if (key !== 'parent') visit(value);
  };
  visit(parsed.program);
}
process.stdout.write(JSON.stringify({ sites: rows, failures }) + '\n');
