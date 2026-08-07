import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served by FastAPI at /review in the container, so the built asset paths have
// to be relative to that. In development the dev server proxies the API to the
// container, which keeps the UI a client of exactly the same endpoints a script
// would call rather than a privileged path with its own shortcuts.
export default defineConfig({
  base: '/review/',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8010',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
