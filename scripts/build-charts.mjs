import { build } from 'esbuild'
import { copyFile, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
const output = `${root}src/pluginbench/vendor/`
await mkdir(output, { recursive: true })
await build({
  absWorkingDir: root,
  stdin: {
    contents: `import { use } from 'echarts/core';
import { BarChart, ScatterChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, AriaComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
use([BarChart, ScatterChart, GridComponent, TooltipComponent, AriaComponent, SVGRenderer]);
export { init, dispose, getInstanceByDom, version } from 'echarts/core';`,
    resolveDir: root,
  },
  bundle: true,
  minify: true,
  format: 'iife',
  globalName: 'echarts',
  target: 'es2020',
  legalComments: 'eof',
  outfile: `${output}echarts.min.js`,
})
await copyFile(`${root}node_modules/echarts/LICENSE`, `${output}ECHARTS-LICENSE`)
await copyFile(`${root}node_modules/echarts/NOTICE`, `${output}ECHARTS-NOTICE`)
await copyFile(`${root}node_modules/zrender/LICENSE`, `${output}ZRENDER-LICENSE`)
