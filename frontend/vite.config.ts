import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // 5174, not Vite's default 5173. strictPort so a busy port fails loudly at startup instead of
  // silently shifting to the next one — the backend's CORS accepts any localhost port either way,
  // but a predictable URL is worth more than a dev server that quietly moves.
  server: {
    port: 5174,
    strictPort: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '~': path.resolve(__dirname, './src'),
    },
  },
})
