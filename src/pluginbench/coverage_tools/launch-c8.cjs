'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { constants } = require('node:os')
const { spawnSync } = require('node:child_process')
const root = fs.realpathSync(__dirname)
const cwd = fs.realpathSync(process.cwd())
const config = path.join(root, 'c8-config.json')

function refuse (reason) {
  throw new Error('PluginBench c8: ' + reason)
}

function checkedOrigin (filename) {
  const origin = fs.realpathSync(filename)
  if (!origin.startsWith(root + path.sep)) refuse('module outside staged distribution: ' + origin)
  return origin
}

// The backend clears startup injection before Node starts; do not pass it to the child.
for (const name of ['NODE_OPTIONS', 'NODE_PATH', 'EXPERIMENTAL_MONOCART']) delete process.env[name]

const createReport = require('./node_modules/c8/lib/report')
const convertSourceMap = require('./node_modules/convert-source-map')
// c8 and v8-to-istanbul share this pinned copy. Never discover task-provided maps.
convertSourceMap.fromSource = () => null
convertSourceMap.fromMapFileSource = () => null
// The pinned converter has no public ignore-hint switch. Disable its single hint parser.
require('./node_modules/v8-to-istanbul/lib/source').prototype._parseIgnore = () => undefined
const settings = JSON.parse(fs.readFileSync(config, 'utf8'))
if (JSON.stringify(settings) !== '{"reporter":["lcovonly"],"all":true}') refuse('unexpected pinned config')
const origin = {
  executable: fs.realpathSync(process.execPath),
  workdir: cwd,
  module: checkedOrigin(require.resolve('./node_modules/c8/lib/report')),
  reporter: checkedOrigin(require.resolve('./node_modules/istanbul-reports/lib/lcovonly')),
  config: checkedOrigin(config),
  source_files: checkedOrigin(path.join(root, 'source-files.json')),
  dependencies: Object.fromEntries(Object.keys(require.cache).map(file => [file, checkedOrigin(file)]))
}

async function main () {
  const input = process.argv.slice(2)
  if (input.length === 1 && input[0] === '--pluginbench-verify') {
    console.log(JSON.stringify(origin))
    return
  }
  const tempDirectory = path.join(root, 'data')
  if (input.length !== 1 || input[0] !== 'report') {
    // Task arguments never reach a tool option parser. Collection never reports.
    if (input[0] !== '--all' || input[1] !== '--' || input.length < 3 || input[2].startsWith('-')) {
      refuse('use --all -- command [arguments]')
    }
    fs.mkdirSync(tempDirectory)
    const child = spawnSync(input[2], input.slice(3), {
      cwd, stdio: 'inherit', env: { ...process.env, NODE_V8_COVERAGE: tempDirectory }
    })
    if (child.error) throw child.error
    if (child.signal) {
      process.kill(process.pid, child.signal)
      // Node ignores some signals (including SIGPIPE); retain the child's failure anyway.
      process.exitCode = 128 + constants.signals[child.signal]
      return
    }
    process.exitCode = child.status === null ? 1 : child.status
    return
  }
  const sourceFiles = JSON.parse(fs.readFileSync(path.join(root, 'source-files.json'), 'utf8'))
  if (!sourceFiles || sourceFiles.length === 0) refuse('no files in the profile source scope')
  const report = createReport({
    ...settings, exclude: [],
    extension: JSON.parse(fs.readFileSync(path.join(root, 'source-extensions.json'), 'utf8')),
    reportsDirectory: path.join(cwd, 'coverage'), tempDirectory,
    src: [cwd], resolve: cwd, omitRelative: true, excludeNodeModules: true
  })
  // Also ignore V8's map cache. Source bytes remain unchanged, preserving V8 offsets.
  report._getSourceMap = () => ({})
  const allowed = new Set(sourceFiles)
  const coverageMap = report.getCoverageMapFromAllCoverageFiles.bind(report)
  report.getCoverageMapFromAllCoverageFiles = async () => {
    const map = await coverageMap()
    map.filter(filename => allowed.has(path.relative(cwd, filename)))
    return map
  }
  await report.run()
}

main().catch(error => {
  console.error(error.stack)
  process.exitCode = 1
})
