import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import type { OutputAsset, OutputChunk } from 'rollup'
import { getManualChunk } from '../build/chunks.ts'

type BuildOutput = Array<OutputAsset | OutputChunk>
let buildOutput: Promise<BuildOutput> | undefined

/** 内存构建使用真实 Vite 配置，既不读取旧 dist，也不覆盖其他构建产物。 */
function productionOutput() {
  buildOutput ||= build({
    root: fileURLToPath(new URL('../', import.meta.url)),
    configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
    logLevel: 'silent',
    build: { write: false, reportCompressedSize: false }
  }).then((result) => {
    if ('on' in result) throw new Error('Expected a production build, not a watcher')
    return (Array.isArray(result) ? result : [result]).flatMap(item => item.output)
  })
  return buildOutput
}

function javascript(output: BuildOutput): OutputChunk[] {
  return output.filter((item): item is OutputChunk => item.type === 'chunk')
}

function staticGraph(chunks: OutputChunk[], root: OutputChunk): OutputChunk[] {
  const byFile = new Map(chunks.map(chunk => [chunk.fileName, chunk]))
  const visited = new Set<string>()
  function visit(chunk: OutputChunk) {
    if (visited.has(chunk.fileName)) return
    visited.add(chunk.fileName)
    for (const dependency of chunk.imports) {
      const target = byFile.get(dependency)
      assert.ok(target, `Missing chunk: ${dependency}`)
      visit(target)
    }
  }
  visit(root)
  return [...visited].map(file => byFile.get(file)!)
}

function renderedModules(chunks: OutputChunk[]) {
  return chunks.flatMap(chunk => Object.entries(chunk.modules)
    .filter(([, module]) => module.renderedLength > 0)
    .map(([id]) => id.replace(/\\/g, '/')))
}

test('dependency groups support Windows, scoped and nested package paths without matching application code', () => {
  for (const [module, expected] of [
    ['vue/dist/vue.runtime.esm-bundler.js', 'vendor-vue'],
    ['@vue/runtime-core/dist/runtime-core.esm-bundler.js', 'vendor-vue'],
    ['pinia/dist/pinia.mjs', 'vendor-vue'],
    ['vue-router/dist/vue-router.mjs', 'vendor-vue'],
    ['vue-i18n/dist/vue-i18n.mjs', 'vendor-i18n'],
    ['@intlify/core-base/dist/core-base.mjs', 'vendor-i18n'],
    ['axios/lib/axios.js', 'vendor-http'],
    ['@element-plus/icons-vue/dist/index.js', 'vendor-icons'],
    ['highlight.js/lib/common.js', 'vendor-highlight'],
    ['markdown-it/lib/index.mjs', 'vendor-markdown'],
    ['linkify-it/index.mjs', 'vendor-markdown']
  ]) {
    assert.equal(getManualChunk(`/project/node_modules/${module}`), expected, module)
    assert.equal(getManualChunk(`C:\\project\\node_modules\\${module.replace(/\//g, '\\')}`), expected, module)
  }
  assert.equal(getManualChunk('/project/node_modules/.pnpm/axios@1/node_modules/axios/index.js'), 'vendor-http')
  for (const module of ['/project/src/vendor-vue.ts', '/project/src/views/ChatView.vue', '/project/node_modules/axios-extra/index.js', '/project/node_modules/unknown/index.js', '/project/node_modules/element-plus/es/components/table/index.mjs']) {
    assert.equal(getManualChunk(module), undefined, module)
  }
})

test('production JavaScript chunks stay within the default 500 kB warning threshold', async () => {
  const chunks = javascript(await productionOutput())
  const entry = chunks.find(chunk => chunk.isEntry)
  assert.ok(entry)
  assert.ok(Buffer.byteLength(entry.code) < 200_000, 'Application entry must stay below 200 kB')
  for (const chunk of chunks) {
    assert.ok(Buffer.byteLength(chunk.code) <= 500_000, `${chunk.fileName} exceeds 500 kB`)
  }
  for (const name of ['vendor-vue', 'vendor-i18n', 'vendor-http', 'vendor-icons', 'vendor-markdown', 'vendor-highlight']) {
    assert.ok(chunks.some(chunk => chunk.name === name), `Missing dependency group: ${name}`)
  }
})

test('all business pages remain dynamic entries and login excludes chat, table and upload dependencies', async () => {
  const chunks = javascript(await productionOutput())
  for (const name of ['AppLayout', 'LoginView', 'ChatView', 'KnowledgeView', 'DocumentsView', 'UsersView', 'AuditView', 'ProfileView']) {
    assert.ok(chunks.find(chunk => chunk.name === name)?.isDynamicEntry, `${name} must remain lazy`)
  }
  for (const root of [chunks.find(chunk => chunk.isEntry)!, chunks.find(chunk => chunk.name === 'LoginView')!]) {
    const modules = renderedModules(staticGraph(chunks, root))
    for (const dependency of ['/markdown-it/', '/highlight.js/', '/element-plus/es/components/table/', '/element-plus/es/components/upload/', '/@noble/hashes/']) {
      assert.equal(modules.some(id => id.includes(dependency)), false, `${dependency} leaked into ${root.name}`)
    }
  }
})

test('chat loads Markdown, code highlighting and its styles only through the chat dependency graph', async () => {
  const output = await productionOutput()
  const chunks = javascript(output)
  const chat = chunks.find(chunk => chunk.name === 'ChatView')!
  const graph = staticGraph(chunks, chat)
  for (const dependency of ['vendor-markdown', 'vendor-highlight']) {
    assert.ok(graph.some(chunk => chunk.name === dependency), `${dependency} must load with chat`)
  }
  const manifestAsset = output.find((item): item is OutputAsset => item.type === 'asset' && item.fileName === '.vite/manifest.json')
  assert.ok(manifestAsset)
  const manifest = JSON.parse(String(manifestAsset.source)) as Record<string, { file: string; css?: string[] }>
  const highlight = graph.find(chunk => chunk.name === 'vendor-highlight')!
  const styles = Object.values(manifest).find(item => item.file === highlight.fileName)?.css || []
  assert.ok(styles.some(file => file.includes('vendor-highlight') && file.endsWith('.css')))
})

test('on-demand loading retains loading directives and required popup styles but excludes unused UI components', async () => {
  const output = await productionOutput()
  const chunks = javascript(output)
  for (const page of ['KnowledgeView', 'DocumentsView', 'UsersView', 'AuditView']) {
    const root = chunks.find(chunk => chunk.name === page)!
    assert.ok(renderedModules(staticGraph(chunks, root)).some(id => id.includes('/loading/src/directive')), `v-loading is missing from ${page}`)
  }
  const modules = renderedModules(chunks)
  for (const unused of ['/element-plus/es/components/date-picker/', '/element-plus/es/components/cascader/', '/element-plus/es/components/tree-v2/']) {
    assert.equal(modules.some(id => id.includes(unused)), false, `${unused} should be tree-shaken`)
  }
  const css = output.filter((item): item is OutputAsset => item.type === 'asset' && item.fileName.endsWith('.css'))
    .map(item => String(item.source)).join('\n')
  for (const selector of ['.el-message{', '.el-message-box{', '.el-loading-mask{', '.el-overlay{']) {
    assert.ok(css.includes(selector), `Missing UI styles: ${selector}`)
  }
})
