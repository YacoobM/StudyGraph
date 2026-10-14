import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The app calls the FastAPI backend directly at VITE_API_URL (see src/services/api.js).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
  },
})
