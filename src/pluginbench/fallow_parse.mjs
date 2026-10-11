// Syntax observer for Fallow 3.18.0's locked Oxc parser, not a task dependency.
// npm's provenance and the Rust crate both identify this exact upstream commit.
import { createHash } from 'node:crypto';
import { lstatSync, readFileSync, readdirSync, readlinkSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [cache, snapshot, input, output, expectedDependencyDigest] = process.argv.slice(2);
const version = '0.126.0';
const commit = '836fe9d51c11981162461216f37a067fc14441a7';
const integrity = 'sha512-FktCvLby/mOHyuijZt22+nOt10dS24gGUZE3XwIbUg7Kf4+rer3/5T7RgwzazlNuVsCjPloZ3p8E+4ONT3A8Kw==';
const hash = bytes => `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
const lockBytes = readFileSync(join(cache, 'package-lock.json'));
const lock = JSON.parse(lockBytes);
const pkg = JSON.parse(readFileSync(join(cache, 'node_modules/oxc-parser/package.json')));
const lockedParser = lock.packages['node_modules/oxc-parser'];
if (pkg.version !== version || lockedParser.version !== version || lockedParser.integrity !== integrity) {
  throw new Error('observer requires the pinned oxc-parser 0.126.0 package and npm lock');
}
const packageHashes = {};
const dependencyEntries = [];
const visit = (directory, relative = '', bindDependencies = true) => {
  for (const name of readdirSync(directory).sort()) {
    const path = join(directory, name);
    const key = relative ? `${relative}/${name}` : name;
    const link = lstatSync(path).isSymbolicLink();
    if (bindDependencies && link) dependencyEntries.push([key, Buffer.from(`link:${readlinkSync(path)}`)]);
    let stat;
    try {
      stat = statSync(path);
    } catch (error) {
      // verify.py records dangling link targets without dereferencing them.
      if (expectedDependencyDigest && link && error.code === 'ENOENT') continue;
      throw error;
    }
    if (stat.isDirectory()) visit(path, key, bindDependencies && !link);
    else if (stat.isFile()) {
      const bytes = readFileSync(path);
      packageHashes[key] = hash(bytes);
      if (bindDependencies && !link) dependencyEntries.push([key, bytes]);
    }
    else throw new Error(`unsupported observer dependency: ${key}`);
  }
};
visit(join(cache, 'node_modules'));
// Match verify.py's dependency_digest: Path-component order, link
// targets (without following directory links), and NUL-separated file bytes.
// Sorted recursive traversal follows pathlib.Path order (a/child before a.js).
const dependencyDigest = createHash('sha256');
for (const [key, bytes] of dependencyEntries) {
  dependencyDigest.update(`${key}\0`);
  dependencyDigest.update(bytes);
  dependencyDigest.update('\0');
}
const observedDependencyDigest = dependencyDigest.digest('hex');
if (expectedDependencyDigest && observedDependencyDigest !== expectedDependencyDigest) {
  throw new Error('observer dependency cache changed');
}
const bindings = Object.entries(lock.packages).filter(([name]) => name.startsWith('node_modules/@oxc-parser/binding-'));
const installedBindings = bindings.filter(([name]) => Object.keys(packageHashes).some(key => key.startsWith(name.slice('node_modules/'.length) + '/')));
if (installedBindings.length !== 1 || installedBindings[0][1].version !== version || !installedBindings[0][1].integrity) {
  throw new Error('observer requires exactly one pinned native Oxc binding');
}
const { parseSync } = await import(pathToFileURL(join(cache, 'node_modules/oxc-parser/src-js/index.js')));
const discovery = JSON.parse(readFileSync(input));
if (!Array.isArray(discovery.files) || discovery.file_count !== discovery.files.length || new Set(discovery.files).size !== discovery.files.length) {
  throw new Error('invalid Fallow input inventory');
}
const root = resolve(snapshot);
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
const files = discovery.files.map(path => {
  const absolute = resolve(root, path);
  if (!absolute.startsWith(root + sep)) throw new Error(`input escapes snapshot: ${path}`);
  const bytes = readFileSync(absolute);
  let errors;
  try {
    // Defaults match SourceType::from_path and ParseOptions::default; semantic
    // lint is disabled. Reject primary parse errors rather than guessing the
    // producer's token-count-dependent JSX retry result.
    errors = parseSync(path, decoder.decode(bytes)).errors.map(error => error.message);
  } catch (error) {
    errors = [String(error)];
  }
  return { path, source_digest: hash(bytes), status: errors.length ? 'invalid' : 'valid', errors };
});
writeFileSync(output, JSON.stringify({
  schema_version: 1,
  parser: 'oxc-parser', version, commit,
  parser_options: 'fallow-3.18.0-default-primary',
  lock_digest: hash(lockBytes),
  lock_source: lockBytes.toString('utf-8'),
  package_integrity: integrity,
  binding: Object.fromEntries(installedBindings),
  dependency_hashes: packageHashes,
  dependency_digest: observedDependencyDigest,
  expected_dependency_digest: expectedDependencyDigest ?? null,
  files,
}, null, 2) + '\n');
