import { defineConfig, type ProxyOptions } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'
import { getManualChunk } from './build/chunks.ts'

/**
 * 开发态默认代理目标：保持本地 uvicorn 的 8000 端口不变。
 * E2E 由 e2e_env.py 通过环境变量指向它自己启动的独占 API。
 */
const DEV_API_TARGET = 'http://localhost:8000'

function apiProxy(target: string, isE2E: boolean): Record<string, ProxyOptions> {
  return {
    '/api': {
      target,
      changeOrigin: true
    },
    ...(isE2E ? { '/__e2e__': { target, changeOrigin: true } } : {})
  }
}

export default defineConfig(({ mode }) => {
  // 测试/E2E 构建禁止读取任何 .env：仓库根目录与 backend 都有真实部署配置，
  // 构建过程不该把它们（尤其是模型密钥）载入 Vite 的 import.meta.env。
  const isE2E = mode === 'e2e' || process.env.RAG_E2E === '1'
  const apiTarget = process.env.RAG_E2E_API_TARGET || DEV_API_TARGET

  return {
    // false 表示完全不加载 env 文件；开发/生产构建保持 Vite 默认行为。
    envDir: isE2E ? false : undefined,
    plugins: [
      vue(),
      Components({
        dts: false,
        resolvers: [ElementPlusResolver({ importStyle: 'css', directives: true })]
      })
    ],
    build: {
      manifest: true,
      rollupOptions: {
        output: {
          onlyExplicitManualChunks: true,
          manualChunks: getManualChunk
        }
      }
    },
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
    },
    server: {
      port: 5173,
      proxy: apiProxy(apiTarget, isE2E)
    },
    // 浏览器跑的是生产构建，`vite preview` 也必须能把 /api 转给本地后端；
    // 否则 E2E 只能靠另一个反向代理，测试与真实部署的路径就不一致了。
    preview: {
      port: 4173,
      proxy: apiProxy(apiTarget, isE2E)
    }
  }
})
