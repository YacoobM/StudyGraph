import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,

    // ----------------------------------------------------------------------
    // BACKEND TEAMMATE: when the real API exists, uncomment this proxy so
    // that requests to /api/* are forwarded to the Flask/Node backend.
    // (You also need to switch ../src/services/api.js to real axios calls.)
    // ----------------------------------------------------------------------
    // proxy: {
    //   '/api': {
    //     target: 'http://localhost:5000',
    //     changeOrigin: true,
    //   },
    // },
  },
})
