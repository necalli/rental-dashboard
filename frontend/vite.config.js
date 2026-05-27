import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

const allowedHosts = (process.env.VITE_ALLOWED_HOSTS || '.ngrok-free.dev,.ngrok.dev,.ngrok.io')
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean)

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_URL || 'http://localhost:5002',
        changeOrigin: true,
        secure: false,
      },
    },
    allowedHosts,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
})
