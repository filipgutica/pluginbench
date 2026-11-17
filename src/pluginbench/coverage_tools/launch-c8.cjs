'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
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
const v8ToIstanbul = require('./node_modules/v8-to-istanbul')
const { TraceMap, decodedMappings } = require('./node_modules/@jridgewell/trace-mapping')
// The pinned converter has no public ignore-hint switch. Disable its single hint parser.
require('./node_modules/v8-to-istanbul/lib/source').prototype._parseIgnore = () => undefined
const remap = JSON.parse(fs.readFileSync(path.join(root, 'source-maps.json'), 'utf8'))
const settings = JSON.parse(fs.readFileSync(config, 'utf8'))
if (JSON.stringify(settings) !== '{"reporter":["lcovonly"],"all":true}') refuse('unexpected pinned config')
const origin = {
  module: checkedOrigin(require.resolve('./node_modules/c8/lib/report')),
  reporter: checkedOrigin(require.resolve('./node_modules/istanbul-reports/lib/lcovonly')),
  config: checkedOrigin(config),
  source_files: checkedOrigin(path.join(root, 'source-files.json')),
  dependencies: Object.fromEntries(Object.keys(require.cache).map(file => [file, checkedOrigin(file)]))
}

function verifiedMap (map, script) {
  if (!Array.isArray(map.sources) || !map.sources.length || map.sections) refuse('unsupported source map')
  if (map.sourcesContent && map.sourcesContent.length !== map.sources.length) refuse('invalid sourcesContent')
  if (typeof map.mappings !== 'string' || !map.mappings.trim()) refuse('empty mappings')
  const segments = decodedMappings(new TraceMap(map))
  const mappedSources = new Set(segments.flat().filter(segment => segment.length >= 4).map(segment => segment[1]))
  if (map.sources.some((_, index) => !mappedSources.has(index))) refuse('source has zero mapped segments')
  const generated = fs.readFileSync(script, 'utf8').trimEnd().split(/\r?\n/)
  if (generated.some((line, index) => line.trim() && !line.trim().startsWith('//') &&
    !(segments[index] || []).some(segment => segment.length >= 4))) refuse('unmapped generated line')
  // Reuse the pinned converter's resolver, including sourceRoot and file/webpack URLs.
  const converter = v8ToIstanbul(script)
  const contents = map.sources.map((source, index) => {
    const filename = fs.realpathSync(converter._resolveSource({ sourcemap: map }, source))
    if (!filename.startsWith(cwd + path.sep)) refuse('source map resolves outside task tree: ' + filename)
    const frozen = fs.readFileSync(filename)
    const embedded = map.sourcesContent && map.sourcesContent[index]
    if (embedded != null && (typeof embedded !== 'string' || !Buffer.from(embedded).equals(frozen))) {
      refuse('sourcesContent differs from frozen source: ' + filename)
    }
    return frozen.toString('utf8')
  })
  return { ...map, sourcesContent: contents }
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
    process.exitCode = child.status === null ? 1 : child.status
    return
  }
  const report = createReport({
    ...settings, reportsDirectory: path.join(cwd, 'coverage'), tempDirectory,
    src: [cwd], resolve: cwd, omitRelative: true, excludeNodeModules: true
  })
  const merge = report._getMergedProcessCov.bind(report)
  report._getMergedProcessCov = () => {
    const merged = merge()
    if (!remap) return merged
    // Validate before c8's conversion loop, which catches and drops converter errors.
    // Cover V8's cache, disk maps, inline maps, and the --all unloaded-file path.
    for (const script of merged.result) {
      const key = pathToFileURL(script.url).href
      const cached = report.sourceMapCache[key]
      const source = fs.readFileSync(script.url, 'utf8')
      const fallback = convertSourceMap.fromSource(source) || convertSourceMap.fromMapFileSource(
        source, filename => fs.readFileSync(path.resolve(path.dirname(script.url), filename), 'utf8'))
      const map = (cached && cached.data) || (fallback && fallback.sourcemap)
      if (map) report.sourceMapCache[key] = { ...cached, data: verifiedMap(map, script.url) }
    }
    return merged
  }
  if (!remap) report._getSourceMap = script => ({
    // Blank map comments without shifting offsets. The converter cannot discover a map.
    source: fs.readFileSync(script.url, 'utf8')
      .replace(convertSourceMap.commentRegex, value => value.replace(/[^\r\n]/g, ' '))
      .replace(convertSourceMap.mapFileCommentRegex, value => value.replace(/[^\r\n]/g, ' '))
  })
  const sourceFiles = JSON.parse(fs.readFileSync(path.join(root, 'source-files.json'), 'utf8'))
  if (sourceFiles) {
    const allowed = new Set(sourceFiles)
    const coverageMap = report.getCoverageMapFromAllCoverageFiles.bind(report)
    report.getCoverageMapFromAllCoverageFiles = async () => {
      const map = await coverageMap()
      map.filter(filename => allowed.has(path.relative(cwd, filename)))
      return map
    }
  }
  await report.run()
}

main().catch(error => {
  console.error(error.stack)
  process.exitCode = 1
})
