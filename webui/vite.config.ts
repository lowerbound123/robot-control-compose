import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'
import { resolve } from 'node:path'
import createAiMiddleware from './dev-proxy.mjs'

const configPath = resolve(process.env.RC_WEBUI_CONFIG_PATH || fileURLToPath(new URL('./config.json', import.meta.url)))

export default defineConfig({
  plugins: [vue(), tailwindcss(), {
    name: 'server-owned-ai-proxy',
    configureServer(server) {
      server.middlewares.use(createAiMiddleware(configPath))
    },
  }],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    strictPort: true,
    fs: { deny: ['.env', '.env.*', '*.{crt,pem,key,p12,pfx,cer,der}', '.npmrc', '.yarnrc.yml',
      '**/.git/**', '**/config.json', configPath] },
    proxy: Object.fromEntries(['/mcp', '/preview/', '/health'].map(path => [path, {
      target: process.env.RC_WEBUI_UPSTREAM || 'http://127.0.0.1:4000',
      changeOrigin: true,
      headers: process.env.ROBOT_CONTROL_MCP_TOKEN ? { Authorization: `Bearer ${process.env.ROBOT_CONTROL_MCP_TOKEN}` } : {},
    }])),
  },
})
