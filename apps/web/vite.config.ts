import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Proxy the API so the browser sees one origin. Screenshots and SSE work without CORS
    // preflight, and the dashboard needs no base-URL configuration.
    proxy: {
      '/agent': { target: 'http://localhost:8000', changeOrigin: true },
      '/approvals': { target: 'http://localhost:8000', changeOrigin: true },
      '/profile': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000', changeOrigin: true },
      '/screenshots': { target: 'http://localhost:8000', changeOrigin: true },
      '/fixtures': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
