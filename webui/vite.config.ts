import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'
import { readFile } from 'node:fs/promises'

export default defineConfig({
  plugins: [vue(), tailwindcss(), {
    name: 'local-runtime-config',
    configureServer(server) {
      server.middlewares.use('/config.json', async (_request, response) => {
        response.setHeader('Cache-Control', 'no-store')
        response.setHeader('Content-Type', 'application/json')
        try {
          response.end(await readFile(new URL('./config.json', import.meta.url)))
        } catch {
          response.statusCode = 404
          response.end('{"error":"WebUI config.json is missing"}')
        }
      })
    },
  }],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    strictPort: true,
    proxy: Object.fromEntries(['/mcp', '/preview/', '/health'].map(path => [path, {
      target: process.env.RC_WEBUI_UPSTREAM || 'http://127.0.0.1:4000',
      changeOrigin: true,
      headers: process.env.ROBOT_CONTROL_MCP_TOKEN ? { Authorization: `Bearer ${process.env.ROBOT_CONTROL_MCP_TOKEN}` } : {},
    }])),
  },
})
