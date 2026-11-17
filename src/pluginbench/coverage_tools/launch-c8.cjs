'use strict'

const fs = require('node:fs')
const path = require('node:path')
const root = fs.realpathSync(__dirname)
const cwd = process.cwd()
const config = path.join(root, 'c8-config.json')
const bin = path.join(root, 'node_modules/c8/bin/c8.js')

function refuse (reason) {
  throw new Error('PluginBench c8: ' + reason)
}

function checkedOrigin (filename) {
  const origin = fs.realpathSync(filename)
  if (!origin.startsWith(root + path.sep)) refuse('module outside staged distribution: ' + origin)
  return origin
}

// The backend clears Node startup injection before starting this process. Never pass it
// to the instrumentee, and do not enable c8's alternative reporter implementation.
for (const name of ['NODE_OPTIONS', 'NODE_PATH', 'EXPERIMENTAL_MONOCART']) delete process.env[name]

const parseArgs = require('./node_modules/c8/lib/parse-args')
const buildYargs = parseArgs.buildYargs
const reporter = checkedOrigin(require.resolve('./node_modules/istanbul-reports/lib/lcovonly'))
const origin = {
  module: checkedOrigin(bin),
  reporter,
  config: checkedOrigin(config),
  dependencies: Object.fromEntries(Object.keys(require.cache).map(file => [file, checkedOrigin(file)]))
}
if (process.argv.slice(2).join(' ') === '--pluginbench-verify') {
  console.log(JSON.stringify(origin))
} else {
  const input = process.argv.slice(2)
  const separator = input.indexOf('--')
  if (separator < 0 || separator === input.length - 1) refuse('use [options] -- command [arguments]')
  const options = []
  const values = new Set(['--src', '--include', '--exclude', '--extension', '--reports-dir',
    '--report-dir', '--temp-directory', '--reporter', '-r'])
  const booleans = new Set(['--all', '--exclude-after-remap', '--exclude-node-modules'])
  for (let index = 0; index < separator; index++) {
    const [name, ...parts] = input[index].split('=')
    if (booleans.has(name)) {
      if (parts.length && !['true', 'false'].includes(parts.join('='))) refuse('invalid boolean: ' + name)
      options.push(input[index])
    } else if (values.has(name)) {
      const value = parts.length ? parts.join('=') : input[++index]
      if (index >= separator || value === undefined) refuse('missing value: ' + name)
      if (['--reporter', '-r'].includes(name)) {
        if (value !== 'lcovonly') refuse('only the built-in lcovonly reporter is allowed')
      } else options.push(name + '=' + value)
    } else refuse('unsupported option: ' + name)
  }
  // c8 also merges package.json config even with --config. Construct AND parse yargs
  // outside the task tree, then restore cwd before running the measured command.
  parseArgs.buildYargs = withCommands => ({
    parse (args) {
      process.chdir(root)
      try {
        const argv = buildYargs(withCommands).parse(args)
        const reporters = Array.isArray(argv.reporter) ? argv.reporter : [argv.reporter]
        if (reporters.some(name => name !== 'lcovonly')) refuse('only lcovonly is allowed')
        const reports = path.resolve(cwd, argv.reportsDir)
        argv.reportsDir = argv['reports-dir'] = reports
        argv.tempDirectory = options.some(value => value.startsWith('--temp-directory='))
          ? path.resolve(cwd, argv.tempDirectory) : path.join(reports, 'tmp')
        return argv
      } finally {
        process.chdir(cwd)
      }
    }
  })
  process.argv = [process.execPath, bin, '--config=' + config, ...options,
    ...input.slice(separator + 1)]
  require(bin)
}
